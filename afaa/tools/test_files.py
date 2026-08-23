# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import base64
from pathlib import Path

import frappe

from afaa.ai.tools import get_tool_definition
from afaa.tests.utils import AFAATestSuite
from afaa.tools.files import (
	AttachFileInput,
	UploadFileInput,
	frappe_attach_file,
	frappe_upload_file,
)


class IntegrationTestFileTools(AFAATestSuite):
	def test_file_tools_are_registered_with_stable_keys(self):
		for tool_key in ("frappe_upload_file", "frappe_attach_file"):
			definition = get_tool_definition(tool_key)
			self.assertIsNotNone(definition)
			self.assertEqual(definition.key, tool_key)
			self.assertEqual(definition.source_app, "afaa")
			self.assertEqual(definition.input_schema["type"], "object")
			self.assertIn("type", definition.output_schema)

	def test_upload_text_file_and_attach_it_to_document(self):
		content = f"AFAA file tool test {frappe.generate_hash(length=8)}"
		uploaded = frappe_upload_file(
			UploadFileInput(file_name="afaa-tool-test.txt", content=content, is_private=True)
		)
		source_file = frappe.get_doc("File", uploaded.name)

		self.assertEqual(source_file.get_content(), content)
		self.assertTrue(uploaded.is_private)
		self.assertIsNone(uploaded.attached_to_doctype)

		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": f"AFAA attachment target {frappe.generate_hash(length=8)}",
			}
		).insert()
		attached = frappe_attach_file(
			AttachFileInput(file_id=uploaded.name, doctype=todo.doctype, name=todo.name)
		)

		self.assertNotEqual(attached.name, uploaded.name)
		self.assertEqual(attached.file_url, uploaded.file_url)
		self.assertEqual(attached.attached_to_doctype, todo.doctype)
		self.assertEqual(attached.attached_to_name, todo.name)
		self.assertIsNone(frappe.db.get_value("File", uploaded.name, "attached_to_doctype"))

	def test_upload_accepts_base64_and_data_uris(self):
		content = b"AFAA binary-safe content\xff\x00"
		for prefix in ("", "data:application/octet-stream;base64,"):
			uploaded = frappe_upload_file(
				UploadFileInput(
					file_name=f"afaa-base64-{frappe.generate_hash(length=6)}.bin",
					content=prefix + base64.b64encode(content).decode(),
					content_encoding="base64",
				)
			)
			file_doc = frappe.get_doc("File", uploaded.name)
			self.assertEqual(Path(file_doc.get_full_path()).read_bytes(), content)

	def test_upload_rejects_invalid_or_empty_base64(self):
		for content in ("not valid base64!", "data:text/plain;base64,"):
			with self.assertRaises(frappe.ValidationError):
				frappe_upload_file(
					UploadFileInput(
						file_name="invalid.txt",
						content=content,
						content_encoding="base64",
					)
				)

	def test_attach_rejects_non_attachment_field(self):
		uploaded = frappe_upload_file(UploadFileInput(file_name="afaa-invalid-field.txt", content="content"))
		todo = frappe.get_doc(
			{
				"doctype": "ToDo",
				"description": f"AFAA attachment target {frappe.generate_hash(length=8)}",
			}
		).insert()

		with self.assertRaises(frappe.ValidationError):
			frappe_attach_file(
				AttachFileInput(
					file_id=uploaded.name,
					doctype=todo.doctype,
					name=todo.name,
					fieldname="description",
				)
			)
