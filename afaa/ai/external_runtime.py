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
	base_url: str | None = Field(default=None, alias="baseUrl", max_length=512)

	@field_validator("base_url")
	@classmethod
	def validate_base_url(cls, value: str | None) -> str | None:
		if value is None:
			return None
		if any(ord(character) < 32 or ord(character) == 127 for character in value):
			raise ValueError("base URL contains control characters")
		if not value.startswith("https://") or len(value.split()) != 1:
			raise ValueError("base URL must be a valid HTTPS URL without whitespace")
		return value


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
		if self.model.base_url is None:
			# Omit the key entirely so runtimes without base-URL support (which
			# forbid unknown model fields) keep accepting this payload.
			payload["model"].pop("baseUrl", None)
		payload["model"]["apiKey"] = self.model.api_key.get_secret_value()
		return payload


class StructuredExternalRuntimeConfig(ExternalRuntimeConfig):
	"""External runtime contract with deferred skills and approved tool schemas."""

	skills: tuple[ExternalRuntimeSkill, ...] = Field(max_length=100)
	tools: tuple[ExternalRuntimeTool, ...] = Field(max_length=100)


class ExternalSubAgentTool(BaseModel):
	"""One tool advertised to a sub-agent, marked runtime-implemented or proxied."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,139}$")
	name: str = Field(min_length=1, max_length=140)
	description: str = Field(min_length=1, max_length=10_000)
	input_schema: dict[str, Any] = Field(alias="inputSchema")
	output_schema: dict[str, Any] = Field(alias="outputSchema")
	runtime: bool = False


class ExternalSubAgentModel(BaseModel):
	"""Credential-free model descriptor for one sub-agent.

	Porch embeds each sub-agent's API key or issues a Codex credential lease;
	afaa never touches secrets here.
	"""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	provider_type: Literal["openai", "google", "zai", "moonshot", "openai_codex"] = Field(
		alias="providerType"
	)
	model_id: str = Field(alias="modelId", min_length=1, max_length=255)
	settings: dict[str, Any]
	timeout: float = Field(ge=1, le=3600)
	retries: int = Field(ge=0, le=10)
	base_url: str | None = Field(default=None, alias="baseUrl", max_length=512)
	provider_account: str | None = Field(default=None, alias="providerAccount", max_length=140)
	account_id: str | None = Field(default=None, alias="accountId", max_length=255)

	@field_validator("base_url")
	@classmethod
	def validate_base_url(cls, value: str | None) -> str | None:
		if value is None:
			return None
		if any(ord(character) < 32 or ord(character) == 127 for character in value):
			raise ValueError("base URL contains control characters")
		if not value.startswith("https://") or len(value.split()) != 1:
			raise ValueError("base URL must be a valid HTTPS URL without whitespace")
		return value

	@model_validator(mode="after")
	def codex_binds_a_provider_account(self) -> ExternalSubAgentModel:
		if self.provider_type == "openai_codex":
			if not self.provider_account or not self.account_id:
				raise ValueError("Codex sub-agents require a provider account binding")
		elif self.provider_account is not None or self.account_id is not None:
			raise ValueError("API-key sub-agents cannot carry provider account bindings")
		return self


class ExternalSubAgent(BaseModel):
	"""One credential-free sub-agent descriptor for a schema-v4 runtime."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	agent_id: str = Field(alias="agentId", pattern=r"^afaa:[a-z0-9][a-z0-9_-]{0,139}$")
	name: str = Field(min_length=1, max_length=140)
	delegate_name: str = Field(alias="delegateName", min_length=1, max_length=140)
	description: str | None = Field(default=None, max_length=10_000)
	instructions: tuple[str, ...] = Field(max_length=100)
	model: ExternalSubAgentModel
	tools: tuple[ExternalSubAgentTool, ...] = Field(max_length=100)
	skills: tuple[ExternalRuntimeSkill, ...] = Field(default=(), max_length=100)
	max_calls: int | None = Field(default=None, alias="maxCalls", ge=1, le=1000)
	timeout_seconds: float | None = Field(default=None, alias="timeoutSeconds", ge=1, le=86400)


