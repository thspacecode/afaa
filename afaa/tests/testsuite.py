import unittest
import uuid
from collections.abc import Callable, Iterator, MutableMapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import frappe
from frappe.tests.utils import load_test_records_for

if TYPE_CHECKING:
	from frappe.model.document import Document


class AFAATestSuite(unittest.TestCase):
	"""Base test suite that avoids importing ERPNext's test bootstrap data."""

	@classmethod
	def registerAs(
		cls, _as: Callable[[Callable[..., Any]], Any]
	) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
		def decorator(cm_func: Callable[..., Any]) -> Callable[..., Any]:
			setattr(cls, cm_func.__name__, _as(cm_func))
			return cm_func

		return decorator

	@classmethod
	def setUpClass(cls) -> None:
		cls.globalTestRecords = {}

	def tearDown(self) -> None:
		frappe.db.rollback()
		frappe.local.request_cache.clear()
		if hasattr(frappe.local, "future_sle"):
			frappe.local.future_sle.clear()

	def load_test_records(self, doctype: str) -> None:
		if doctype not in self.globalTestRecords:
			records = load_test_records_for(doctype)
			self.globalTestRecords[doctype] = records[doctype]

	@staticmethod
	def insert_doc(doc_dict: dict[str, Any]) -> "Document":
		doc = frappe.new_doc(doctype=doc_dict.get("doctype"))
		doc.update(doc_dict)
		doc.save()
		return doc

	@contextmanager
	def set_user(self, user: str) -> Iterator[None]:
		try:
			old_user = frappe.session.user
			frappe.set_user(user)
			yield
		finally:
			frappe.set_user(old_user)

	@contextmanager
	def set_flags(self, **flags: Any) -> Iterator[None]:
		"""Temporarily set ``frappe.flags``, restoring previous values on exit."""
		previous = {name: frappe.flags.get(name) for name in flags}
		try:
			frappe.flags.update(flags)
			yield
		finally:
			frappe.flags.update(previous)

	@contextmanager
	def set_create_user(self, roles: list[str] | None = None) -> Iterator[str]:
		"""Create and activate an ephemeral user for the duration of the block."""
		email = f"test_user_{uuid.uuid4().hex[:12]}@afaa.test"
		user = frappe.new_doc("User")
		user.email = email
		user.first_name = "Test"
		user.send_welcome_email = 0
		for role in roles or []:
			user.append("roles", {"role": role})
		user.insert(ignore_permissions=True)

		with self.set_user(email):
			yield email


@AFAATestSuite.registerAs(staticmethod)
@contextmanager
def change_settings(
	doctype: str,
	settings_dict: MutableMapping[str, Any] | None = None,
	/,
	*,
	docname: str | None = None,
	**settings: Any,
) -> Iterator[None]:
	"""Temporarily change fields on a single or named document."""
	import copy

	if settings_dict is None:
		settings_dict = settings

	document = frappe.get_doc(doctype, docname) if docname else frappe.get_doc(doctype)
	previous_settings = copy.deepcopy(settings_dict)
	for key in previous_settings:
		previous_settings[key] = getattr(document, key)

	for key, value in settings_dict.items():
		setattr(document, key, value)
	document.save(ignore_permissions=True)

	try:
		yield
	finally:
		document = frappe.get_doc(doctype, docname) if docname else frappe.get_doc(doctype)
		for key, value in previous_settings.items():
			setattr(document, key, value)
		document.save(ignore_permissions=True)
