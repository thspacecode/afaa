# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document


class AISkillBundleFile(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		byte_length: DF.Int
		encoding: DF.Data
		file: DF.Link
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		relative_path: DF.Data
		sha256: DF.Data
	# end: auto-generated types

	pass