class SubAgentAwareExternalRuntimeConfig(StructuredExternalRuntimeConfig):
	"""Schema-v4 API-key runtime contract carrying Level 2 sub-agent descriptors."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	schema_version: Literal[4] = Field(default=4, alias="schemaVersion")
	agent_level: Literal["0", "1", "2"] = Field(default="1", alias="agentLevel")
	sub_agents: tuple[ExternalSubAgent, ...] = Field(alias="subAgents", max_length=10)


class ExternalRuntimeMCPServer(BaseModel):
	"""One resolved (server x account) MCP connection for a schema-v5 runtime.

	Porch and porch-agent see a flat, uniquely-keyed list of connections and
	are unaware of the account layer. The bearer token is a ``SecretStr``: it
	is redacted in public dumps and travels only inside the private
	server-to-server payload.
	"""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	key: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,99}$")
	name: str = Field(min_length=1, max_length=140)
	url: str = Field(min_length=8, max_length=1000)
	transport: Literal["auto", "streamable_http", "sse"] = "auto"
	allowed_tools: tuple[str, ...] = Field(default=(), alias="allowedTools", max_length=100)
	authorization_token: SecretStr | None = Field(default=None, alias="authorizationToken")
	connect_timeout: int = Field(alias="connectTimeout", ge=1, le=120)
	read_timeout: int = Field(alias="readTimeout", ge=1, le=600)

	@field_validator("url")
	@classmethod
	def validate_url(cls, value: str) -> str:
		if any(ord(character) < 32 or ord(character) == 127 for character in value):
			raise ValueError("MCP URL contains control characters")
		if len(value.split()) != 1:
			raise ValueError("MCP URL must not contain whitespace")
		from urllib.parse import urlsplit

		parts = urlsplit(value)
		if parts.scheme != "https" or not parts.hostname:
			raise ValueError("MCP URL must be a valid HTTPS URL")
		if parts.username or parts.password or parts.query or parts.fragment:
			raise ValueError("MCP URL must not embed credentials, a query string, or a fragment")
		try:
			port = parts.port
		except ValueError as error:
			raise ValueError("MCP URL has an invalid port") from error
		if port is not None and not 1 <= port <= 65535:
			raise ValueError("MCP URL has an invalid port")
		return value

	@field_validator("allowed_tools")
	@classmethod
	def validate_allowed_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
		if len(value) != len(set(value)) or any(not item or len(item) > 140 for item in value):
			raise ValueError("allowed MCP tools must be unique, non-empty, and bounded")
		return value

	@model_validator(mode="after")
	def bearer_combination(self) -> ExternalRuntimeMCPServer:
		if self.authorization_token is not None and not self.authorization_token.get_secret_value():
			raise ValueError("authorization token must not be empty")
		return self


class MCPAwareExternalRuntimeConfig(SubAgentAwareExternalRuntimeConfig):
	"""Schema-v5 API-key runtime contract additionally carrying MCP connections."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	schema_version: Literal[5] = Field(default=5, alias="schemaVersion")
	mcp_contract_version: Literal[1] = Field(default=1, alias="mcpContractVersion")
	mcp_servers: tuple[ExternalRuntimeMCPServer, ...] = Field(default=(), alias="mcpServers", max_length=10)

	@model_validator(mode="after")
	def unique_mcp_keys(self) -> MCPAwareExternalRuntimeConfig:
		keys = [server.key for server in self.mcp_servers]
		if len(keys) != len(set(keys)):
			raise ValueError("MCP server keys must be unique")
		return self

	def private_payload(self) -> dict[str, Any]:
		"""Serialize for a trusted machine caller, including the MCP tokens."""
		payload = super().private_payload()
		entries = payload.get("mcpServers") or []
		for entry, server in zip(entries, self.mcp_servers, strict=True):
			if server.authorization_token is None:
				entry.pop("authorizationToken", None)
			else:
				entry["authorizationToken"] = server.authorization_token.get_secret_value()
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


