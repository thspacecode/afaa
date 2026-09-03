# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import frappe
from frappe import _
from frappe.model.document import Document


class BaseProvider(ABC):
	key: ClassVar[str]
	label: ClassVar[str]
	required_distributions: ClassVar[tuple[str, ...]] = ()
	source_app: ClassVar[str | None] = None
	supported_auth_methods: ClassVar[tuple[str, ...]] = ("API Key",)
	supports_oauth_connection: ClassVar[bool] = False
	model_catalog_is_account_specific: ClassVar[bool] = False

	def __init__(self, provider_account_doc):
		self.provider_account_doc = provider_account_doc

	@abstractmethod
	def build_model(self, model_doc: Document):
		pass

	@abstractmethod
	def list_models(self) -> list[str]:
		pass

	def validate_account(self) -> None:
		authentication_method = self.provider_account_doc.authentication_method or "API Key"
		if authentication_method not in self.supported_auth_methods:
			frappe.throw(
				_("Authentication method {0} is not supported by {1}.").format(
					frappe.bold(authentication_method), frappe.bold(self.label)
				)
			)

	def is_authenticated(self) -> bool:
		return bool(self.get_api_key())

	def get_authentication_status(self) -> str:
		return "Connected" if self.is_authenticated() else "Not Connected"

	def resolve_access_token(self) -> str:
		token = self.get_api_key()
		if not token:
			frappe.throw(
				_("AI Provider Account {0} is not authenticated.").format(
					frappe.bold(self.provider_account_doc.name)
				)
			)
		return token

	def disconnect(self) -> None:
		frappe.throw(_("Provider {0} does not support OAuth connections.").format(frappe.bold(self.label)))

	def prepare_model_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
		"""Apply provider constraints to model settings exposed to the runtime."""
		return settings

	def get_api_key(self) -> str | None:
		return self.provider_account_doc.get_password("api_key", raise_exception=False) or None
