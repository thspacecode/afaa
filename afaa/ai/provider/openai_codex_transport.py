# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

"""Pydantic AI transport constrained for the ChatGPT Codex backend."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import frappe
from frappe import _
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models import ModelMessage, ModelRequestParameters, ModelResponse, StreamedResponse
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.settings import ModelSettings


class CodexProviderError(frappe.ValidationError):
	"""A sanitized, user-facing Codex request error."""


class OpenAICodexResponsesModel(OpenAIResponsesModel):
	"""Responses model that prevents storage and sanitizes subscription failures."""

	def __init__(self, *args, on_authentication_expired: Callable[[], None] | None = None, **kwargs):
		super().__init__(*args, **kwargs)
		self.on_authentication_expired = on_authentication_expired

	def prepare_request(
		self,
		model_settings: ModelSettings | None,
		model_request_parameters: ModelRequestParameters,
	) -> tuple[ModelSettings | None, ModelRequestParameters]:
		settings, request_parameters = super().prepare_request(
			model_settings,
			model_request_parameters,
		)
		settings = {**(settings or {}), "openai_store": False}
		return settings, request_parameters

	async def request(
		self,
		messages: list[ModelMessage],
		model_settings: ModelSettings | None,
		model_request_parameters: ModelRequestParameters,
	) -> ModelResponse:
		try:
			return await super().request(messages, model_settings, model_request_parameters)
		except ModelHTTPError as error:
			raise_codex_provider_error(error, self.on_authentication_expired)

	@asynccontextmanager
	async def request_stream(
		self,
		messages: list[ModelMessage],
		model_settings: ModelSettings | None,
		model_request_parameters: ModelRequestParameters,
		run_context: RunContext[Any] | None = None,
	) -> AsyncIterator[StreamedResponse]:
		try:
			async with super().request_stream(
				messages,
				model_settings,
				model_request_parameters,
				run_context,
			) as response:
				yield response
		except ModelHTTPError as error:
			raise_codex_provider_error(error, self.on_authentication_expired)


def force_codex_model_settings(settings: dict[str, Any]) -> dict[str, Any]:
	"""Return runtime settings with immutable Codex privacy constraints applied."""
	return {**settings, "openai_store": False}


def raise_codex_provider_error(
	error: ModelHTTPError | int,
	on_authentication_expired: Callable[[], None] | None = None,
) -> None:
	"""Raise a translated Codex error without exposing the provider response body."""
	status_code = error.status_code if isinstance(error, ModelHTTPError) else error
	if status_code == 401:
		if on_authentication_expired:
			on_authentication_expired()
		message = _("ChatGPT authorization expired; reconnect account.")
	elif status_code == 403:
		message = _("Account or workspace is not authorized for Codex.")
	elif status_code == 429:
		message = _("ChatGPT/Codex subscription usage limit reached.")
	elif _is_model_unavailable(error):
		message = _("Model is not enabled for this subscription.")
	else:
		message = _("OpenAI Codex request failed (HTTP {0}).").format(status_code)
	raise CodexProviderError(message) from None


def _is_model_unavailable(error: ModelHTTPError | int) -> bool:
	if isinstance(error, int):
		return error == 404
	if error.status_code == 404 or error.suggested_model_id:
		return True
	if not isinstance(error.body, dict):
		return False
	provider_error = error.body.get("error")
	code = provider_error.get("code") if isinstance(provider_error, dict) else error.body.get("code")
	return code in {"model_not_found", "model_not_enabled", "unsupported_model"}
