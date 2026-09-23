# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document

from afaa.ai.prompts import parse_json_object
from afaa.ai.tools import get_tool_definition
from afaa.utils.data import validate_key


class AITool(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		available: DF.Check
		description: DF.SmallText
		disabled: DF.Check
		input_schema: DF.JSON | None
		method: DF.Data | None
		output_schema: DF.JSON | None
		source_app: DF.Data | None
		tool_key: DF.Data
		tool_name: DF.Data
	# end: auto-generated types

	def validate(self):
		validate_key(self.tool_key, _("Tool Key"))
		self.apply_tool_definition()

	def apply_tool_definition(self):
		from afaa.ai.agent_levels import RUNTIME_TOOL_SOURCE_APP, runtime_tool_definition

		runtime_definition = runtime_tool_definition(self.tool_key)
		if runtime_definition is not None:
			# Reserved tools implemented by Porch Agent's workspace runtime. They
			# are never executed in-process here; the record only makes the key
			# selectable for Level 2 (Sub Agent) allowed tools.
			if self.source_app and self.source_app != RUNTIME_TOOL_SOURCE_APP:
				frappe.throw(
					_("Tool {0} is reserved for the workspace runtime.").format(frappe.bold(self.tool_key))
				)
			self.tool_name = runtime_definition["tool_name"]
			self.description = runtime_definition["description"]
			self.source_app = RUNTIME_TOOL_SOURCE_APP
			self.method = runtime_definition["method"]
			self.input_schema = json.dumps(
				runtime_definition["input_schema"], indent=2, sort_keys=True
			)
			self.output_schema = json.dumps(
				runtime_definition["output_schema"], indent=2, sort_keys=True
			)
			self.available = 1
			return

		definition = get_tool_definition(self.tool_key)
		if not definition:
			self.available = 0
			if self.is_new() or not self.disabled:
				frappe.throw(
					_("Tool {0} is not registered by an installed app.").format(frappe.bold(self.tool_key))
				)
			parse_json_object(self.input_schema, _("Input Schema"))
			parse_json_object(self.output_schema, _("Output Schema"))
			return

		self.tool_name = definition.name
		self.description = definition.description
		self.source_app = definition.source_app
		self.method = definition.method
		self.input_schema = json.dumps(definition.input_schema, indent=2, sort_keys=True)
		self.output_schema = json.dumps(definition.output_schema, indent=2, sort_keys=True)
		self.available = 1
