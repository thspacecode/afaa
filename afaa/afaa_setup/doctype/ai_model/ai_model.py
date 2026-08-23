# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from afaa.ai.provider import build_model, get_provider_account, get_provider_class, make_model_name


class AIModel(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		available: DF.Check
		disabled: DF.Check
		model_id: DF.Data
		model_name: DF.Data
		provider: DF.Link
		supports_tools: DF.Check
	# end: auto-generated types

	def before_naming(self):
		self.model_name = make_model_name(self.provider, self.model_id)

	def validate(self):
		self.model_name = make_model_name(self.provider, self.model_id)
		self.validate_unique_model()
		self.validate_provider()
		self.validate_availability()
		self.validate_registry_fields()

	def validate_unique_model(self):
		filters = {"provider": self.provider, "model_id": self.model_id}
		if not self.is_new():
			filters["name"] = ["!=", self.name]
		if frappe.db.exists("AI Model", filters):
			frappe.throw(
				_("Model ID {0} already exists for this provider.").format(frappe.bold(self.model_id))
			)

	def validate_provider(self):
		provider = frappe.get_doc("AI Provider", self.provider)
		if not self.disabled and provider.disabled:
			frappe.throw(_("AI Provider {0} is disabled.").format(frappe.bold(provider.name)))
		if not self.disabled:
			get_provider_class(provider.provider_type)

	def validate_availability(self):
		if not self.disabled and not self.available:
			frappe.throw(_("An unavailable AI Model cannot be enabled."))

	def validate_registry_fields(self):
		if self.is_new():
			return
		previous = self.get_doc_before_save()
		if previous and (previous.provider != self.provider or previous.model_id != self.model_id):
			frappe.throw(_("Provider and Model ID cannot be changed after an AI Model is created."))

	@frappe.whitelist()
	def test_model(self, provider_account=None):
		frappe.only_for(["AI Manager", "System Manager"])
		from pydantic_ai import Agent

		if not self.available:
			frappe.throw(_("AI Model {0} is unavailable.").format(frappe.bold(self.name)))

		account = get_provider_account(self.provider, provider_account)
		try:
			result = Agent(build_model(self, account), output_type=str).run_sync(
				"Reply with only the word OK.",
				model_settings={"timeout": 60},
			)
		except Exception as error:
			frappe.log_error(title=_("AI model connection test failed"), message=frappe.get_traceback())
			return {"ok": False, "message": str(error)}

		return {"ok": True, "message": str(result.output)[:500]}
