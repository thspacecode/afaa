# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from afaa.utils.data import validate_key


class AISkillTag(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		description: DF.SmallText | None
		disabled: DF.Check
		tag_key: DF.Data
		tag_name: DF.Data
	# end: auto-generated types

	def validate(self):
		validate_key(self.tag_key, _("Tag Key"))
		stored_key = None if self.is_new() else frappe.db.get_value(self.doctype, self.name, "tag_key")
		if stored_key and stored_key != self.tag_key:
			frappe.throw(_("Tag Key cannot be changed after creation."), frappe.PermissionError)
