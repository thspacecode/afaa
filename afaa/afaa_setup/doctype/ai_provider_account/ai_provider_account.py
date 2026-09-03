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
		authentication_method: DF.Literal["API Key", "OAuth Subscription"]
		connected_app: DF.Link | None
		connected_user: DF.Link | None
		disabled: DF.Check
		external_account_email: DF.Data | None
		external_account_id: DF.Data | None
		oauth_connected_on: DF.Datetime | None
		oauth_status: DF.Literal["Not Connected", "Connected", "Expired", "Error"]
		provider: DF.Link
		subscription_plan: DF.Data | None
	# end: auto-generated types

	def before_validate(self):
		if not self.provider:
			return
		provider = frappe.get_doc("AI Provider", self.provider)
		provider_class = get_provider_class(provider.provider_type, require_available=False)
		if len(provider_class.supported_auth_methods) == 1:
			self.authentication_method = provider_class.supported_auth_methods[0]
		if provider.provider_type == "openai_codex" and not self.connected_app:
			from afaa.ai.oauth.openai_codex import CONNECTED_APP_NAME

			if frappe.db.exists("Connected App", CONNECTED_APP_NAME):
				self.connected_app = CONNECTED_APP_NAME

	def validate(self):
		parse_json_object(self.account_settings, _("Account Settings"))
		provider = frappe.get_doc("AI Provider", self.provider)
		provider_class = get_provider_class(provider.provider_type, require_available=False)
		if not self.disabled and provider.disabled:
			frappe.throw(_("AI Provider {0} is disabled.").format(frappe.bold(provider.name)))
		if not self.disabled and not is_provider_available(provider_class):
			get_provider_class(provider.provider_type, require_available=True)

		provider_class(self).validate_account()

		if not self.is_new():
			previous = self.get_doc_before_save()
			if previous and previous.provider != self.provider:
				frappe.throw(_("Provider cannot be changed after the account is created."))

	@frappe.whitelist()
	def get_authentication_status(self):
		frappe.only_for(["AI Manager", "System Manager"])
		provider = frappe.get_doc("AI Provider", self.provider)
		adapter = get_provider_class(provider.provider_type)(self)
		return {
			"authenticated": adapter.is_authenticated(),
			"status": adapter.get_authentication_status(),
			"authentication_method": self.authentication_method,
			"supports_oauth_connection": adapter.supports_oauth_connection,
		}

	@frappe.whitelist(methods=["POST"])
	def start_oauth_login(self):
		frappe.only_for(["AI Manager", "System Manager"])
		from afaa.ai.oauth.openai_codex import start_oauth_login

		return start_oauth_login(self)

	@frappe.whitelist(methods=["POST"])
	def poll_oauth_login(self, flow_id: str):
		frappe.only_for(["AI Manager", "System Manager"])
		from afaa.ai.oauth.openai_codex import poll_oauth_login

		return poll_oauth_login(self, flow_id)

	@frappe.whitelist(methods=["POST"])
	def disconnect_oauth(self):
		frappe.only_for(["AI Manager", "System Manager"])
		provider = frappe.get_doc("AI Provider", self.provider)
		adapter = get_provider_class(provider.provider_type)(self)
		if not adapter.supports_oauth_connection:
			frappe.throw(_("This AI Provider Account does not support OAuth connections."))
		adapter.disconnect()
		return {"status": "disconnected"}

	@frappe.whitelist()
	def sync_models(self):
		frappe.only_for(["AI Manager", "System Manager"])
		if self.disabled:
			frappe.throw(_("Enable this AI Provider Account before synchronizing models."))
		provider = frappe.get_doc("AI Provider", self.provider)
		adapter = get_provider_class(provider.provider_type)(self)
		if not adapter.is_authenticated():
			frappe.throw(_("Authenticate this AI Provider Account before synchronizing models."))
		savepoint = "ai_provider_account_model_sync"
		frappe.db.savepoint(savepoint)
		try:
			report = sync_provider_models(self)
		except Exception as error:
			frappe.db.rollback(save_point=savepoint)
			frappe.log_error(title=_("AI provider model sync failed"), message=frappe.get_traceback())
			return {"ok": False, "message": str(error)}

		return {"ok": True, **report}
