# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from afaa.utils.data import validate_key, validate_unique_rows


class AISkill(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from afaa.afaa_setup.doctype.ai_skill_tag_link.ai_skill_tag_link import AISkillTagLink
		from afaa.afaa_setup.doctype.ai_skill_tool.ai_skill_tool import AISkillTool

		description: DF.SmallText | None
		disabled: DF.Check
		instructions: DF.Code
		required_tools: DF.Table[AISkillTool]
		skill_key: DF.Data
		skill_name: DF.Data
		tags: DF.TableMultiSelect[AISkillTagLink]
	# end: auto-generated types

	def validate(self):
		validate_key(self.skill_key, _("Skill Key"))
		stored_key = None if self.is_new() else frappe.db.get_value(self.doctype, self.name, "skill_key")
		if stored_key and stored_key != self.skill_key:
			frappe.throw(_("Skill Key cannot be changed after creation."), frappe.PermissionError)
		validate_unique_rows(self.tags, "tag", _("Skill Tag"))
		self.validate_tools()

	def after_insert(self):
		from afaa.ai.skill_bundles import ensure_skill_bundle_root

		ensure_skill_bundle_root(self.name)

	def before_rename(self, old_name, new_name, merge=False):
		frappe.throw(
			_("AI Skills cannot be renamed because Skill Key is a stable identity."), frappe.PermissionError
		)

	def validate_tools(self):
		seen = set()
		for row in self.required_tools:
			if row.tool in seen:
				frappe.throw(_("Tool {0} is listed more than once.").format(frappe.bold(row.tool)))
			seen.add(row.tool)
			if not self.disabled and not frappe.db.get_value("AI Tool", row.tool, "available"):
				frappe.throw(_("AI Tool {0} is unavailable.").format(frappe.bold(row.tool)))
