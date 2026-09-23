# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from typing import Any

import frappe
from frappe import _
from pydantic import BaseModel, ConfigDict, SecretStr

from afaa.ai.mcp import (
	AUTH_TYPE_BEARER,
	AUTH_TYPE_NONE,
	effective_mcp_key,
	mcp_account_display_name,
	resolve_mcp_account,
	validate_mcp_url,
)
from afaa.ai.prompts import parse_json_object, render_system_prompt
from afaa.ai.provider import get_provider_class


class ResolvedModel(BaseModel):
	model_config = ConfigDict(frozen=True)

	name: str
	provider: str
	provider_account: str
	provider_type: str
	model_id: str
	settings: dict[str, Any]
	base_url: str | None = None


class ResolvedTool(BaseModel):
	model_config = ConfigDict(frozen=True)

	key: str
	name: str
	description: str
	method: str
	input_schema: dict[str, Any]
	output_schema: dict[str, Any]


class ResolvedSkill(BaseModel):
	model_config = ConfigDict(frozen=True)

	key: str
	name: str
	description: str | None
	instructions: str
	required_tools: tuple[str, ...]


class ResolvedSubAgent(BaseModel):
	"""One configured Level 2 delegate of a Level 1 agent."""

	model_config = ConfigDict(frozen=True)

	name: str
	key: str
	description: str | None
	prompt: str
	model: ResolvedModel
	tools: tuple[ResolvedTool, ...]
	skills: tuple[ResolvedSkill, ...] = ()
	max_calls: int | None = None
	timeout_seconds: float | None = None
	timeout: float
	retries: int


class ResolvedMCPServer(BaseModel):
	"""One resolved (server x account) MCP connection of an agent.

	``effective_key`` is the model-visible namespace: the server's default
	account keeps ``<server_key>`` and any other account becomes
	``<server_key>-<account_key>``. The bearer token is a secret and never
	appears in public dumps or fingerprints.
	"""

	model_config = ConfigDict(frozen=True)

	server_key: str
	account_key: str | None = None
	effective_key: str
	name: str
	url: str
	transport: str = "auto"
	allowed_tools: tuple[str, ...] = ()
	connect_timeout: int = 10
	read_timeout: int = 60
	auth_type: str = AUTH_TYPE_NONE
	token: SecretStr | None = None


class ResolvedOutputField(BaseModel):
	model_config = ConfigDict(frozen=True)

	field_name: str
	field_type: str
	description: str | None
	required: bool


class ResolvedTask(BaseModel):
	model_config = ConfigDict(frozen=True)

	document_name: str
	key: str
	name: str
	description: str
	instructions: str
	allow_soft_failure: bool
	required_tools: tuple[str, ...]
	expected_output: tuple[ResolvedOutputField, ...]


class ResolvedAIAgent(BaseModel):
	model_config = ConfigDict(frozen=True)

	key: str
	name: str
	model: ResolvedModel
	prompt: str
	tasks: tuple[ResolvedTask, ...]
	skills: tuple[ResolvedSkill, ...]
	tools: tuple[ResolvedTool, ...]
	sub_agents: tuple[ResolvedSubAgent, ...] = ()
	mcp_servers: tuple[ResolvedMCPServer, ...] = ()
	agent_level: str = "1"
	timeout: float
	retries: int


