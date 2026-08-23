# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe

from afaa.ai.tools import get_tool_definition
from afaa.tests.utils import AFAATestSuite
from afaa.tools.fetch import (
	CountDocumentsInput,
	GetDocTypeSchemaInput,
	GetDocumentInput,
	GetDocumentListInput,
	frappe_get_count,
	frappe_get_doc,
	frappe_get_doctype_schema,
	frappe_get_list,
)


class IntegrationTestFetchTools(AFAATestSuite):
	def test_fetch_tools_are_registered_with_stable_keys(self):
		for tool_key in (
			"frappe_get_doctype_schema",
			"frappe_get_list",
			"frappe_get_doc",
			"frappe_get_count",
		):
			definition = get_tool_definition(tool_key)
			self.assertIsNotNone(definition)
			self.assertEqual(definition.key, tool_key)
			self.assertEqual(definition.source_app, "afaa")
			self.assertEqual(definition.input_schema["type"], "object")
			self.assertIn("type", definition.output_schema)

	def test_doctype_schema_only_returns_readable_non_password_fields(self):
		schema = frappe_get_doctype_schema(GetDocTypeSchemaInput(doctype="User"))
		fields = {field.fieldname: field for field in schema.fields}

		self.assertEqual(schema.doctype, "User")
		self.assertIn("email", fields)
		self.assertNotIn("new_password", fields)
		self.assertIn("read", schema.permissions)

	def test_get_list_caps_page_length(self):
		with patch("afaa.tools.fetch.frappe.get_list", return_value=[]) as get_list:
			frappe_get_list(GetDocumentListInput(doctype="ToDo", limit_start=-5, limit_page_length=1000))

		get_list.assert_called_once_with(
			"ToDo",
			fields=["name", "modified"],
			filters=None,
			or_filters=None,
			order_by=None,
			start=0,
			limit=100,
		)

	def test_get_document_list_and_count(self):
		description = f"AFAA fetch tool test {frappe.generate_hash(length=8)}"
		doc = frappe.get_doc({"doctype": "ToDo", "description": description}).insert()

		fetched = frappe_get_doc(GetDocumentInput(doctype="ToDo", name=doc.name))
		self.assertEqual(fetched.root["description"], description)

		listed = frappe_get_list(
			GetDocumentListInput(doctype="ToDo", fields=["name"], filters={"name": doc.name})
		)
		self.assertEqual(listed.root, [{"name": doc.name}])
		self.assertEqual(
			frappe_get_count(CountDocumentsInput(doctype="ToDo", filters={"name": doc.name})).root,
			1,
		)
