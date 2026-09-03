# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

"""Opt-in live feasibility spike for ChatGPT/Codex subscription OAuth.

Run only from a trusted shell with an account whose policy eligibility has been
reviewed. The utility keeps credentials in memory, never returns them, and revokes the
refresh token before exiting.
"""

import asyncio
import os
import time
from typing import Any

import frappe
from openai import AsyncOpenAI, AuthenticationError, RateLimitError
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider as PydanticOpenAIProvider

from afaa.ai.oauth.openai_codex import (
	DEFAULT_CLIENT_ID,
	OAuthTokens,
	OpenAICodexOAuthClient,
	decode_chatgpt_claims,
)

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"


class SpikeStructuredOutput(BaseModel):
	answer: int
	label: str


def run_phase0_spike(
	model_id: str | None = None,
	*,
	acknowledge_policy_risk: bool = False,
	poll_timeout: int = 15 * 60,
) -> dict[str, Any]:
	"""Run the destructive live spike and return only non-secret verification results.

	Invoke with ``AFAA_CODEX_LIVE_TEST=1`` and ``acknowledge_policy_risk=true``.
	This is intentionally not whitelisted and does not write Token Cache records.
	"""
	if os.environ.get("AFAA_CODEX_LIVE_TEST") != "1":
		raise frappe.ValidationError("Set AFAA_CODEX_LIVE_TEST=1 to run the live Codex spike.")
	if not acknowledge_policy_risk:
		raise frappe.ValidationError(
			"Explicitly acknowledge that public client availability is not service authorization."
		)
	model_id = model_id or frappe.conf.get("afaa_openai_codex_spike_model")
	if not model_id or not isinstance(model_id, str):
		raise frappe.ValidationError("Pass model_id or configure afaa_openai_codex_spike_model.")

	client_id = frappe.conf.get("afaa_openai_codex_client_id") or DEFAULT_CLIENT_ID
	oauth = OpenAICodexOAuthClient(client_id=client_id)
	device = oauth.request_device_authorization()
	print(f"Open {device.verification_url}")
	print(f"Enter one-time code: {device.user_code}")
	print("No OAuth token will be printed or persisted by this spike.")

	tokens: OAuthTokens | None = None
	refresh_token_to_revoke: str | None = None
	revoked = False
	try:
		deadline = time.monotonic() + min(max(poll_timeout, 1), device.expires_in)
		grant = None
		while grant is None and time.monotonic() < deadline:
			grant = oauth.poll_device_authorization(device)
			if grant is None:
				time.sleep(device.interval)
		if grant is None:
			raise frappe.ValidationError("Codex device authorization expired before completion.")

		tokens = oauth.exchange_authorization_code(grant)
		refresh_token_to_revoke = tokens.refresh_token
		claims = decode_chatgpt_claims(tokens.id_token)

		refreshed = oauth.refresh(tokens.refresh_token, previous_id_token=tokens.id_token)
		refresh_token_to_revoke = refreshed.refresh_token
		refreshed_claims = decode_chatgpt_claims(refreshed.id_token)
		if refreshed_claims.account_id != claims.account_id:
			raise frappe.ValidationError("The refreshed Codex token changed ChatGPT account identity.")

		proof = asyncio.run(
			run_pydantic_ai_proof(
				access_token=refreshed.access_token,
				account_id=claims.account_id,
				model_id=model_id,
			)
		)
		oauth.revoke(refreshed.refresh_token)
		revoked = True
		return {
			"device_authorization": "complete",
			"token_exchange": "complete",
			"metadata": {
				"account_id_present": bool(claims.account_id),
				"email_present": bool(claims.email),
				"plan_type": claims.plan_type,
			},
			"refresh": {
				"complete": True,
				"rotation_observed": refreshed.refresh_token != tokens.refresh_token,
			},
			"revocation": "complete",
			"pydantic_ai": proof,
			"policy_gate": {
				"status": "manual_confirmation_required",
				"items": [
					"public OAuth client/device flow use",
					"originator: afaa acceptance",
					"server-side execution for intended account types",
					"subscription sharing between Frappe users",
				],
			},
		}
	finally:
		if refresh_token_to_revoke and not revoked:
			oauth.revoke(refresh_token_to_revoke)


