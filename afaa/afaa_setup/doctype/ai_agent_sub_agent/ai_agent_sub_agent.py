# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

# import frappe
from frappe.model.document import Document

# from frappe import _


class AIAgentSubAgent(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		max_calls: DF.Int
		sub_agent: DF.Link
		timeout_seconds: DF.Int
	# end: auto-generated types

	pass