def resolve_ai_agent(
	agent_name: str,
	context=None,
	*,
	require_enabled: bool = True,
	include_sub_agents: bool = False,
	include_mcp_servers: bool = False,
) -> ResolvedAIAgent:
	agent = frappe.get_doc("AI Agent", agent_name)
	if require_enabled and agent.disabled:
		frappe.throw(_("AI Agent {0} is disabled.").format(frappe.bold(agent.name)))

	from afaa.ai.agent_levels import AGENT_LEVEL_WORKER, agent_level_number

	agent_level = (getattr(agent, "agent_level", None) or AGENT_LEVEL_WORKER).strip()
	sub_agents = (
		_resolve_sub_agents(agent, context, require_enabled=require_enabled)
		if (include_sub_agents and agent_level_number(agent_level) == 1)
		else ()
	)
	mcp_servers = _resolve_mcp_servers(agent, require_enabled=require_enabled) if include_mcp_servers else ()

	model = frappe.get_doc("AI Model", agent.model)
	provider = frappe.get_doc("AI Provider", model.provider)
	provider_account = frappe.get_doc("AI Provider Account", agent.provider_account)
	if provider_account.provider != provider.name:
		frappe.throw(
			_("AI Provider Account {0} does not match AI Model {1}.").format(
				frappe.bold(provider_account.name), frappe.bold(model.name)
			)
		)

	if require_enabled:
		for doc in (model, provider, provider_account):
			if doc.disabled:
				frappe.throw(_("{0} {1} is disabled.").format(doc.doctype, frappe.bold(doc.name)))
		if not model.available:
			frappe.throw(_("AI Model {0} is unavailable.").format(frappe.bold(model.name)))

	provider_adapter = get_provider_class(provider.provider_type)(provider_account)

	allowed_tool_names = {row.tool for row in agent.allowed_tools}
	tools = tuple(_resolve_tool(name, require_enabled=require_enabled) for name in sorted(allowed_tool_names))
	tool_keys = {tool.key for tool in tools}

	tasks = []
	for row in agent.tasks:
		task = frappe.get_doc("AI Task Definition", row.task)
		if require_enabled and task.disabled:
			frappe.throw(_("AI Task Definition {0} is disabled.").format(frappe.bold(task.name)))

		required_tools = tuple(sorted(item.tool for item in task.required_tools))
		missing = set(required_tools) - tool_keys
		if missing:
			frappe.throw(
				_("AI Task Definition {0} requires tools not allowed by this agent: {1}").format(
					frappe.bold(task.name), ", ".join(sorted(missing))
				)
			)
		tasks.append(
			ResolvedTask(
				document_name=task.name,
				key=task.task_key,
				name=task.task_name,
				description=task.description,
				instructions=task.instructions,
				allow_soft_failure=bool(task.allow_soft_failure),
				required_tools=required_tools,
				expected_output=tuple(
					ResolvedOutputField(
						field_name=field.field_name,
						field_type=field.field_type,
						description=field.description,
						required=bool(field.required),
					)
					for field in task.expected_output
				),
			)
		)

	skills = []
	for skill_name in _agent_skill_names(agent):
		skill = frappe.get_doc("AI Skill", skill_name)
		if require_enabled and skill.disabled:
			frappe.throw(_("AI Skill {0} is disabled.").format(frappe.bold(skill.name)))

		required_tools = tuple(sorted(item.tool for item in skill.required_tools))
		missing = set(required_tools) - tool_keys
		if missing:
			frappe.throw(
				_("AI Skill {0} requires tools not allowed by this agent: {1}").format(
					frappe.bold(skill.name), ", ".join(sorted(missing))
				)
			)
		skills.append(
			ResolvedSkill(
				key=skill.skill_key,
				name=skill.skill_name,
				description=skill.description or None,
				instructions=skill.instructions,
				required_tools=required_tools,
			)
		)

	settings = parse_json_object(agent.model_overrides, _("Additional Model Settings"))
	if agent.use_temperature:
		settings.setdefault("temperature", agent.temperature)
	if agent.max_tokens:
		settings.setdefault("max_tokens", agent.max_tokens)
	settings = provider_adapter.prepare_model_settings(settings)

	return ResolvedAIAgent(
		key=agent.agent_key,
		name=agent.agent_name,
		model=ResolvedModel(
			name=model.name,
			provider=provider.name,
			provider_account=provider_account.name,
			provider_type=provider.provider_type,
			model_id=model.model_id,
			settings=settings,
			base_url=(provider.base_url or "").strip() or None,
		),
		prompt=render_system_prompt(agent.system_prompt, context),
		tasks=tuple(tasks),
		skills=tuple(skills),
		tools=tools,
		sub_agents=tuple(sub_agents),
		mcp_servers=tuple(mcp_servers),
		agent_level=agent_level,
		timeout=agent.timeout,
		retries=agent.retries,
	)


def _agent_skill_names(agent) -> list[str]:
	"""Effective skill documents of one agent: explicit rows, then tag expansion.

	The agent's effective skill set is the union of its explicit ``AI Agent
	Skill`` rows (in row order) and every ``AI Skill`` carrying one of the
	agent's selected tags, deduplicated, with tag-derived skills appended in
	deterministic key order. Tags never mutate the agent's explicit rows, and
	disabled tags resolve to nothing. Per-skill guards (disabled skills,
	required tools vs allowed tools) are applied by the caller.
	"""
	explicit_names: list[str] = []
	seen: set[str] = set()
	for row in getattr(agent, "skills", None) or []:
		if row.skill and row.skill not in seen:
			seen.add(row.skill)
			explicit_names.append(row.skill)

	tag_names = [row.tag for row in (getattr(agent, "skill_tags", None) or []) if row.tag]
	if not tag_names:
		return explicit_names

	disabled_tags = set(
		frappe.get_all("AI Skill Tag", filters={"name": ("in", tag_names), "disabled": 1}, pluck="name")
	)
	active_tags = [tag for tag in dict.fromkeys(tag_names) if tag not in disabled_tags]
	if not active_tags:
		return explicit_names

	tagged_skills = set(
		frappe.get_all(
			"AI Skill Tag Link",
			filters={"parenttype": "AI Skill", "tag": ("in", active_tags)},
			pluck="parent",
		)
	)
	return explicit_names + sorted(tagged_skills - seen)


