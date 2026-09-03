# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from afaa.ai.prompts import parse_json_object, validate_jinja_template
from afaa.utils.data import validate_key


class AIAgent(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from afaa.afaa_setup.doctype.ai_agent_skill.ai_agent_skill import AIAgentSkill
		from afaa.afaa_setup.doctype.ai_agent_task_assignment.ai_agent_task_assignment import (
			AIAgentTaskAssignment,
		)
		from afaa.afaa_setup.doctype.ai_agent_tool.ai_agent_tool import AIAgentTool

		agent_key: DF.Data
		agent_name: DF.Data
		allowed_tools: DF.Table[AIAgentTool]
		description: DF.SmallText | None
		disabled: DF.Check
		max_tokens: DF.Int
		model: DF.Link
		model_overrides: DF.JSON | None
		provider: DF.Link
		provider_account: DF.Link
		retries: DF.Int
		skills: DF.Table[AIAgentSkill]
		system_prompt: DF.Code
		tasks: DF.Table[AIAgentTaskAssignment]
		temperature: DF.Float
		timeout: DF.Duration | None
		use_temperature: DF.Check
	# end: auto-generated types

	def validate(self):
		self.validate_agent_key()
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
		self.validate_unique_rows("allowed_tools", "tool", _("Allowed Tool"))
		self.validate_dependencies()

	def validate_agent_key(self):
		validate_key(self.agent_key, _("Agent Key"))
		if not self.is_new():
			previous = self.get_doc_before_save()
			if previous and previous.agent_key != self.agent_key:
				frappe.throw(_("Agent Key cannot be changed after the AI Agent is created."))

	def validate_unique_rows(self, table_field: str, link_field: str, label: str):
		seen = set()
		for row in self.get(table_field):
			value = row.get(link_field)
			if value in seen:
				frappe.throw(_("{0} {1} is listed more than once.").format(label, frappe.bold(value)))
			seen.add(value)

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

		for tool_name in allowed_tools:
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
