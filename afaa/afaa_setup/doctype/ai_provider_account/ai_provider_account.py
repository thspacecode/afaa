# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from afaa.ai.prompts import parse_json_object
from afaa.ai.provider import get_provider_class, is_provider_available, sync_provider_models


class AIProviderAccount(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		account_name: DF.Data
		account_settings: DF.JSON | None
		api_key: DF.Password | None
		disabled: DF.Check
		provider: DF.Link
	# end: auto-generated types

	def validate(self):
		parse_json_object(self.account_settings, _("Account Settings"))
		provider = frappe.get_doc("AI Provider", self.provider)
		provider_class = get_provider_class(provider.provider_type, require_available=False)
		if not self.disabled and provider.disabled:
			frappe.throw(_("AI Provider {0} is disabled.").format(frappe.bold(provider.name)))
		if not self.disabled and not is_provider_available(provider_class):
			get_provider_class(provider.provider_type, require_available=True)

		if not self.is_new():
			previous = self.get_doc_before_save()
			if previous and previous.provider != self.provider:
				frappe.throw(_("Provider cannot be changed after the account is created."))

	@frappe.whitelist()
	def sync_models(self):
		frappe.only_for(["AI Manager", "System Manager"])
		if self.disabled:
			frappe.throw(_("Enable this AI Provider Account before synchronizing models."))
		savepoint = "ai_provider_account_model_sync"
		frappe.db.savepoint(savepoint)
		try:
			report = sync_provider_models(self)
		except Exception as error:
			frappe.db.rollback(save_point=savepoint)
			frappe.log_error(title=_("AI provider model sync failed"), message=frappe.get_traceback())
			return {"ok": False, "message": str(error)}

		return {"ok": True, **report}
