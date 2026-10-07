# Copyright (c) 2026, SpaceCode and Contributors
# See license.txt

"""Focused unit tests for the Codex transport wire dialect.

The Codex backend serves streaming responses only and never stores data, so the
transport must merge the upstream `openai_codex_model_profile` into every model
profile: ordinary `request()` calls are forced through the upstream stream
aggregation path with `store=false` and list-shaped input, while
`request_stream()` keeps native streaming. All provider failures stay sanitized.

The semantic-preservation tests round-trip upstream typed Responses fixtures
through the adapter's public `request()` surface and inspect the actual
`responses.create` kwargs, pinning that text, tool calls, tool results, history,
and reasoning survive the forced-stream aggregation and request mapping intact.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, Mock, patch

from openai.types import responses
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
	ModelRequest,
	PartDeltaEvent,
	PartStartEvent,
	SystemPromptPart,
	TextContent,
	TextPart,
	TextPartDelta,
	ThinkingPart,
	ToolCallPart,
	ToolReturnPart,
	UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OMIT, OpenAIResponsesModel, OpenAIResponsesStreamedResponse
from pydantic_ai.output import OutputObjectDefinition
from pydantic_ai.tools import ToolDefinition

from afaa.ai.provider.openai_codex_provider import OpenAICodexProvider
from afaa.ai.provider.openai_codex_transport import (
	CodexProviderError,
	merge_codex_model_profile,
)


def build_codex_transport_model(mark_authentication_expired=None, model_id="gpt-codex-test"):
	"""Build the transport model the same way `AI Provider Account` runtime does."""
	account = SimpleNamespace(external_account_id="account-123")
	provider = OpenAICodexProvider(account)
	if mark_authentication_expired is not None:
		provider.mark_authentication_expired = mark_authentication_expired
	with patch.object(provider, "resolve_access_token", return_value="oauth-access-token"):
		return provider, provider.build_model(SimpleNamespace(model_id=model_id))


def codex_response(**overrides):
	fields = {
		"id": "resp_codex_test",
		"created_at": 1700000000.0,
		"model": "gpt-codex-test",
		"object": "response",
		"output": [],
		"parallel_tool_calls": False,
		"tool_choice": "none",
		"tools": [],
	}
	fields.update(overrides)
	return responses.Response(**fields)


def codex_usage():
	return responses.ResponseUsage(
		input_tokens=3,
		input_tokens_details={"cached_tokens": 0, "cache_write_tokens": 0},
		output_tokens=5,
		output_tokens_details={"reasoning_tokens": 0},
		total_tokens=8,
	)


def codex_stream_events(text="Hello from the Codex stream"):
	"""Plain-text Responses stream: created, two text deltas, completed."""
	split = len(text) // 2
	completed_message = responses.ResponseOutputMessage(
		id="msg_codex_test",
		content=[responses.ResponseOutputText(text=text, type="output_text", annotations=[])],
		role="assistant",
		status="completed",
		type="message",
	)
	return [
		responses.ResponseCreatedEvent(
			response=codex_response(status="in_progress"),
			sequence_number=0,
			type="response.created",
		),
		responses.ResponseTextDeltaEvent(
			item_id="msg_codex_test",
			output_index=0,
			content_index=0,
			delta=text[:split],
			logprobs=[],
			sequence_number=1,
			type="response.output_text.delta",
		),
		responses.ResponseTextDeltaEvent(
			item_id="msg_codex_test",
			output_index=0,
			content_index=0,
			delta=text[split:],
			logprobs=[],
			sequence_number=2,
			type="response.output_text.delta",
		),
		responses.ResponseCompletedEvent(
			response=codex_response(
				output=[completed_message],
				status="completed",
				usage=codex_usage(),
			),
			sequence_number=3,
			type="response.completed",
		),
	]


WEATHER_TOOL = ToolDefinition(
	name="get_weather",
	parameters_json_schema={
		"type": "object",
		"properties": {"city": {"type": "string"}},
		"required": ["city"],
	},
	description="Look up current weather.",
)

CITY_WEATHER_SCHEMA = {
	"type": "object",
	"properties": {"city": {"type": "string"}, "high_c": {"type": "number"}},
	"required": ["city", "high_c"],
}


def codex_tool_call_events():
	"""Function-call Responses stream: created, call item added, completed."""
	wire_call = responses.ResponseFunctionToolCall(
		arguments='{"city": "Paris"}',
		call_id="call_1",
		name="get_weather",
		id="fc_1",
		type="function_call",
	)
	return [
		responses.ResponseCreatedEvent(
			response=codex_response(status="in_progress"),
			sequence_number=0,
			type="response.created",
		),
		responses.ResponseOutputItemAddedEvent(
			item=wire_call,
			output_index=0,
			sequence_number=1,
			type="response.output_item.added",
		),
		responses.ResponseCompletedEvent(
			response=codex_response(
				output=[
					responses.ResponseFunctionToolCall(
						arguments='{"city": "Paris"}',
						call_id="call_1",
						name="get_weather",
						id="fc_1",
						status="completed",
						type="function_call",
					)
				],
				status="completed",
				usage=codex_usage(),
			),
			sequence_number=2,
			type="response.completed",
		),
	]


def codex_reasoning_then_text_events():
	"""Reasoning Responses stream: encrypted reasoning item, then answer text."""
	completed_reasoning = responses.ResponseReasoningItem(
		id="rs_1",
		summary=[{"text": "Weighing night trains.", "type": "summary_text"}],
		encrypted_content="enc-codex-payload",
		type="reasoning",
	)
	completed_message = responses.ResponseOutputMessage(
		id="msg_1",
		content=[responses.ResponseOutputText(text="Night train it is.", type="output_text", annotations=[])],
		role="assistant",
		status="completed",
		type="message",
	)
	return [
		responses.ResponseCreatedEvent(
			response=codex_response(status="in_progress"),
			sequence_number=0,
			type="response.created",
		),
		responses.ResponseOutputItemAddedEvent(
			item=responses.ResponseReasoningItem(id="rs_1", summary=[], type="reasoning"),
			output_index=0,
			sequence_number=1,
			type="response.output_item.added",
		),
		responses.ResponseReasoningSummaryTextDeltaEvent(
			item_id="rs_1",
			output_index=0,
			summary_index=0,
			delta="Weighing night trains.",
			sequence_number=2,
			type="response.reasoning_summary_text.delta",
		),
		responses.ResponseOutputItemDoneEvent(
			item=completed_reasoning,
			output_index=0,
			sequence_number=3,
			type="response.output_item.done",
		),
		responses.ResponseTextDeltaEvent(
			item_id="msg_1",
			output_index=1,
			content_index=0,
			delta="Night train it is.",
			logprobs=[],
			sequence_number=4,
			type="response.output_text.delta",
		),
		responses.ResponseCompletedEvent(
			response=codex_response(
				output=[completed_reasoning, completed_message],
				status="completed",
				usage=codex_usage(),
			),
			sequence_number=5,
			type="response.completed",
		),
	]


class FakeEventStream:
	"""Minimal stand-in for `AsyncStream[responses.ResponseStreamEvent]`."""

	def __init__(self, events):
		self._iterator = iter(events)
		self.closed = False

	def __aiter__(self):
		return self

	async def __anext__(self):
		try:
			return next(self._iterator)
		except StopIteration:
			raise StopAsyncIteration from None

	async def __aenter__(self):
		return self

	async def __aexit__(self, *exc_info):
		await self.close()
		return False

	async def close(self):
		self.closed = True


class TestCodexTransportProfile(TestCase):
	def test_codex_profile_merges_stream_only_and_store_false_dialect(self):
		_, model = build_codex_transport_model()
		profile = model.profile

		self.assertTrue(profile["openai_responses_requires_streaming"])
		self.assertTrue(profile["openai_responses_requires_store_false"])
		self.assertFalse(profile["openai_supports_input_token_counting"])
		for setting in ("max_tokens", "temperature", "top_p"):
			self.assertIn(setting, profile["openai_unsupported_model_settings"])

	def test_merge_codex_model_profile_cannot_relax_codex_dialect(self):
		merged = merge_codex_model_profile(
			"gpt-codex-test",
			{
				"openai_responses_requires_streaming": False,
				"openai_responses_requires_store_false": False,
				"context_window": 12345,
			},
		)

		self.assertTrue(merged["openai_responses_requires_streaming"])
		self.assertTrue(merged["openai_responses_requires_store_false"])
		# Non-dialect caller hints are still merged through.
		self.assertEqual(merged["context_window"], 12345)

	def test_merge_codex_model_profile_without_caller_profile(self):
		merged = merge_codex_model_profile("gpt-codex-test")

		self.assertTrue(merged["openai_responses_requires_streaming"])
		self.assertTrue(merged["openai_responses_requires_store_false"])


class TestCodexTransportRequest(TestCase):
	def test_request_forces_stream_aggregation_with_store_false_and_list_input(self):
		_, model = build_codex_transport_model()
		stream = FakeEventStream(codex_stream_events())
		create = AsyncMock(return_value=stream)

		with (
			patch("pydantic_ai.models.ALLOW_MODEL_REQUESTS", True),
			patch.object(model.client.responses, "create", create),
		):
			response = asyncio.run(
				model.request(
					[ModelRequest.user_text_prompt("Say something")],
					{"temperature": 0.2, "openai_store": True},
					ModelRequestParameters(),
				)
			)
		asyncio.run(model.client.close())

		create.assert_awaited_once()
		kwargs = create.await_args.kwargs
		# The Codex backend serves streaming responses only and never stores data.
		self.assertIs(kwargs["stream"], True)
		self.assertIs(kwargs["store"], False)
		# Responses wire input is a list of input items, never a bare string.
		self.assertIsInstance(kwargs["input"], list)
		self.assertEqual(kwargs["model"], "gpt-codex-test")
		# Unsupported sampling settings are dropped before sending.
		self.assertIs(kwargs["temperature"], OMIT)
		# Plain text is aggregated from the forced stream into one response.
		self.assertEqual(response.parts[0].content, "Hello from the Codex stream")
		self.assertTrue(stream.closed)

	def test_profile_enforces_stream_and_store_even_when_settings_disagree(self):
		_, model = build_codex_transport_model()
		stream = FakeEventStream(codex_stream_events())
		create = AsyncMock(return_value=stream)

		def passthrough_prepare_request(model_settings, model_request_parameters):
			return model_settings, model_request_parameters

		with (
			patch("pydantic_ai.models.ALLOW_MODEL_REQUESTS", True),
			patch.object(model.client.responses, "create", create),
			patch.object(model, "prepare_request", passthrough_prepare_request),
		):
			asyncio.run(
				model.request(
					[ModelRequest.user_text_prompt("Say something")],
					{"openai_store": True, "temperature": 0.2},
					ModelRequestParameters(),
				)
			)
		asyncio.run(model.client.close())

		kwargs = create.await_args.kwargs
		# The merged model profile alone (not the sanitized settings override)
		# forces streaming and store=false past disagreeing runtime settings.
		self.assertIs(kwargs["stream"], True)
		self.assertIs(kwargs["store"], False)
		self.assertIs(kwargs["temperature"], OMIT)

	def test_request_stream_remains_native_streaming_response(self):
		_, model = build_codex_transport_model()
		stream = FakeEventStream(codex_stream_events())
		create = AsyncMock(return_value=stream)

		async def consume():
			async with model.request_stream(
				[ModelRequest.user_text_prompt("Say something")],
				None,
				ModelRequestParameters(),
			) as streamed:
				events = [event async for event in streamed]
				return streamed, events

		with (
			patch("pydantic_ai.models.ALLOW_MODEL_REQUESTS", True),
			patch.object(model.client.responses, "create", create),
		):
			streamed, events = asyncio.run(consume())
		asyncio.run(model.client.close())

		create.assert_awaited_once()
		kwargs = create.await_args.kwargs
		self.assertIs(kwargs["stream"], True)
		self.assertIs(kwargs["store"], False)
		self.assertIsInstance(kwargs["input"], list)

		# Native stream object: per-delta events flow through instead of one
		# aggregated model response.
		self.assertIsInstance(streamed, OpenAIResponsesStreamedResponse)
		starts = [event for event in events if isinstance(event, PartStartEvent)]
		deltas = [event for event in events if isinstance(event, PartDeltaEvent)]
		self.assertEqual(len(starts), 1)
		self.assertEqual(len(deltas), 1)
		self.assertIsInstance(deltas[0].delta, TextPartDelta)
		self.assertEqual(deltas[0].delta.content_delta, "e Codex stream")
		# The streamed response still resolves to the full aggregated text.
		self.assertEqual(streamed.get().parts[0].content, "Hello from the Codex stream")


class TestCodexTransportStreamErrors(TestCase):
	def test_request_stream_maps_provider_errors_without_exposing_body(self):
		mark_authentication_expired = Mock()
		_, model = build_codex_transport_model(mark_authentication_expired)
		error = ModelHTTPError(429, "gpt-codex-test", {"error": {"message": "provider-secret"}})

		@asynccontextmanager
		async def raising_stream(*args, **kwargs):
			raise error
			yield

		async def run():
			async with model.request_stream([], None, ModelRequestParameters()):
				pass

		with (
			patch.object(OpenAIResponsesModel, "request_stream", raising_stream),
			self.assertRaisesRegex(CodexProviderError, "subscription usage limit reached") as raised,
		):
			asyncio.run(run())

		self.assertNotIn("provider-secret", str(raised.exception))
		mark_authentication_expired.assert_not_called()

	def test_request_stream_reports_expired_authentication(self):
		mark_authentication_expired = Mock()
		_, model = build_codex_transport_model(mark_authentication_expired)
		error = ModelHTTPError(401, "gpt-codex-test", {"error": {"message": "provider-secret"}})

		@asynccontextmanager
		async def raising_stream(*args, **kwargs):
			raise error
			yield

		async def run():
			async with model.request_stream([], None, ModelRequestParameters()):
				pass

		with (
			patch.object(OpenAIResponsesModel, "request_stream", raising_stream),
			self.assertRaisesRegex(CodexProviderError, "ChatGPT authorization expired") as raised,
		):
			asyncio.run(run())

		self.assertNotIn("provider-secret", str(raised.exception))
		mark_authentication_expired.assert_called_once_with()


class TestCodexTransportSemanticPreservation(TestCase):
	"""Round-trip semantics: typed Responses fixtures in, Codex wire kwargs out.

	Each test drives the adapter's public `request()` surface only: a fake
	`responses.create` captures the actual wire kwargs, and upstream typed
	response-event fixtures stand in for the Codex stream. Upstream parser
	internals (delta stitching, JSON-schema rewriting) are not re-tested —
	these tests pin preservation across the transport seam.
	"""

	def run_codex_request(self, model, messages, model_request_parameters=None, events=None):
		"""Run one `model.request()` against a fake Codex stream; return response and create mock."""
		stream = FakeEventStream(events or codex_stream_events())
		create = AsyncMock(return_value=stream)
		with (
			patch("pydantic_ai.models.ALLOW_MODEL_REQUESTS", True),
			patch.object(model.client.responses, "create", create),
		):
			response = asyncio.run(
				model.request(
					messages,
					None,
					model_request_parameters or ModelRequestParameters(),
				)
			)
		asyncio.run(model.client.close())
		return response, create

	def test_structured_json_text_response_round_trips_verbatim(self):
		_, model = build_codex_transport_model()
		payload = '{"city": "Paris", "high_c": 21}'
		response, create = self.run_codex_request(
			model,
			[ModelRequest.user_text_prompt("Weather in Paris?")],
			ModelRequestParameters(
				output_mode="native",
				output_object=OutputObjectDefinition(json_schema=CITY_WEATHER_SCHEMA, name="city_weather"),
			),
			events=codex_stream_events(payload),
		)

		# The structured-output schema reaches the Codex wire as `text.format`.
		text_format = create.await_args.kwargs["text"]["format"]
		self.assertEqual(text_format["type"], "json_schema")
		self.assertEqual(text_format["name"], "city_weather")
		self.assertEqual(text_format["schema"]["properties"], CITY_WEATHER_SCHEMA["properties"])
		self.assertEqual(text_format["schema"]["required"], CITY_WEATHER_SCHEMA["required"])
		# The Codex dialect holds with structured output requested.
		kwargs = create.await_args.kwargs
		self.assertIs(kwargs["stream"], True)
		self.assertIs(kwargs["store"], False)
		self.assertIsInstance(kwargs["input"], list)

		# Stream aggregation preserves the JSON payload verbatim, so the
		# agent-layer parser receives exactly what the backend produced.
		self.assertIsInstance(response.parts[0], TextPart)
		self.assertEqual(response.parts[0].content, payload)
		self.assertEqual(json.loads(response.parts[0].content), {"city": "Paris", "high_c": 21})

	def test_function_tool_call_output_maps_to_tool_call_part(self):
		_, model = build_codex_transport_model()
		response, create = self.run_codex_request(
			model,
			[ModelRequest.user_text_prompt("Weather in Paris?")],
			ModelRequestParameters(function_tools=[WEATHER_TOOL]),
			events=codex_tool_call_events(),
		)

		# The declared tool reaches the wire as a Responses function tool; the
		# schema itself is upstream-normalized, so pin the semantic fields only.
		(tool_definition,) = create.await_args.kwargs["tools"]
		self.assertEqual(tool_definition["name"], "get_weather")
		self.assertEqual(tool_definition["type"], "function")
		self.assertEqual(tool_definition["description"], "Look up current weather.")
		self.assertEqual(tool_definition["parameters"]["properties"], {"city": {"type": "string"}})
		self.assertEqual(tool_definition["parameters"]["required"], ["city"])

		# The streamed function call preserves name, arguments, and call id.
		(tool_call,) = response.parts
		self.assertIsInstance(tool_call, ToolCallPart)
		self.assertEqual(tool_call.tool_name, "get_weather")
		self.assertEqual(tool_call.args_as_dict(), {"city": "Paris"})
		self.assertEqual(tool_call.tool_call_id, "call_1")

	def test_tool_result_in_subsequent_request_maps_function_call_output(self):
		_, model = build_codex_transport_model()
		tool_call_turn, _ = self.run_codex_request(
			model,
			[ModelRequest.user_text_prompt("Weather in Paris?")],
			ModelRequestParameters(function_tools=[WEATHER_TOOL]),
			events=codex_tool_call_events(),
		)

		_, create = self.run_codex_request(
			model,
			[
				ModelRequest.user_text_prompt("Weather in Paris?"),
				tool_call_turn,
				ModelRequest(
					parts=[
						ToolReturnPart(
							tool_name="get_weather",
							content='{"temp_c": 18}',
							tool_call_id="call_1",
						)
					]
				),
			],
			ModelRequestParameters(function_tools=[WEATHER_TOOL]),
		)

		kwargs = create.await_args.kwargs
		# `store=false` backends keep no server-side state: the tool turn
		# re-sends the full history as list input, never a response-id chain.
		self.assertIsInstance(kwargs["input"], list)
		self.assertIs(kwargs["previous_response_id"], OMIT)
		self.assertIs(kwargs["conversation"], OMIT)
		self.assertEqual(
			kwargs["input"],
			[
				{"role": "user", "content": "Weather in Paris?"},
				{
					"type": "function_call",
					"call_id": "call_1",
					"name": "get_weather",
					"arguments": '{"city": "Paris"}',
				},
				{
					"type": "function_call_output",
					"call_id": "call_1",
					"output": '{"temp_c": 18}',
				},
			],
		)

	def test_multi_turn_history_preserved_in_mapped_list_input(self):
		_, model = build_codex_transport_model()
		first_turn, _ = self.run_codex_request(
			model,
			[ModelRequest.user_text_prompt("Weather in Paris?")],
			events=codex_stream_events("Checking Paris."),
		)

		_, create = self.run_codex_request(
			model,
			[
				ModelRequest(
					parts=[
						SystemPromptPart("You are terse."),
						UserPromptPart("Weather in Paris?"),
					]
				),
				first_turn,
				# Content that is already a list keeps its item sequence.
				ModelRequest(parts=[UserPromptPart(content=["Now ", TextContent("Rome?")])]),
			],
		)

		kwargs = create.await_args.kwargs
		self.assertIs(kwargs["previous_response_id"], OMIT)
		self.assertIs(kwargs["conversation"], OMIT)
		# Every history item survives into the mapped list input, in order.
		self.assertEqual(
			kwargs["input"],
			[
				{"role": "system", "content": "You are terse."},
				{"role": "user", "content": "Weather in Paris?"},
				{"role": "assistant", "content": "Checking Paris."},
				{
					"role": "user",
					"content": [
						{"type": "input_text", "text": "Now "},
						{"type": "input_text", "text": "Rome?"},
					],
				},
			],
		)

	def test_reasoning_and_encrypted_reasoning_carry_into_subsequent_input(self):
		_, model = build_codex_transport_model(model_id="gpt-5.3-codex")
		reasoning_turn, _ = self.run_codex_request(
			model,
			[ModelRequest.user_text_prompt("Paris or Rome?")],
			events=codex_reasoning_then_text_events(),
		)

		# The stream's encrypted reasoning survives aggregation as thinking state.
		thinking, text = reasoning_turn.parts
		self.assertIsInstance(thinking, ThinkingPart)
		self.assertEqual(thinking.content, "Weighing night trains.")
		self.assertEqual(thinking.signature, "enc-codex-payload")
		self.assertIsInstance(text, TextPart)
		self.assertEqual(text.content, "Night train it is.")

		_, create = self.run_codex_request(
			model,
			[ModelRequest.user_text_prompt("Paris or Rome?"), reasoning_turn],
		)

		kwargs = create.await_args.kwargs
		# Encrypted reasoning is requested so it can be carried on the next turn.
		self.assertIn("reasoning.encrypted_content", kwargs["include"])
		# The current user prompt maps first, then the reasoning item replays
		# ahead of the assistant message with the encrypted payload and summary
		# text preserved.
		self.assertEqual(
			kwargs["input"],
			[
				{"role": "user", "content": "Paris or Rome?"},
				{
					"type": "reasoning",
					"id": "rs_1",
					"summary": [{"text": "Weighing night trains.", "type": "summary_text"}],
					"encrypted_content": "enc-codex-payload",
				},
				{
					"role": "assistant",
					"id": "msg_1",
					"content": [{"text": "Night train it is.", "type": "output_text", "annotations": []}],
					"type": "message",
					"status": "completed",
				},
			],
		)

	def test_sanitized_errors_never_leak_prompt_account_token_or_body(self):
		_, model = build_codex_transport_model()
		poisoned_body = {
			"error": {
				"message": (
					"prompt=SECRET-PROMPT account-id=account-123 token=oauth-access-token raw provider body"
				),
				"code": "model_not_enabled",
			}
		}

		async def raising_request(*args, **kwargs):
			raise ModelHTTPError(400, "gpt-codex-test", poisoned_body)

		async def run():
			await model.request(
				[ModelRequest.user_text_prompt("SECRET-PROMPT")],
				None,
				ModelRequestParameters(),
			)

		with (
			patch("pydantic_ai.models.ALLOW_MODEL_REQUESTS", True),
			patch.object(OpenAIResponsesModel, "request", raising_request),
			self.assertRaises(CodexProviderError) as raised,
		):
			asyncio.run(run())

		# A translated, actionable message with no provider payload attached.
		self.assertEqual(str(raised.exception), "Model is not enabled for this subscription.")
		for secret in (
			"SECRET-PROMPT",
			"account-123",
			"oauth-access-token",
			"model_not_enabled",
			"provider body",
		):
			self.assertNotIn(secret, str(raised.exception))
		# `raise ... from None` also drops the poisoned HTTP error context.
		self.assertIsNone(raised.exception.__cause__)
		self.assertTrue(raised.exception.__suppress_context__)
