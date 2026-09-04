# Copyright (c) 2026, SpaceCode and Contributors
# See license.txt

import asyncio
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, Mock, patch

import frappe
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIResponsesModel

from afaa.ai.provider import sync_provider_models
from afaa.ai.provider.openai_codex_provider import CODEX_BASE_URL, OpenAICodexProvider
from afaa.ai.provider.openai_codex_transport import (
	CodexProviderError,
	raise_codex_provider_error,
)


class TestOpenAICodexProvider(TestCase):
	def test_codex_http_error_mapping(self):
		authentication_expired = Mock()
		cases = [
			(401, "ChatGPT authorization expired; reconnect account"),
			(403, "Account or workspace is not authorized for Codex"),
			(429, "ChatGPT/Codex subscription usage limit reached"),
			(404, "Model is not enabled for this subscription"),
		]

		for status_code, message in cases:
			with (
				self.subTest(status_code=status_code),
				self.assertRaisesRegex(CodexProviderError, message),
			):
				raise_codex_provider_error(status_code, authentication_expired)

		authentication_expired.assert_called_once_with()

	def test_builds_responses_model_with_codex_transport(self):
		account = SimpleNamespace(external_account_id="account-123")
		provider = OpenAICodexProvider(account)

		with patch.object(provider, "resolve_access_token", return_value="oauth-access-token") as resolve:
			model = provider.build_model(SimpleNamespace(model_id="gpt-codex-test"))

			async def refresh_token_and_close_client():
				await model.client._refresh_api_key()
				await model.client.close()

			asyncio.run(refresh_token_and_close_client())

		self.assertIsInstance(model, OpenAIResponsesModel)
		self.assertEqual(model.model_name, "gpt-codex-test")
		self.assertEqual(str(model.client.base_url), f"{CODEX_BASE_URL}/")
		self.assertEqual(model.client.default_headers["chatgpt-account-id"], "account-123")
		self.assertEqual(model.client.default_headers["originator"], "afaa")
		self.assertEqual(model.client.default_headers["OpenAI-Beta"], "responses=experimental")
		self.assertEqual(model.client.api_key, "oauth-access-token")
		self.assertEqual(resolve.call_count, 2)

	def test_store_cannot_be_enabled_by_runtime_overrides(self):
		account = SimpleNamespace(external_account_id="account-123")
		provider = OpenAICodexProvider(account)

		with patch.object(provider, "resolve_access_token", return_value="oauth-access-token"):
			model = provider.build_model(SimpleNamespace(model_id="gpt-codex-test"))

		settings, _ = model.prepare_request(
			{"openai_store": True, "temperature": 0.2},
			ModelRequestParameters(),
		)
		asyncio.run(model.client.close())

		self.assertFalse(settings["openai_store"])
		self.assertEqual(settings["temperature"], 0.2)
		self.assertEqual(
			provider.prepare_model_settings({"openai_store": True, "max_tokens": 100}),
			{"openai_store": False, "max_tokens": 100},
		)

	def test_missing_account_id_is_rejected_before_client_construction(self):
		provider = OpenAICodexProvider(SimpleNamespace(external_account_id=None))

		with self.assertRaisesRegex(frappe.ValidationError, "Unable to determine ChatGPT account"):
			provider.build_model(SimpleNamespace(model_id="gpt-codex-test"))

	def test_model_request_maps_provider_errors_without_exposing_body(self):
		account = SimpleNamespace(external_account_id="account-123")
		provider = OpenAICodexProvider(account)
		provider.mark_authentication_expired = Mock()

		with patch.object(provider, "resolve_access_token", return_value="oauth-access-token"):
			model = provider.build_model(SimpleNamespace(model_id="gpt-codex-test"))

		error = ModelHTTPError(
			429,
			"gpt-codex-test",
			{"error": {"message": "provider-secret"}},
		)
		with (
			patch.object(OpenAIResponsesModel, "request", new=AsyncMock(side_effect=error)),
			self.assertRaisesRegex(CodexProviderError, "subscription usage limit reached") as raised,
		):
			asyncio.run(model.request([], None, ModelRequestParameters()))
		asyncio.run(model.client.close())

		self.assertNotIn("provider-secret", str(raised.exception))
		provider.mark_authentication_expired.assert_not_called()

	def test_lists_only_visible_api_models_from_codex_catalog(self):
		provider = OpenAICodexProvider(SimpleNamespace(external_account_id="account-123"))
		response = Mock(
			ok=True,
			status_code=200,
		)
		response.json.return_value = {
			"models": [
				{"slug": " gpt-codex-visible ", "supported_in_api": True, "visibility": "list"},
				{"slug": "gpt-codex-hidden", "supported_in_api": True, "visibility": "hide"},
				{"slug": "gpt-codex-internal", "supported_in_api": False, "visibility": "list"},
				{"slug": "", "supported_in_api": True, "visibility": "list"},
				{"supported_in_api": True, "visibility": "list"},
				"malformed",
			]
		}

		with (
			patch.object(provider, "resolve_access_token", return_value="oauth-access-token"),
			patch("afaa.ai.provider.openai_codex_provider.requests.get", return_value=response) as get,
		):
			models = provider.list_models()

		self.assertEqual(models, ["gpt-codex-visible"])
		get.assert_called_once_with(
			f"{CODEX_BASE_URL}/models",
			params={"client_version": "0.153.2"},
			headers={
				"Authorization": "Bearer oauth-access-token",
				"chatgpt-account-id": "account-123",
				"originator": "afaa",
			},
			timeout=30,
		)
		self.assertNotIn("/v1/models", get.call_args.args[0])

	def test_catalog_maps_subscription_http_errors(self):
		provider = OpenAICodexProvider(SimpleNamespace(external_account_id="account-123"))
		provider.mark_authentication_expired = Mock()
		response = Mock(ok=False, status_code=403)

		with (
			patch.object(provider, "resolve_access_token", return_value="oauth-access-token"),
			patch("afaa.ai.provider.openai_codex_provider.requests.get", return_value=response),
			self.assertRaisesRegex(CodexProviderError, "not authorized for Codex"),
		):
			provider.list_models()

		provider.mark_authentication_expired.assert_not_called()

	def test_rejects_invalid_codex_catalog_without_exposing_response_body(self):
		provider = OpenAICodexProvider(SimpleNamespace(external_account_id="account-123"))
		response = Mock(ok=True, status_code=200)
		response.json.return_value = {"models": "not-a-list", "secret": "provider-secret"}

		with (
			patch.object(provider, "resolve_access_token", return_value="oauth-access-token"),
			patch("afaa.ai.provider.openai_codex_provider.requests.get", return_value=response),
			self.assertRaisesRegex(frappe.ValidationError, "invalid model catalog") as error,
		):
			provider.list_models()

		self.assertNotIn("provider-secret", str(error.exception))

	def test_codex_sync_does_not_globally_disable_models_missing_from_one_account(self):
		account = SimpleNamespace(provider="openai_codex")
		existing_model = SimpleNamespace(model_id="account-specific-model", name="codex:model")

		with (
			patch("afaa.ai.provider.list_provider_models", return_value=[]),
			patch(
				"afaa.ai.provider.frappe.get_doc",
				return_value=SimpleNamespace(provider_type="openai_codex"),
			),
			patch("afaa.ai.provider.frappe.get_all", return_value=[existing_model]),
			patch("afaa.ai.provider.frappe.db.set_value") as set_value,
		):
			report = sync_provider_models(account)

		self.assertEqual(report, {"created": [], "updated": [], "unavailable": []})
		set_value.assert_not_called()
