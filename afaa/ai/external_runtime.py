# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

import frappe
from frappe import _
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from afaa.ai.runtime import resolve_ai_agent
from afaa.ai.skill_bundles import SkillBundleReference
from afaa.ai.tools import EXTERNAL_READ_TOOL_METHODS, get_tool_definition


class ExternalRuntimeSkill(BaseModel):
	"""Credential-free skill capability data safe to pin on an external thread."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,139}$")
	name: str = Field(min_length=1, max_length=140)
	description: str | None = None
	instructions: str = Field(min_length=1)
	required_tools: tuple[str, ...] = Field(alias="requiredTools", max_length=100)
	fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

	@model_validator(mode="after")
	def validate_fingerprint(self) -> ExternalRuntimeSkill:
		bundle = getattr(self, "bundle", None)
		expected = external_skill_fingerprint(
			key=self.key,
			name=self.name,
			description=self.description,
			instructions=self.instructions,
			required_tools=self.required_tools,
			bundle=bundle,
		)
		if self.fingerprint != expected:
			raise ValueError("skill fingerprint does not match its content")
		return self


class BundleExternalRuntimeSkill(ExternalRuntimeSkill):
	"""Bundle-aware skill metadata safe to pin without raw file bodies."""

	bundle: SkillBundleReference


class ExternalRuntimeTool(BaseModel):
	"""Public schema for one approved tool; the registered Python method is intentionally omitted."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,139}$")
	name: str = Field(min_length=1, max_length=140)
	description: str = Field(min_length=1)
	input_schema: dict[str, Any] = Field(alias="inputSchema")
	output_schema: dict[str, Any] = Field(alias="outputSchema")


class ExternalRuntimeModel(BaseModel):
	"""Server-to-server model configuration for a schema-v1 external runtime."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	provider_type: Literal["openai", "google", "zai", "moonshot"] = Field(alias="providerType")
	model_id: str = Field(alias="modelId", min_length=1, max_length=255)
	settings: dict[str, Any]
	timeout: float = Field(ge=1, le=3600)
	retries: int = Field(ge=0, le=10)
	api_key: SecretStr = Field(alias="apiKey")


class ExternalRuntimeConfig(BaseModel):
	"""Legacy schema-v1 contract consumed by trusted external runtime services."""

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


class StructuredExternalRuntimeConfig(ExternalRuntimeConfig):
	"""External runtime contract with deferred skills and approved tool schemas."""

	skills: tuple[ExternalRuntimeSkill, ...] = Field(max_length=100)
	tools: tuple[ExternalRuntimeTool, ...] = Field(max_length=100)


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
	"""Legacy internal AFAA descriptor from which Porch may issue a leased runtime DTO."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, strict=True)

	agent_id: str = Field(alias="agentId", pattern=r"^afaa:[a-z0-9][a-z0-9_-]{0,139}$")
	name: str = Field(min_length=1, max_length=140)
	instructions: tuple[str, ...] = Field(max_length=100)
	model: CodexExternalRuntimeModel
	configuration_fingerprint: str = Field(alias="configurationFingerprint", pattern=r"^[0-9a-f]{64}$")