class SubAgentAwareCodexExternalRuntimeDescriptor(StructuredCodexExternalRuntimeDescriptor):
	"""Schema-v4 Codex descriptor carrying Level 2 sub-agent descriptors."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	schema_version: Literal[4] = Field(default=4, alias="schemaVersion")
	agent_level: Literal["0", "1", "2"] = Field(default="1", alias="agentLevel")
	sub_agents: tuple[ExternalSubAgent, ...] = Field(alias="subAgents", max_length=10)


class MCPAwareCodexExternalRuntimeDescriptor(SubAgentAwareCodexExternalRuntimeDescriptor):
	"""Schema-v5 Codex descriptor additionally carrying MCP connections.

	Model credentials stay with Porch's lease broker exactly like schema v2;
	the MCP connections ride the descriptor so Porch can embed them into the
	inline runtime dict. Tokens are ``SecretStr`` and never appear in public
	dumps — use :meth:`private_mcp_servers` for the trusted plaintext list.
	"""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

	schema_version: Literal[5] = Field(default=5, alias="schemaVersion")
	mcp_contract_version: Literal[1] = Field(default=1, alias="mcpContractVersion")
	mcp_servers: tuple[ExternalRuntimeMCPServer, ...] = Field(default=(), alias="mcpServers", max_length=10)

	@model_validator(mode="after")
	def unique_mcp_keys(self) -> MCPAwareCodexExternalRuntimeDescriptor:
		keys = [server.key for server in self.mcp_servers]
		if len(keys) != len(set(keys)):
			raise ValueError("MCP server keys must be unique")
		return self

	def private_mcp_servers(self) -> list[dict[str, Any]]:
		"""Serialize the MCP connections with plaintext tokens for the trusted caller."""
		entries = [server.model_dump(mode="json", by_alias=True) for server in self.mcp_servers]
		for entry, server in zip(entries, self.mcp_servers, strict=True):
			if server.authorization_token is None:
				entry.pop("authorizationToken", None)
			else:
				entry["authorizationToken"] = server.authorization_token.get_secret_value()
		return entries


# Compatibility aliases for callers that group return variants as configurations.
CodexExternalRuntimeConfig = CodexExternalRuntimeDescriptor
ExternalRuntime = (
	ExternalRuntimeConfig
	| StructuredExternalRuntimeConfig
	| SubAgentAwareExternalRuntimeConfig
	| MCPAwareExternalRuntimeConfig
	| CodexExternalRuntimeDescriptor
	| StructuredCodexExternalRuntimeDescriptor
	| SubAgentAwareCodexExternalRuntimeDescriptor
	| MCPAwareCodexExternalRuntimeDescriptor
)


def resolve_external_runtime(
	agent_name: str,
	context=None,
	*,
	legacy_skill_instructions: bool = False,
	include_sub_agents: bool = False,
	include_mcp_servers: bool = False,
) -> ExternalRuntime:
	"""Resolve one enabled AFAA agent for execution outside the Frappe process.

	Structured skills are the default. Existing threads may explicitly request the
	legacy contract, which flattens skill instructions into the agent instructions.
	``include_sub_agents`` additionally resolves the Level 2 delegates of a Level 1
	agent into a schema-v4 contract; any drifted child configuration raises so the
	caller can degrade to the previous contract. ``include_mcp_servers`` resolves
	the agent's MCP connections into a schema-v5 contract; MCP is administrator
	intent and never silently dropped, so the caller must not degrade it.
	"""
	resolved = resolve_ai_agent(
		agent_name,
		context,
		include_sub_agents=include_sub_agents,
		include_mcp_servers=include_mcp_servers,
	)
	if legacy_skill_instructions:
		instructions = _non_empty_instructions(
			resolved.prompt, *(skill.instructions for skill in resolved.skills)
		)
		skills: tuple[ExternalRuntimeSkill, ...] = ()
		tools: tuple[ExternalRuntimeTool, ...] = ()
	else:
		instructions = _non_empty_instructions(resolved.prompt)
		skills, tools = build_structured_runtime_capabilities(resolved)

	if include_sub_agents and resolved.sub_agents:
		if legacy_skill_instructions:
			frappe.throw(
				_("Sub-agent contracts require the structured runtime contract."),
				frappe.ValidationError,
			)
		sub_agents = _build_external_sub_agents(resolved)
	else:
		sub_agents = ()

	mcp_servers = (
		build_external_mcp_servers(resolved) if (include_mcp_servers and resolved.mcp_servers) else ()
	)

	if resolved.model.provider_type == "openai_codex":
		return resolve_codex_external_runtime(
			resolved,
			instructions,
			skills=skills,
			tools=tools,
			legacy_skill_instructions=legacy_skill_instructions,
			sub_agents=sub_agents,
			mcp_servers=mcp_servers,
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
		if sub_agents or mcp_servers:
			safe_configuration["agentLevel"] = resolved.agent_level
			safe_configuration["subAgents"] = tuple(
				sub_agent.model_dump(mode="json", by_alias=True) for sub_agent in sub_agents
			)
			if mcp_servers:
				safe_configuration["schemaVersion"] = 5
				safe_configuration["mcpContractVersion"] = 1
				safe_configuration["mcpServers"] = _mcp_server_payload(mcp_servers)
				config_type = MCPAwareExternalRuntimeConfig
			else:
				safe_configuration["schemaVersion"] = 4
				config_type = SubAgentAwareExternalRuntimeConfig
		else:
			config_type = StructuredExternalRuntimeConfig
	dto_values: dict[str, Any] = {
		**safe_configuration,
		"model": {**safe_configuration["model"], "apiKey": api_key},
		"configurationFingerprint": configuration_fingerprint(safe_configuration),
	}
	if mcp_servers:
		# The fingerprint hashes the masked dump so token rotation cannot change
		# it; the DTO itself carries the real secret for the private payload.
		dto_values["mcpServers"] = _mcp_server_payload(mcp_servers, private=True)
	return config_type.model_validate(dto_values)


def _mcp_server_payload(
	mcp_servers: tuple[ExternalRuntimeMCPServer, ...], *, private: bool = False
) -> list[dict[str, Any]]:
	"""Dump MCP connections; masked for fingerprints, plaintext for the DTO.

	The masked ``authorizationToken`` keeps the configuration fingerprint
	stable across token rotation; the private variant re-injects the real
	secret so the validated DTO can serve ``private_payload``.
	"""
	entries = [server.model_dump(mode="json", by_alias=True) for server in mcp_servers]
	if not private:
		return entries
	for entry, server in zip(entries, mcp_servers, strict=True):
		if server.authorization_token is None:
			entry.pop("authorizationToken", None)
		else:
			entry["authorizationToken"] = server.authorization_token.get_secret_value()
	return entries


def build_external_mcp_servers(resolved) -> tuple[ExternalRuntimeMCPServer, ...]:
	"""Build the flat, uniquely-keyed MCP connection list for a schema-v5 payload."""
	entries: list[ExternalRuntimeMCPServer] = []
	seen: set[str] = set()
	for server in resolved.mcp_servers:
		if server.effective_key in seen:
			frappe.throw(
				_("MCP attachment {0} collides with another attachment of this agent.").format(
					frappe.bold(server.effective_key)
				),
				frappe.ValidationError,
			)
		seen.add(server.effective_key)
		token = server.token.get_secret_value() if server.token is not None else None
		entries.append(
			ExternalRuntimeMCPServer(
				key=server.effective_key,
				name=server.name,
				url=server.url,
				transport=server.transport or "auto",
				allowedTools=server.allowed_tools,
				authorizationToken=token,
				connectTimeout=server.connect_timeout,
				readTimeout=server.read_timeout,
			)
		)
	return tuple(entries)


def resolve_codex_external_runtime(
	resolved,
	instructions: tuple[str, ...],
	*,
	skills: tuple[ExternalRuntimeSkill, ...] = (),
	tools: tuple[ExternalRuntimeTool, ...] = (),
	legacy_skill_instructions: bool = True,
	sub_agents: tuple[ExternalSubAgent, ...] = (),
	mcp_servers: tuple[ExternalRuntimeMCPServer, ...] = (),
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
		if sub_agents or mcp_servers:
			safe_configuration["agentLevel"] = resolved.agent_level
			safe_configuration["subAgents"] = tuple(
				sub_agent.model_dump(mode="json", by_alias=True) for sub_agent in sub_agents
			)
			if mcp_servers:
				safe_configuration["schemaVersion"] = 5
				safe_configuration["mcpContractVersion"] = 1
				safe_configuration["mcpServers"] = _mcp_server_payload(mcp_servers)
				config_type = MCPAwareCodexExternalRuntimeDescriptor
			else:
				safe_configuration["schemaVersion"] = 4
				config_type = SubAgentAwareCodexExternalRuntimeDescriptor
		else:
			config_type = StructuredCodexExternalRuntimeDescriptor
	dto_values: dict[str, Any] = {
		**safe_configuration,
		"configurationFingerprint": configuration_fingerprint(safe_configuration),
	}
	if mcp_servers:
		# Strict mode on the Codex DTOs rejects list-to-tuple coercion.
		dto_values["mcpServers"] = tuple(_mcp_server_payload(mcp_servers, private=True))
	return config_type.model_validate(dto_values)


def _build_external_sub_agents(resolved) -> tuple[ExternalSubAgent, ...]:
	"""Build credential-free Level 2 descriptors from resolved delegates.

	Each child carries its own model, tools, and skills. API-key children keep the secret
	with Porch; Codex children bind their provider account so Porch can issue one
	credential lease per child. Any drift fails closed.
	"""
	sub_agents: list[ExternalSubAgent] = []
	for child in resolved.sub_agents:
		model = _external_sub_agent_model(child)
		tools = tuple(
			_external_sub_agent_tool(tool) for tool in sorted(child.tools, key=lambda item: item.key)
		)
		sub_agents.append(
			ExternalSubAgent(
				agentId=f"afaa:{child.key}",
				name=child.name,
				delegateName=child.name,
				description=child.description,
				instructions=_non_empty_instructions(child.prompt),
				model=model,
				tools=tools,
				skills=build_sub_agent_skill_capabilities(child),
				maxCalls=child.max_calls,
				timeoutSeconds=child.timeout_seconds,
			)
		)
	return tuple(sub_agents)


def build_sub_agent_skill_capabilities(child) -> tuple[ExternalRuntimeSkill, ...]:
	"""Build deterministic credential-free skill DTOs for one resolved delegate.

	Unlike the parent path, a delegate's skills may require the reserved
	runtime tool keys (``read_file`` & co.) as long as the child advertises
	them; a required tool the child does not allow fails closed so the whole
	contract degrades instead of silently widening the child's toolset.
	"""
	tool_keys = {tool.key for tool in child.tools}
	skills = []
	for skill in sorted(child.skills, key=lambda item: item.key):
		required_tools = tuple(sorted(set(skill.required_tools)))
		unsupported = set(required_tools) - tool_keys
		if unsupported:
			frappe.throw(
				_("AI Skill {0} requires tools not allowed by sub-agent {1}: {2}").format(
					frappe.bold(skill.name), frappe.bold(child.name), ", ".join(sorted(unsupported))
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
		skills.append(
			ExternalRuntimeSkill.model_validate(
				{**snapshot, "fingerprint": configuration_fingerprint(snapshot)}
			)
		)
	return tuple(skills)


def _external_sub_agent_model(child) -> ExternalSubAgentModel:
	"""Resolve one delegate's own model descriptor, credential-free."""
	if child.model.provider_type == "openai_codex":
		from afaa.ai.oauth.openai_codex_service import CodexReconnectRequiredError

		account = frappe.get_doc("AI Provider Account", child.model.provider_account)
		if account.disabled or account.oauth_status != "Connected" or not account.connected_user:
			raise CodexReconnectRequiredError(
				_("ChatGPT authorization expired; reconnect account.")
			) from None
		account_id = (account.external_account_id or "").strip()
		if not account_id:
			raise CodexReconnectRequiredError(
				_("ChatGPT authorization expired; reconnect account.")
			) from None
		return ExternalSubAgentModel(
			providerType="openai_codex",
			modelId=child.model.model_id,
			settings={**child.model.settings, "openai_store": False},
			timeout=child.timeout,
			retries=child.retries,
			providerAccount=account.name,
			accountId=account_id,
		)
	if child.model.provider_type not in {"openai", "google", "zai", "moonshot"}:
		frappe.throw(
			_("AI provider type {0} is not supported by sub-agents.").format(
				frappe.bold(child.model.provider_type)
			),
			frappe.ValidationError,
		)
	base_url = (child.model.base_url or "").strip() or None
	# API-key children stay credential-free and unbound: Porch re-resolves the
	# child agent through its Space allowlist (which returns the provider
	# account) and embeds the key itself, exactly like the parent path.
	return ExternalSubAgentModel(
		providerType=child.model.provider_type,
		modelId=child.model.model_id,
		settings=dict(child.model.settings),
		timeout=child.timeout,
		retries=child.retries,
		baseUrl=base_url,
	)


