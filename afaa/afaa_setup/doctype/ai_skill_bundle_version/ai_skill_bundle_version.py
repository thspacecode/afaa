# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class AISkillBundleVersion(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from afaa.afaa_setup.doctype.ai_skill_bundle_file.ai_skill_bundle_file import AISkillBundleFile

		bundle_digest: DF.Data
		file_count: DF.Int
		files: DF.Table[AISkillBundleFile]
		manifest: DF.Code
		skill: DF.Data
		total_bytes: DF.Int
		version_id: DF.Data
	# end: auto-generated types

	def validate(self):
		if not self.flags.afaa_bundle_internal:
			frappe.throw(
				_("AI Skill bundle versions are managed internally and immutable."), frappe.PermissionError
			)

	def before_rename(self, old_name, new_name, merge=False):
		frappe.throw(_("AI Skill bundle versions cannot be renamed."), frappe.PermissionError)

	def on_trash(self):
		if not self.flags.afaa_bundle_internal:
			frappe.throw(
				_("AI Skill bundle versions can only be removed by retention cleanup."),
				frappe.PermissionError,
			)
		if frappe.db.exists("AI Skill Bundle Reference", {"bundle_version": self.name}):
			frappe.throw(_("A retained AI Skill bundle version cannot be removed."), frappe.ValidationError)
