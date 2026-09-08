# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class AISkillBundleReference(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bundle_version: DF.Link
		reference_doctype: DF.Data
		reference_id: DF.Data
		reference_key: DF.Data
		reference_name: DF.Data
	# end: auto-generated types

	def validate(self):
		if not self.flags.afaa_bundle_internal:
			frappe.throw(
				_("AI Skill bundle retention references are managed internally."), frappe.PermissionError
			)

	def before_rename(self, old_name, new_name, merge=False):
		frappe.throw(_("AI Skill bundle retention references cannot be renamed."), frappe.PermissionError)

	def on_trash(self):
		if not self.flags.afaa_bundle_internal:
			frappe.throw(
				_("AI Skill bundle retention references are managed internally."), frappe.PermissionError
			)