class StructuredCodexExternalRuntimeDescriptor(CodexExternalRuntimeDescriptor):
	"""Credential-free Codex descriptor with deferred skills and approved tool schemas."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, strict=True)

	skills: tuple[ExternalRuntimeSkill, ...] = Field(max_length=100)
	tools: tuple[ExternalRuntimeTool, ...] = Field(max_length=100)


# Compatibility aliases for callers that group return variants as configurations.
CodexExternalRuntimeConfig = CodexExternalRuntimeDescriptor
ExternalRuntime = (
	ExternalRuntimeConfig
	| StructuredExternalRuntimeConfig
	| CodexExternalRuntimeDescriptor
	| StructuredCodexExternalRuntimeDescriptor
)


def resolve_external_runtime(
	agent_name: str,
	context=None,
	*,
	legacy_skill_instructions: bool = False,
) -> ExternalRuntime:
	"""Resolve one enabled AFAA agent for execution outside the Frappe process.

	Structured skills are the default. Existing threads may explicitly request the
	legacy contract, which flattens skill instructions into the agent instructions.
	"""
	resolved = resolve_ai_agent(agent_name, context)
	if legacy_skill_instructions:
		instructions = _non_empty_instructions(
			resolved.prompt, *(skill.instructions for skill in resolved.skills)
		)
		skills: tuple[ExternalRuntimeSkill, ...] = ()
		tools: tuple[ExternalRuntimeTool, ...] = ()
	else:
		instructions = _non_empty_instructions(resolved.prompt)
		skills, tools = build_structured_runtime_capabilities(resolved)

	if resolved.model.provider_type == "openai_codex":
		return resolve_codex_external_runtime(
			resolved,
			instructions,
			skills=skills,
			tools=tools,
			legacy_skill_instructions=legacy_skill_instructions,
		)
	if resolved.model.provider_type not in {"openai", "google", "zai", "moonshot"}:
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

	safe_configuration = _base_configuration(resolved, instructions)
	config_type: type[ExternalRuntimeConfig]
	if legacy_skill_instructions:
		config_type = ExternalRuntimeConfig
	else:
		safe_configuration.update(_structured_payload(skills, tools))
		config_type = StructuredExternalRuntimeConfig
	return config_type.model_validate(
		{
			**safe_configuration,
			"model": {**safe_configuration["model"], "apiKey": api_key},
			"configurationFingerprint": configuration_fingerprint(safe_configuration),
		}
	)


def resolve_codex_external_runtime(
	resolved,
	instructions: tuple[str, ...],
	*,
	skills: tuple[ExternalRuntimeSkill, ...] = (),
	tools: tuple[ExternalRuntimeTool, ...] = (),
	legacy_skill_instructions: bool = True,
) -> CodexExternalRuntimeDescriptor:
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
	config_type: type[CodexExternalRuntimeDescriptor]
	if legacy_skill_instructions:
		config_type = CodexExternalRuntimeDescriptor
	else:
		safe_configuration.update(_structured_payload(skills, tools))
		config_type = StructuredCodexExternalRuntimeDescriptor
	return config_type.model_validate(
		{
			**safe_configuration,
			"configurationFingerprint": configuration_fingerprint(safe_configuration),
		}
	)


def build_structured_runtime_capabilities(
	resolved,
	*,
	include_bundles: bool = False,
) -> tuple[tuple[ExternalRuntimeSkill, ...], tuple[ExternalRuntimeTool, ...]]:
	"""Build deterministic skill DTOs and approved tools from one resolved agent."""
	tools = []
	for tool in sorted(resolved.tools, key=lambda item: item.key):
		expected_method = EXTERNAL_READ_TOOL_METHODS.get(tool.key)
		if not expected_method:
			continue
		definition = get_tool_definition(tool.key)
		if not definition or definition.key != tool.key or definition.method != expected_method:
			frappe.throw(
				_("AI Tool {0} is not registered as an approved read-only tool.").format(
					frappe.bold(tool.key)
				),
				frappe.ValidationError,
			)
		tools.append(
			ExternalRuntimeTool(
				key=definition.key,
				name=definition.name,
				description=definition.description,
				inputSchema=definition.input_schema,
				outputSchema=definition.output_schema,
			)
		)

	external_tool_keys = {tool.key for tool in tools}
	skills = []
	aggregate_bundle_bytes = 0
	for skill in sorted(resolved.skills, key=lambda item: item.key):
		required_tools = tuple(sorted(set(skill.required_tools)))
		unsupported = set(required_tools) - external_tool_keys
		if unsupported:
			frappe.throw(
				_("AI Skill {0} requires tools unsupported by external runtimes: {1}").format(
					frappe.bold(skill.name), ", ".join(sorted(unsupported))
				),
				frappe.ValidationError,
			)
		snapshot = {
			"key": skill.key,
			"name": skill.name,
			"description": skill.description or None,
			"instructions": skill.instructions,
			"requiredTools": required_tools,
		}
		skill_type: type[ExternalRuntimeSkill] = ExternalRuntimeSkill
		if include_bundles:
			from afaa.ai.skill_bundles import create_skill_bundle_version, get_skill_bundle_limits

			bundle = create_skill_bundle_version(skill.key)
			aggregate_bundle_bytes += bundle.total_bytes
			if aggregate_bundle_bytes > get_skill_bundle_limits().max_agent_bundle_bytes:
				frappe.throw(
					_("Agent bundles exceed afaa_skill_bundle_max_agent_bundle_bytes ({0}).").format(
						get_skill_bundle_limits().max_agent_bundle_bytes
					),
					frappe.ValidationError,
				)
			snapshot["bundle"] = bundle.model_dump(mode="json", by_alias=True)
			skill_type = BundleExternalRuntimeSkill
		skills.append(
			skill_type.model_validate({**snapshot, "fingerprint": configuration_fingerprint(snapshot)})
		)
	return tuple(skills), tuple(tools)


def build_bundle_aware_runtime_capabilities(
	resolved,
) -> tuple[tuple[BundleExternalRuntimeSkill, ...], tuple[ExternalRuntimeTool, ...]]:
	"""Explicit bundle-aware contract used when a caller negotiates bundle snapshots."""
	skills, tools = build_structured_runtime_capabilities(resolved, include_bundles=True)
	return tuple(BundleExternalRuntimeSkill.model_validate(skill) for skill in skills), tools


def _base_configuration(resolved, instructions: tuple[str, ...]) -> dict[str, Any]:
	return {
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


def _structured_payload(
	skills: tuple[ExternalRuntimeSkill, ...], tools: tuple[ExternalRuntimeTool, ...]
) -> dict[str, Any]:
	return {
		"skills": tuple(skill.model_dump(mode="json", by_alias=True) for skill in skills),
		"tools": tuple(tool.model_dump(mode="json", by_alias=True) for tool in tools),
	}


def _non_empty_instructions(*values: str | None) -> tuple[str, ...]:
	return tuple(value.strip() for value in values if value and value.strip())


def external_skill_fingerprint(
	*,
	key: str,
	name: str,
	description: str | None,
	instructions: str,
	required_tools: tuple[str, ...] | list[str],
	bundle: Any | None = None,
	bundle_digest: str | None = None,
) -> str:
	"""Hash exactly the immutable fields carried by a pinned skill DTO."""
	payload = {
		"key": key,
		"name": name,
		"description": description,
		"instructions": instructions,
		"requiredTools": tuple(required_tools),
	}
	if bundle is not None:
		payload["bundle"] = (
			bundle.model_dump(mode="json", by_alias=True) if isinstance(bundle, BaseModel) else bundle
		)
	elif bundle_digest is not None:
		payload["bundleDigest"] = bundle_digest
	return configuration_fingerprint(payload)


def configuration_fingerprint(configuration: dict[str, Any]) -> str:
	"""Hash canonical, credential-free runtime configuration."""
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
