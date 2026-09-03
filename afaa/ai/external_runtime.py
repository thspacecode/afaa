# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

import frappe
from frappe import _
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from afaa.ai.runtime import resolve_ai_agent


class ExternalRuntimeModel(BaseModel):
	"""Server-to-server model configuration for a schema-v1 external runtime."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	provider_type: Literal["openai", "google"] = Field(alias="providerType")
	model_id: str = Field(alias="modelId", min_length=1, max_length=255)
	settings: dict[str, Any]
	timeout: float = Field(ge=1, le=3600)
	retries: int = Field(ge=0, le=10)
	api_key: SecretStr = Field(alias="apiKey")


class ExternalRuntimeConfig(BaseModel):
	"""Versioned schema-v1 contract consumed by trusted external runtime services."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
	agent_id: str = Field(alias="agentId", pattern=r"^afaa:[a-z0-9][a-z0-9_-]{0,139}$")
	name: str = Field(min_length=1, max_length=140)
	instructions: tuple[str, ...] = Field(max_length=100)
	model: ExternalRuntimeModel
	configuration_fingerprint: str = Field(alias="configurationFingerprint", pattern=r"^[0-9a-f]{64}$")

	def private_payload(self) -> dict[str, Any]:
		"""Serialize for a trusted machine caller, including the provider secret."""
		payload = self.model_dump(mode="json", by_alias=True)
		payload["model"]["apiKey"] = self.model.api_key.get_secret_value()
		return payload


class CodexExternalRuntimeModel(BaseModel):
	"""Non-secret model configuration used by a downstream credential broker."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, strict=True)

	provider_type: Literal["openai_codex"] = Field(alias="providerType")
	provider_account: str = Field(alias="providerAccount", min_length=1, max_length=140)
	account_id: str = Field(alias="accountId", min_length=1, max_length=255)
	model_id: str = Field(alias="modelId", min_length=1, max_length=255)
	settings: dict[str, Any]
	timeout: float = Field(ge=1, le=3600)
	retries: int = Field(ge=0, le=10)

	@field_validator("provider_account", "account_id")
	@classmethod
	def reject_control_characters(cls, value: str) -> str:
		if any(ord(character) < 32 or ord(character) == 127 for character in value):
			raise ValueError("control characters are not allowed")
		return value


class CodexExternalRuntimeDescriptor(BaseModel):
	"""Internal AFAA descriptor from which Porch may issue a leased runtime DTO."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, strict=True)

	agent_id: str = Field(alias="agentId", pattern=r"^afaa:[a-z0-9][a-z0-9_-]{0,139}$")
	name: str = Field(min_length=1, max_length=140)
	instructions: tuple[str, ...] = Field(max_length=100)
	model: CodexExternalRuntimeModel
	configuration_fingerprint: str = Field(alias="configurationFingerprint", pattern=r"^[0-9a-f]{64}$")


# Compatibility alias for callers that group both return variants as configurations.
CodexExternalRuntimeConfig = CodexExternalRuntimeDescriptor
ExternalRuntime = ExternalRuntimeConfig | CodexExternalRuntimeDescriptor


def resolve_external_runtime(agent_name: str, context=None) -> ExternalRuntime:
	"""Resolve one enabled AFAA agent for execution outside the Frappe process."""
	resolved = resolve_ai_agent(agent_name, context)
	instructions = tuple(
		value.strip()
		for value in (resolved.prompt, *(skill.instructions for skill in resolved.skills))
		if value and value.strip()
	)

	if resolved.model.provider_type == "openai_codex":
		return resolve_codex_external_runtime(resolved, instructions)
	if resolved.model.provider_type not in {"openai", "google"}:
		frappe.throw(
			_("AI provider type {0} is not supported by external runtimes.").format(
				frappe.bold(resolved.model.provider_type)
			),
			frappe.ValidationError,
		)

	account = frappe.get_doc("AI Provider Account", resolved.model.provider_account)
	api_key = account.get_password("api_key", raise_exception=False)
	if not api_key:
		frappe.throw(
			_("AI Provider Account {0} has no API key.").format(frappe.bold(account.name)),
			frappe.ValidationError,
		)

	safe_configuration = {
		"schemaVersion": 1,
		"agentId": f"afaa:{resolved.key}",
		"name": resolved.name,
		"instructions": instructions,
		"model": {
			"providerType": resolved.model.provider_type,
			"modelId": resolved.model.model_id,
			"settings": resolved.model.settings,
			"timeout": resolved.timeout,
			"retries": resolved.retries,
		},
	}
	return ExternalRuntimeConfig.model_validate(
		{
			**safe_configuration,
			"model": {**safe_configuration["model"], "apiKey": api_key},
			"configurationFingerprint": configuration_fingerprint(safe_configuration),
		}
	)


def resolve_codex_external_runtime(resolved, instructions: tuple[str, ...]) -> CodexExternalRuntimeDescriptor:
	"""Build a credential-free Codex descriptor without reading Token Cache passwords."""
	from afaa.ai.oauth.openai_codex_service import CodexReconnectRequiredError

	account = frappe.get_doc("AI Provider Account", resolved.model.provider_account)
	if account.disabled or account.oauth_status != "Connected" or not account.connected_user:
		raise CodexReconnectRequiredError(_("ChatGPT authorization expired; reconnect account.")) from None

	account_id = (account.external_account_id or "").strip()
	if not account_id:
		raise CodexReconnectRequiredError(_("ChatGPT authorization expired; reconnect account.")) from None

	safe_configuration = {
		"agentId": f"afaa:{resolved.key}",
		"name": resolved.name,
		"instructions": instructions,
		"model": {
			"providerType": "openai_codex",
			"providerAccount": account.name,
			"accountId": account_id,
			"modelId": resolved.model.model_id,
			"settings": {**resolved.model.settings, "openai_store": False},
			"timeout": resolved.timeout,
			"retries": resolved.retries,
		},
	}
	return CodexExternalRuntimeDescriptor.model_validate(
		{
			**safe_configuration,
			"configurationFingerprint": configuration_fingerprint(safe_configuration),
		}
	)


def configuration_fingerprint(configuration: dict[str, Any]) -> str:
	return hashlib.sha256(
		json.dumps(configuration, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
	).hexdigest()


def notify_provider_account_invalidated(provider_account: str) -> None:
	"""Best-effort hook for downstream brokers holding account-scoped credentials."""
	for hook_path in frappe.get_hooks("afaa_provider_account_invalidated"):
		try:
			frappe.get_attr(hook_path)(provider_account)
		except Exception as error:
			frappe.log_error(
				title=_("AFAA provider account invalidation hook failed"),
				message=f"Hook {hook_path} failed ({type(error).__name__})",
			)
