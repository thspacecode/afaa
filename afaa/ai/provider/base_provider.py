# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from abc import ABC, abstractmethod
from typing import ClassVar

from frappe.model.document import Document


class BaseProvider(ABC):
	key: ClassVar[str]
	label: ClassVar[str]
	required_distributions: ClassVar[tuple[str, ...]] = ()
	source_app: ClassVar[str | None] = None

	def __init__(self, provider_account_doc):
		self.provider_account_doc = provider_account_doc

	@abstractmethod
	def build_model(self, model_doc: Document):
		pass

	@abstractmethod
	def list_models(self) -> list[str]:
		pass

	def get_api_key(self) -> str | None:
		return self.provider_account_doc.get_password("api_key", raise_exception=False) or None
