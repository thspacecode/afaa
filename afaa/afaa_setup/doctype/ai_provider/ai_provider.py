# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from afaa.ai.provider import get_provider_class, is_provider_available


class AIProvider(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Password | None
		disabled: DF.Check
		provider_name: DF.Data
		provider_settings: DF.JSON | None
		provider_type: DF.Autocomplete
	# end: auto-generated types

	def before_naming(self):
		self.provider_name = self.provider_type

	def validate(self):
		self.provider_name = self.provider_type
		self.validate_provider_type()

	def validate_provider_type(self):
		provider_class = get_provider_class(self.provider_type, require_available=False)
		if not self.is_new():
			previous = self.get_doc_before_save()
			if previous and previous.provider_type != self.provider_type:
				frappe.throw(_("Provider Type cannot be changed after the provider is created."))

		if not self.disabled and not is_provider_available(provider_class):
			get_provider_class(self.provider_type, require_available=True)