def _resolve_sub_agents(agent, context, *, require_enabled: bool) -> list[ResolvedSubAgent]:
	"""Resolve each configured Level 2 delegate of one Level 1 agent.

	Any drifted, disabled, or misconfigured child fails closed so callers can
	degrade the whole delegation contract rather than silently dropping it.
	"""
	from afaa.ai.agent_levels import AGENT_LEVEL_WORKER, agent_level_number

	sub_agents: list[ResolvedSubAgent] = []
	for row in getattr(agent, "sub_agents", None) or []:
		if not row.sub_agent:
			continue
		if row.sub_agent == agent.name:
			frappe.throw(
				_("AI Agent {0} cannot delegate to itself.").format(frappe.bold(agent.name)),
				frappe.ValidationError,
			)
		child = frappe.get_doc("AI Agent", row.sub_agent)
		child_level = (getattr(child, "agent_level", None) or AGENT_LEVEL_WORKER).strip()
		if agent_level_number(child_level) != 2:
			frappe.throw(
				_("{0} is not a Sub Agent (Agent Level 2).").format(frappe.bold(child.name)),
				frappe.ValidationError,
			)
		resolved_child = resolve_ai_agent(
			row.sub_agent, context, require_enabled=require_enabled, include_sub_agents=False
		)
		max_calls = int(row.max_calls or 0)
		timeout_seconds = float(row.timeout_seconds or 0)
		sub_agents.append(
			ResolvedSubAgent(
				name=child.agent_name,
				key=child.agent_key,
				description=child.description or None,
				prompt=resolved_child.prompt,
				model=resolved_child.model,
				tools=resolved_child.tools,
				skills=resolved_child.skills,
				max_calls=max_calls if max_calls > 0 else None,
				timeout_seconds=timeout_seconds if timeout_seconds > 0 else None,
				timeout=resolved_child.timeout,
				retries=resolved_child.retries,
			)
		)
	return sub_agents


def _resolve_mcp_servers(agent, *, require_enabled: bool) -> list[ResolvedMCPServer]:
	"""Expand each MCP attachment row into one (server x account) connection.

	Servers and accounts are revalidated on every run: a disabled or drifted
	dependency fails closed instead of silently dropping a connection. The
	bearer token is read from the account's encrypted password field and only
	ever leaves the process through the private runtime payload.
	"""
	servers: list[ResolvedMCPServer] = []
	effective_keys: dict[str, str] = {}
	for row in getattr(agent, "mcp_servers", None) or []:
		if not row.mcp_server:
			continue
		server = frappe.get_doc("AI MCP Server", row.mcp_server)
		if require_enabled and server.disabled:
			frappe.throw(
				_("AI MCP Server {0} is disabled.").format(frappe.bold(server.name)),
				frappe.ValidationError,
			)
		account = resolve_mcp_account(
			server.name, row.mcp_server_account or None, require_enabled=require_enabled
		)
		effective_key = effective_mcp_key(server.server_key, account.account_key, account.is_default)
		collision = effective_keys.get(effective_key)
		if collision is not None:
			frappe.throw(
				_("MCP attachment {0} collides with attachment {1}.").format(
					frappe.bold(effective_key), frappe.bold(collision)
				),
				frappe.ValidationError,
			)
		effective_keys[effective_key] = server.name

		auth_type = account.auth_type or AUTH_TYPE_NONE
		token: str | None = None
		if auth_type == AUTH_TYPE_BEARER:
			token = account.get_password("bearer_token", raise_exception=False)
			if not token:
				frappe.throw(
					_("AI MCP Server Account {0} has no bearer token.").format(frappe.bold(account.name)),
					frappe.ValidationError,
				)

		servers.append(
			ResolvedMCPServer(
				server_key=server.server_key,
				account_key=account.account_key,
				effective_key=effective_key,
				name=mcp_account_display_name(server, account),
				url=validate_mcp_url(server.url),
				transport=(server.transport or "auto"),
				allowed_tools=tuple(
					sorted({row.tool_name for row in (server.allowed_tools or []) if row.enabled})
				),
				connect_timeout=int(server.connect_timeout or 10),
				read_timeout=int(server.read_timeout or 60),
				auth_type=auth_type,
				token=SecretStr(token) if token else None,
			)
		)
	servers.sort(key=lambda server: server.effective_key)
	return servers


@frappe.whitelist()
def get_resolved_ai_agent(agent_name: str, context=None) -> dict[str, Any]:
	frappe.only_for(["AI Manager", "System Manager"])
	return resolve_ai_agent(agent_name, context).model_dump(mode="json")


def _resolve_tool(name: str, *, require_enabled: bool) -> ResolvedTool:
	tool = frappe.get_doc("AI Tool", name)
	if require_enabled and (tool.disabled or not tool.available):
		frappe.throw(_("AI Tool {0} is disabled or unavailable.").format(frappe.bold(name)))

	return ResolvedTool(
		key=tool.tool_key,
		name=tool.tool_name,
		description=tool.description,
		method=tool.method,
		input_schema=parse_json_object(tool.input_schema, _("Input Schema")),
		output_schema=parse_json_object(tool.output_schema, _("Output Schema")),
	)
