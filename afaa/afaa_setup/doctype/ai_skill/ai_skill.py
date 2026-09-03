# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from afaa.utils.data import validate_key


class AISkill(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from afaa.afaa_setup.doctype.ai_skill_tool.ai_skill_tool import AISkillTool

		description: DF.SmallText | None
		disabled: DF.Check
		instructions: DF.Code
		required_tools: DF.Table[AISkillTool]
		skill_key: DF.Data
		skill_name: DF.Data
	# end: auto-generated types

	def validate(self):
		validate_key(self.skill_key, _("Skill Key"))
		self.validate_tools()

	def validate_tools(self):
		seen = set()
		for row in self.required_tools:
			if row.tool in seen:
				frappe.throw(_("Tool {0} is listed more than once.").format(frappe.bold(row.tool)))
			seen.add(row.tool)
			if not self.disabled and not frappe.db.get_value("AI Tool", row.tool, "available"):
				frappe.throw(_("AI Tool {0} is unavailable.").format(frappe.bold(row.tool)))
