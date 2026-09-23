# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from typing import Any

import frappe
from frappe import _
from pydantic import BaseModel, ConfigDict

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
	agent_level: str = "1"
	timeout: float
	retries: int


def resolve_ai_agent(
	agent_name: str, context=None, *, require_enabled: bool = True, include_sub_agents: bool = False
) -> ResolvedAIAgent:
	agent = frappe.get_doc("AI Agent", agent_name)
	if require_enabled and agent.disabled:
		frappe.throw(_("AI Agent {0} is disabled.").format(frappe.bold(agent.name)))

	from afaa.ai.agent_levels import AGENT_LEVEL_WORKER, agent_level_number

	agent_level = (getattr(agent, "agent_level", None) or AGENT_LEVEL_WORKER).strip()
	sub_agents = _resolve_sub_agents(agent, context, require_enabled=require_enabled) if (
		include_sub_agents and agent_level_number(agent_level) == 1
	) else ()

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
	for row in agent.skills:
		skill = frappe.get_doc("AI Skill", row.skill)
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
		agent_level=agent_level,
		timeout=agent.timeout,
		retries=agent.retries,
	)


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
