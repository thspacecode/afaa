# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

import frappe
from frappe import _
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from afaa.ai.runtime import resolve_ai_agent


class ExternalRuntimeModel(BaseModel):
	"""Server-to-server model configuration for an external AFAA runtime."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	provider_type: Literal["openai", "google"] = Field(alias="providerType")
	model_id: str = Field(alias="modelId", min_length=1, max_length=255)
	settings: dict[str, Any]
	timeout: float = Field(ge=1, le=3600)
	retries: int = Field(ge=0, le=10)
	api_key: SecretStr = Field(alias="apiKey")


class ExternalRuntimeConfig(BaseModel):
	"""Versioned contract consumed only by trusted external runtime services."""

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


def resolve_external_runtime(agent_name: str, context=None) -> ExternalRuntimeConfig:
	"""Resolve one enabled AFAA agent for execution outside the Frappe process."""
	resolved = resolve_ai_agent(agent_name, context)
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

	instructions = tuple(
		value.strip()
		for value in (resolved.prompt, *(skill.instructions for skill in resolved.skills))
		if value and value.strip()
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
	fingerprint = hashlib.sha256(
		json.dumps(safe_configuration, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
	).hexdigest()
	return ExternalRuntimeConfig.model_validate(
		{
			**safe_configuration,
			"model": {**safe_configuration["model"], "apiKey": api_key},
			"configurationFingerprint": fingerprint,
		}
	)
