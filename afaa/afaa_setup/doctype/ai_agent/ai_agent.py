# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from afaa.ai.agent_levels import (
	AGENT_LEVEL_SUB_AGENT,
	AGENT_LEVEL_WORKER,
	AGENT_LEVELS,
	SUB_AGENT_RUNTIME_TOOL_KEYS,
)
from afaa.ai.mcp import (
	MAX_MCP_SERVERS_PER_AGENT,
	effective_mcp_key,
	resolve_mcp_account,
)
from afaa.ai.prompts import parse_json_object, validate_jinja_template
from afaa.utils.data import validate_key
from afaa.utils.data import validate_unique_rows as validate_distinct_rows

MAX_SUB_AGENTS_PER_PARENT = 10


class AIAgent(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from afaa.afaa_setup.doctype.ai_agent_mcp_server.ai_agent_mcp_server import AIAgentMCPServer
		from afaa.afaa_setup.doctype.ai_agent_skill.ai_agent_skill import AIAgentSkill
		from afaa.afaa_setup.doctype.ai_agent_sub_agent.ai_agent_sub_agent import AIAgentSubAgent
		from afaa.afaa_setup.doctype.ai_agent_task_assignment.ai_agent_task_assignment import (
			AIAgentTaskAssignment,
		)
		from afaa.afaa_setup.doctype.ai_agent_tool.ai_agent_tool import AIAgentTool
		from afaa.afaa_setup.doctype.ai_skill_tag_link.ai_skill_tag_link import AISkillTagLink

		agent_key: DF.Data
		agent_level: DF.Literal["", "0", "1", "2"]
		agent_name: DF.Data
		allowed_tools: DF.Table[AIAgentTool]
		description: DF.SmallText | None
		disabled: DF.Check
		max_tokens: DF.Int
		mcp_servers: DF.Table[AIAgentMCPServer]
		model: DF.Link
		model_overrides: DF.JSON | None
		provider: DF.Link
		provider_account: DF.Link
		retries: DF.Int
		skill_tags: DF.TableMultiSelect[AISkillTagLink]
		skills: DF.Table[AIAgentSkill]
		sub_agents: DF.Table[AIAgentSubAgent]
		system_prompt: DF.Code
		tasks: DF.Table[AIAgentTaskAssignment]
		temperature: DF.Float
		timeout: DF.Duration | None
		use_temperature: DF.Check
	# end: auto-generated types

	def validate(self):
		self.validate_agent_key()
		self.validate_agent_level()
		parse_json_object(self.model_overrides, _("Additional Model Settings"))
		validate_jinja_template(self.system_prompt)
		self.timeout = flt(self.timeout)
		self.retries = cint(self.retries)
		self.max_tokens = cint(self.max_tokens)
		if self.use_temperature:
			self.temperature = flt(self.temperature)
			if not 0 <= self.temperature <= 2:
				frappe.throw(_("Temperature must be between 0 and 2."))
		if self.max_tokens < 0:
			frappe.throw(_("Max Tokens cannot be negative."))
		if self.timeout <= 0:
			frappe.throw(_("Timeout must be greater than zero."))
		if self.retries < 0:
			frappe.throw(_("Retries cannot be negative."))

		self.validate_unique_rows("tasks", "task", _("Task Definition"))
		self.validate_unique_rows("skills", "skill", _("Skill"))
		self.validate_unique_rows("skill_tags", "tag", _("Skill Tag"))
		self.validate_unique_rows("allowed_tools", "tool", _("Allowed Tool"))
		self.validate_unique_rows("sub_agents", "sub_agent", _("Sub Agent"))
		self.validate_sub_agents()
		self.validate_mcp_servers()
		self.validate_dependencies()

	def validate_agent_key(self):
		validate_key(self.agent_key, _("Agent Key"))
		if not self.is_new():
			previous = self.get_doc_before_save()
			if previous and previous.agent_key != self.agent_key:
				frappe.throw(_("Agent Key cannot be changed after the AI Agent is created."))

	def validate_agent_level(self):
		if (self.agent_level or AGENT_LEVEL_WORKER) not in AGENT_LEVELS:
			frappe.throw(_("Agent Level is invalid."), frappe.ValidationError)
		if not self.agent_level:
			self.agent_level = AGENT_LEVEL_WORKER

	def validate_sub_agents(self):
		if self.agent_level != AGENT_LEVEL_WORKER:
			if self.sub_agents:
				frappe.throw(
					_("Only Worker Agents may configure Sub Agents."),
					frappe.ValidationError,
				)
			return
		if len(self.sub_agents) > MAX_SUB_AGENTS_PER_PARENT:
			frappe.throw(
				_("An agent may reference at most {0} sub-agents.").format(MAX_SUB_AGENTS_PER_PARENT),
				frappe.ValidationError,
			)
		for row in self.sub_agents:
			if row.sub_agent == self.name:
				frappe.throw(_("An agent cannot delegate to itself."), frappe.ValidationError)
			row.max_calls = cint(row.max_calls)
			row.timeout_seconds = cint(row.timeout_seconds)
			if row.max_calls < 0 or row.timeout_seconds < 0:
				frappe.throw(_("Sub-agent limits cannot be negative."), frappe.ValidationError)
			target = frappe.db.get_value(
				"AI Agent",
				row.sub_agent,
				["agent_level", "disabled"],
				as_dict=True,
			)
			if not target:
				frappe.throw(
					_("Sub Agent {0} does not exist.").format(frappe.bold(row.sub_agent)),
					frappe.ValidationError,
				)
			if target.disabled:
				frappe.throw(
					_("Sub Agent {0} is disabled.").format(frappe.bold(row.sub_agent)),
					frappe.ValidationError,
				)
			if (target.agent_level or AGENT_LEVEL_WORKER) != AGENT_LEVEL_SUB_AGENT:
				frappe.throw(
					_("{0} is not a Sub Agent (Agent Level 2).").format(frappe.bold(row.sub_agent)),
					frappe.ValidationError,
				)
			other_parent = frappe.db.get_value(
				"AI Agent Sub Agent",
				{"sub_agent": row.sub_agent, "parent": ("!=", self.name), "parenttype": "AI Agent"},
				"parent",
			)
			if other_parent:
				frappe.throw(
					_("Sub Agent {0} is already referenced by {1}.").format(
						frappe.bold(row.sub_agent), frappe.bold(other_parent)
					),
					frappe.ValidationError,
				)

	def on_trash(self):
		if frappe.db.exists("AI Agent Sub Agent", {"sub_agent": self.name, "parenttype": "AI Agent"}):
			parents = frappe.get_all(
				"AI Agent Sub Agent",
				filters={"sub_agent": self.name, "parenttype": "AI Agent"},
				pluck="parent",
			)
			frappe.throw(
				_(
					"AI Agent {0} is referenced as a sub-agent by {1}. Remove the reference before trashing."
				).format(frappe.bold(self.name), frappe.bold(", ".join(sorted(parents)))),
				frappe.ValidationError,
			)

	def validate_mcp_servers(self):
		"""Validate the (server, account) MCP attachments of one agent.

		Each row expands to exactly one runtime connection: an explicit account
		must belong to the linked server, an omitted account resolves the
		server's single enabled default, and every derived effective key must
		be unique so tool namespaces never collide. Level 2 (Sub Agent)
		records cannot carry MCP servers in the v4 delegation contract.
		"""
		rows = self.mcp_servers or []
		if not rows:
			return
		if self.agent_level == AGENT_LEVEL_SUB_AGENT:
			frappe.throw(
				_("Sub Agents cannot configure MCP servers."),
				frappe.ValidationError,
			)
		if len(rows) > MAX_MCP_SERVERS_PER_AGENT:
			frappe.throw(
				_("An agent may reference at most {0} MCP servers.").format(MAX_MCP_SERVERS_PER_AGENT),
				frappe.ValidationError,
			)

		seen_pairs: set[tuple[str, str | None]] = set()
		effective_keys: dict[str, tuple[str, str | None]] = {}
		for row in rows:
			pair = (row.mcp_server, row.mcp_server_account or None)
			if pair in seen_pairs:
				frappe.throw(
					_("MCP server {0} with the same account is listed more than once.").format(
						frappe.bold(row.mcp_server)
					)
				)
			seen_pairs.add(pair)
			server = frappe.get_doc("AI MCP Server", row.mcp_server)
			if self.disabled:
				continue
			if server.disabled:
				frappe.throw(
					_("AI MCP Server {0} is disabled.").format(frappe.bold(server.name)),
					frappe.ValidationError,
				)
			account = resolve_mcp_account(server.name, row.mcp_server_account or None)
			effective_key = effective_mcp_key(server.server_key, account.account_key, account.is_default)
			previous = effective_keys.get(effective_key)
			if previous is not None:
				frappe.throw(
					_(
						"MCP attachment {0} collides with another attachment of this agent ({1} / {2})."
					).format(
						frappe.bold(effective_key),
						frappe.bold(previous[0]),
						frappe.bold(pair[0]),
					),
					frappe.ValidationError,
				)
			effective_keys[effective_key] = pair

	def validate_unique_rows(self, table_field: str, link_field: str, label: str):
		validate_distinct_rows(self.get(table_field), link_field, label)

	def validate_dependencies(self):
		model = frappe.get_doc("AI Model", self.model)
		provider_account = frappe.get_doc("AI Provider Account", self.provider_account)
		if provider_account.provider != model.provider:
			frappe.throw(
				_("AI Provider Account {0} does not match AI Model {1}.").format(
					frappe.bold(provider_account.name), frappe.bold(model.name)
				)
			)
		if self.disabled:
			return

		provider_disabled = frappe.db.get_value("AI Provider", model.provider, "disabled")
		if model.disabled or not model.available:
			frappe.throw(_("AI Model {0} is disabled or unavailable.").format(frappe.bold(model.name)))
		if provider_disabled:
			frappe.throw(_("AI Provider {0} is disabled.").format(frappe.bold(model.provider)))
		if provider_account.disabled:
			frappe.throw(_("AI Provider Account {0} is disabled.").format(frappe.bold(provider_account.name)))

		allowed_tools = {row.tool for row in self.allowed_tools}
		runtime_tool_keys = allowed_tools & SUB_AGENT_RUNTIME_TOOL_KEYS
		if runtime_tool_keys and self.agent_level != AGENT_LEVEL_SUB_AGENT:
			frappe.throw(
				_("Runtime workspace tools are reserved for Sub Agents: {0}").format(
					", ".join(sorted(runtime_tool_keys))
				),
				frappe.ValidationError,
			)
		for tool_name in sorted(runtime_tool_keys):
			runtime_tool = frappe.db.get_value("AI Tool", tool_name, ["disabled", "available"], as_dict=True)
			if not runtime_tool or runtime_tool.disabled or not runtime_tool.available:
				frappe.throw(_("AI Tool {0} is disabled or unavailable.").format(frappe.bold(tool_name)))
		registered_tools = allowed_tools - SUB_AGENT_RUNTIME_TOOL_KEYS
		for row in self.tasks:
			task = frappe.get_doc("AI Task Definition", row.task)
			if task.disabled:
				frappe.throw(_("AI Task Definition {0} is disabled.").format(frappe.bold(task.name)))
			required_tools = {item.tool for item in task.required_tools}
			missing = required_tools - allowed_tools
			if missing:
				frappe.throw(
					_("AI Task Definition {0} requires tools not allowed by this agent: {1}").format(
						frappe.bold(task.name), ", ".join(sorted(missing))
					)
				)

		if allowed_tools and not model.supports_tools:
			frappe.throw(
				_("AI Model {0} is not configured to support tools.").format(frappe.bold(model.name))
			)

		for tool_name in registered_tools:
			tool = frappe.db.get_value("AI Tool", tool_name, ["disabled", "available"], as_dict=True)
			if not tool or tool.disabled or not tool.available:
				frappe.throw(_("AI Tool {0} is disabled or unavailable.").format(frappe.bold(tool_name)))

		for row in self.skills:
			skill = frappe.get_doc("AI Skill", row.skill)
			if skill.disabled:
				frappe.throw(_("AI Skill {0} is disabled.").format(frappe.bold(skill.name)))
			required_tools = {item.tool for item in skill.required_tools}
			missing = required_tools - allowed_tools
			if missing:
				frappe.throw(
					_("AI Skill {0} requires tools not allowed by this agent: {1}").format(
						frappe.bold(skill.name), ", ".join(sorted(missing))
					)
				)