async def run_pydantic_ai_proof(*, access_token: str, account_id: str, model_id: str) -> dict[str, Any]:
	client = build_codex_client(access_token=access_token, account_id=account_id)
	settings = {
		"openai_store": False,
		"openai_reasoning_effort": "low",
		"openai_reasoning_summary": "auto",
	}
	rate_limit_observed = False
	try:
		model = OpenAIResponsesModel(
			model_id,
			provider=PydanticOpenAIProvider(openai_client=client),
		)

		try:
			plain = await Agent(model=model, model_settings=settings).run(
				"Reply with one short sentence confirming plain text output."
			)

			structured = await Agent(
				model=model,
				output_type=SpikeStructuredOutput,
				model_settings=settings,
			).run("Return answer=42 and label='codex-phase-zero'.")

			tool_calls: list[tuple[int, int]] = []

			def multiply(a: int, b: int) -> int:
				tool_calls.append((a, b))
				return a * b

			tool_agent = Agent(model=model, tools=[multiply], model_settings=settings)
			tool_result = await tool_agent.run("You must call multiply with 6 and 7, then report its result.")

			first_turn = await Agent(model=model, model_settings=settings).run(
				"Remember the marker cobalt-17 and briefly explain why primes are useful."
			)
			second_turn = await Agent(model=model, model_settings=settings).run(
				"What marker did I ask you to remember?",
				message_history=first_turn.all_messages(),
			)
		except RateLimitError:
			rate_limit_observed = True
			raise frappe.ValidationError("Codex subscription usage limit was observed during the spike.")

		authentication_error_status = await verify_authentication_error(
			account_id=account_id,
			model_id=model_id,
		)
		messages = [message.model_dump(mode="json") for message in first_turn.all_messages()]
		return {
			"model_class": type(model).__name__,
			"plain_text": isinstance(plain.output, str) and bool(plain.output.strip()),
			"structured_output": structured.output
			== SpikeStructuredOutput(answer=42, label="codex-phase-zero"),
			"tool_call": bool(tool_calls) and tool_result.usage.tool_calls > 0,
			"multi_turn": "cobalt-17" in str(second_turn.output).lower(),
			"encrypted_reasoning_observed": contains_encrypted_reasoning(messages),
			"authentication_error_status": authentication_error_status,
			"usage_limit_error": "observed" if rate_limit_observed else "not_observed",
			"usage_limit_note": "A live 429 is verified only when the subscription is actually limited.",
		}
	finally:
		await client.close()


def build_codex_client(*, access_token: str, account_id: str) -> AsyncOpenAI:
	return AsyncOpenAI(
		api_key=access_token,
		base_url=CODEX_BASE_URL,
		default_headers={
			"chatgpt-account-id": account_id,
			"originator": "afaa",
			"OpenAI-Beta": "responses=experimental",
		},
	)


async def verify_authentication_error(*, account_id: str, model_id: str) -> int:
	client = build_codex_client(access_token="afaa-intentionally-invalid", account_id=account_id)
	try:
		await client.responses.create(model=model_id, input="authentication probe", store=False)
	except AuthenticationError as error:
		return error.status_code
	finally:
		await client.close()
	raise frappe.ValidationError("The Codex endpoint unexpectedly accepted an invalid bearer token.")


def contains_encrypted_reasoning(value: Any) -> bool:
	if isinstance(value, dict):
		if value.get("signature") or value.get("encrypted_content"):
			return True
		return any(contains_encrypted_reasoning(item) for item in value.values())
	if isinstance(value, list):
		return any(contains_encrypted_reasoning(item) for item in value)
	return False
