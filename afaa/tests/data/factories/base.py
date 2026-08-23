import json
from collections.abc import Mapping
from typing import Any, ClassVar

import frappe
from frappe.model.document import Document
from frappe.tests.utils import whitelist_for_tests


class DocTypeFactory[T: Document]:
	"""Build, insert, and upsert test documents with overridable defaults."""

	doctype: ClassVar[str | None] = None
	password_fields: ClassVar[tuple[str, ...]] = ()

	@classmethod
	def defaults(cls) -> dict[str, Any]:
		return {}

	@classmethod
	def get_test_master_data(cls) -> dict[str, Any]:
		"""Return the normalized ``afaa_test_master_data`` site configuration."""
		raw_config = frappe.conf.get("afaa_test_master_data") or {}
		if not isinstance(raw_config, Mapping):
			frappe.throw("afaa_test_master_data in site_config.json must be a JSON object.")

		config = {"provider": "openai", "model": "afaa-test-model", **dict(raw_config)}
		provider = config.get("provider")
		if not isinstance(provider, str) or not provider.strip():
			frappe.throw("afaa_test_master_data.provider must be a non-empty string.")
		config["provider"] = provider.strip()

		model = config.get("model")
		if not isinstance(model, str) or not model.strip():
			frappe.throw("afaa_test_master_data.model must be a non-empty string.")
		config["model"] = model.strip()

		settings = config.get("account_settings")
		if isinstance(settings, str):
			try:
				settings = json.loads(settings)
			except json.JSONDecodeError as error:
				frappe.throw(f"afaa_test_master_data.account_settings is invalid JSON: {error}")
		if settings is not None and not isinstance(settings, Mapping):
			frappe.throw("afaa_test_master_data.account_settings must be a JSON object.")
		config["account_settings"] = dict(settings) if settings else None
		return config

	@classmethod
	def build(cls, **overrides: Any) -> T:
		if not cls.doctype:
			raise TypeError(f"{cls.__name__} must define 'doctype'")

		return frappe.get_doc(
			{
				"doctype": cls.doctype,
				**cls.defaults(),
				**overrides,
			}
		)

	@classmethod
	@whitelist_for_tests()
	def create(cls, *, ignore_permissions: bool = False, **overrides: Any) -> T:
		doc = cls.build(**overrides)
		doc.insert(ignore_permissions=ignore_permissions)
		return doc

	@classmethod
	def upsert(cls, name: str, *, ignore_permissions: bool = True, **overrides: Any) -> T:
		"""Create a named document or update it when factory values differ."""
		if not cls.doctype:
			raise TypeError(f"{cls.__name__} must define 'doctype'")

		values = {**cls.defaults(), **overrides}
		for field in cls.password_fields:
			if values.get(field) is None:
				values.pop(field, None)

		if not frappe.db.exists(cls.doctype, name):
			return cls.create(ignore_permissions=ignore_permissions, **values)

		doc = frappe.get_doc(cls.doctype, name)
		if any(not cls.value_matches(doc, field, value) for field, value in values.items()):
			doc.update(values)
			doc.save(ignore_permissions=ignore_permissions)
		return doc

	@classmethod
	def value_matches(cls, doc: T, field: str, expected: Any) -> bool:
		if field in cls.password_fields:
			return doc.get_password(field, raise_exception=False) == expected

		actual = doc.get(field)
		if not isinstance(expected, list):
			return actual == expected
		if len(actual or []) != len(expected):
			return False
		return all(
			all(actual_row.get(key) == value for key, value in expected_row.items())
			for actual_row, expected_row in zip(actual, expected, strict=True)
		)
