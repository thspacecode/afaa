# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import keyword
import re

import frappe
from frappe import _
from frappe.model.document import Document

from afaa.ai.outputs import OUTPUT_FIELD_TYPES, AIExecutionOutput
from afaa.ai.prompts import validate_jinja_template
from afaa.utils.data import validate_key

FIELD_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class AITaskDefinition(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from afaa.afaa_setup.doctype.ai_task_output_field.ai_task_output_field import AITaskOutputField
		from afaa.afaa_setup.doctype.ai_task_tool.ai_task_tool import AITaskTool

		allow_soft_failure: DF.Check
		description: DF.SmallText
		disabled: DF.Check
		expected_output: DF.Table[AITaskOutputField]
		instructions: DF.Code
		required_tools: DF.Table[AITaskTool]
		task_key: DF.Data
		task_name: DF.Data
	# end: auto-generated types

	def validate(self):
		self.validate_task_key()
		validate_jinja_template(self.instructions)
		self.validate_tools()
		self.validate_expected_output()
		self.validate_disabled_assignments()

	def validate_task_key(self):
		validate_key(self.task_key, _("Task Key"))
		if not self.is_new():
			previous = self.get_doc_before_save()
			if previous and previous.task_key != self.task_key:
				frappe.throw(_("Task Key cannot be changed after the AI Task Definition is created."))

	def validate_tools(self):
		seen = set()
		for row in self.required_tools:
			if row.tool in seen:
				frappe.throw(_("Tool {0} is listed more than once.").format(frappe.bold(row.tool)))
			seen.add(row.tool)
			if not self.disabled and not frappe.db.get_value("AI Tool", row.tool, "available"):
				frappe.throw(_("AI Tool {0} is unavailable.").format(frappe.bold(row.tool)))

	def validate_expected_output(self):
		if not self.expected_output:
			frappe.throw(_("Expected Output must contain at least one field."))

		seen = set()
		for row in self.expected_output:
			field_name = row.field_name or ""
			if not FIELD_NAME_PATTERN.fullmatch(field_name) or keyword.iskeyword(field_name):
				frappe.throw(
					_("Output field {0} must be a valid lowercase Python field name.").format(
						frappe.bold(field_name)
					)
				)
			if hasattr(AIExecutionOutput, field_name):
				frappe.throw(
					_("Output field {0} conflicts with the base output model.").format(
						frappe.bold(field_name)
					)
				)
			if field_name in seen:
				frappe.throw(_("Output field {0} is listed more than once.").format(frappe.bold(field_name)))
			seen.add(field_name)

			if row.field_type not in OUTPUT_FIELD_TYPES:
				frappe.throw(_("Unsupported output field type: {0}").format(row.field_type))

	def validate_disabled_assignments(self):
		if not self.disabled or self.is_new():
			return
		agents = frappe.get_all(
			"AI Agent Task Assignment",
			filters={"task": self.name, "parenttype": "AI Agent", "parentfield": "tasks"},
			pluck="parent",
		)
		active_agents = (
			frappe.get_all(
				"AI Agent",
				filters={"name": ("in", agents), "disabled": 0},
				pluck="name",
			)
			if agents
			else []
		)
		if active_agents:
			frappe.throw(
				_("Cannot disable a task assigned to enabled AI Agents: {0}").format(
					", ".join(sorted(active_agents))
				)
			)