def _external_sub_agent_tool(tool) -> ExternalSubAgentTool:
	"""Classify one delegate tool as runtime-implemented or Frappe-proxied."""
	from afaa.ai.agent_levels import runtime_tool_definition

	runtime_definition = runtime_tool_definition(tool.key)
	if runtime_definition is not None:
		if tool.method != runtime_definition["method"] or tool.key != runtime_definition["tool_name"]:
			frappe.throw(
				_("AI Tool {0} no longer matches the reserved sub-agent runtime tool.").format(
					frappe.bold(tool.key)
				),
				frappe.ValidationError,
			)
		return ExternalSubAgentTool(
			key=tool.key,
			name=tool.name,
			description=tool.description,
			inputSchema=tool.input_schema,
			outputSchema=tool.output_schema,
			runtime=True,
		)

	expected_method = EXTERNAL_READ_TOOL_METHODS.get(tool.key)
	definition = get_tool_definition(tool.key) if expected_method else None
	if (
		not expected_method
		or not definition
		or definition.key != tool.key
		or definition.method != expected_method
		or tool.method != expected_method
	):
		frappe.throw(
			_("AI Tool {0} is not an approved tool for sub-agents.").format(frappe.bold(tool.key)),
			frappe.ValidationError,
		)
	return ExternalSubAgentTool(
		key=tool.key,
		name=tool.name,
		description=tool.description,
		inputSchema=tool.input_schema,
		outputSchema=tool.output_schema,
		runtime=False,
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
	model = {
		"providerType": resolved.model.provider_type,
		"modelId": resolved.model.model_id,
		"settings": resolved.model.settings,
		"timeout": resolved.timeout,
		"retries": resolved.retries,
	}
	base_url = (getattr(resolved.model, "base_url", None) or "").strip()
	if base_url:
		model["baseUrl"] = base_url
	return {
		"schemaVersion": 1,
		"agentId": f"afaa:{resolved.key}",
		"name": resolved.name,
		"instructions": instructions,
		"model": model,
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
