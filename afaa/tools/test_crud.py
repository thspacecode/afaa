# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from unittest.mock import MagicMock, patch

import frappe

from afaa.ai.tools import get_tool_definition
from afaa.tests.utils import AFAATestSuite
from afaa.tools.crud import (
	CreateDocumentInput,
	DocumentActionInput,
	UpdateDocumentInput,
	frappe_cancel_doc,
	frappe_create_doc,
	frappe_delete_doc,
	frappe_submit_doc,
	frappe_update_doc,
)


class IntegrationTestCrudTools(AFAATestSuite):
	def test_crud_tools_are_registered_with_stable_keys(self):
		for tool_key in (
			"frappe_create_doc",
			"frappe_update_doc",
			"frappe_delete_doc",
			"frappe_submit_doc",
			"frappe_cancel_doc",
		):
			definition = get_tool_definition(tool_key)
			self.assertIsNotNone(definition)
			self.assertEqual(definition.key, tool_key)
			self.assertEqual(definition.source_app, "afaa")
			self.assertEqual(definition.input_schema["type"], "object")
			self.assertIn("type", definition.output_schema)

	def test_create_update_and_delete_document(self):
		description = f"AFAA CRUD tool test {frappe.generate_hash(length=8)}"
		created = frappe_create_doc(CreateDocumentInput(doctype="ToDo", values={"description": description}))
		name = created.root["name"]

		self.assertEqual(created.root["description"], description)

		updated = frappe_update_doc(
			UpdateDocumentInput(doctype="ToDo", name=name, values={"description": f"{description} updated"})
		)
		self.assertEqual(updated.root["description"], f"{description} updated")

		self.assertEqual(
			frappe_delete_doc(DocumentActionInput(doctype="ToDo", name=name)).model_dump(),
			{"doctype": "ToDo", "name": name, "deleted": True},
		)
		self.assertFalse(frappe.db.exists("ToDo", name))

	def test_mutations_reject_document_control_fields(self):
		with self.assertRaises(frappe.ValidationError):
			frappe_create_doc(
				CreateDocumentInput(doctype="ToDo", values={"description": "Invalid", "doctype": "User"})
			)

		with self.assertRaises(frappe.ValidationError):
			frappe_update_doc(
				UpdateDocumentInput(
					doctype="ToDo", name="missing", values={"flags": {"ignore_permissions": True}}
				)
			)

	def test_submit_and_cancel_use_document_api(self):
		doc = MagicMock()
		doc.as_dict.return_value = {"doctype": "Test Document", "name": "TEST-1"}

		with patch("afaa.tools.crud.frappe.get_doc", return_value=doc):
			self.assertEqual(
				frappe_submit_doc(DocumentActionInput(doctype="Test Document", name="TEST-1")).root,
				{"doctype": "Test Document", "name": "TEST-1"},
			)
			doc.submit.assert_called_once_with()
			doc.check_permission.assert_called_with("read")

			doc.reset_mock()
			doc.as_dict.return_value = {"doctype": "Test Document", "name": "TEST-1"}
			self.assertEqual(
				frappe_cancel_doc(DocumentActionInput(doctype="Test Document", name="TEST-1")).root,
				{"doctype": "Test Document", "name": "TEST-1"},
			)
			doc.cancel.assert_called_once_with()
			doc.check_permission.assert_called_with("read")
