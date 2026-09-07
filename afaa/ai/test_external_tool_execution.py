# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from pydantic import BaseModel, ConfigDict, ValidationError

from afaa.ai.external_tool_execution import execute_external_tool
from afaa.ai.tools import EXTERNAL_READ_TOOL_METHODS, tool
from afaa.tests.utils import AFAATestSuite


class ProxyToolInput(BaseModel):
	model_config = ConfigDict(extra="forbid")

	value: int


class ProxyToolOutput(BaseModel):
	value: int
	user: str


proxy_calls = []


@tool(key="frappe_get_count", name="Proxy Test Tool")
def proxy_test_tool(request: ProxyToolInput) -> ProxyToolOutput:
	"""Execute the test tool as the active session user."""
	proxy_calls.append(request)
	return ProxyToolOutput(value=request.value, user=frappe.session.user)


@tool(key="frappe_get_count", name="Invalid Proxy Test Tool")
def invalid_result_tool(request: ProxyToolInput) -> ProxyToolOutput:
	"""Return an invalid result for output-boundary tests."""
	return {"value": request.value}


class TestExternalToolExecution(AFAATestSuite):
	def setUp(self):
		self.original_user = frappe.session.user
		frappe.set_user("Administrator")
		self.original_definition = proxy_test_tool.__afaa_tool_definition__
		self.definition = replace(
			self.original_definition,
			method=EXTERNAL_READ_TOOL_METHODS["frappe_get_count"],
		)
		proxy_test_tool.__afaa_tool_definition__ = self.definition
		self.enabled_status = SimpleNamespace(disabled=0, available=1)
		proxy_calls.clear()

	def tearDown(self):
		proxy_test_tool.__afaa_tool_definition__ = self.original_definition
		frappe.set_user(self.original_user)
		super().tearDown()

	def test_validates_arguments_executes_as_session_user_and_serializes_output(self):
		with (
			patch("afaa.ai.external_tool_execution.get_tool_definition", return_value=self.definition),
			patch("afaa.ai.external_tool_execution.frappe.db.get_value", return_value=self.enabled_status),
			patch("afaa.ai.external_tool_execution.frappe.get_attr", return_value=proxy_test_tool),
		):
			result = execute_external_tool("frappe_get_count", {"value": 3})

		self.assertEqual(result, {"value": 3, "user": "Administrator"})

	def test_rejects_invalid_arguments_before_calling_tool(self):
		with (
			patch("afaa.ai.external_tool_execution.get_tool_definition", return_value=self.definition),
			patch("afaa.ai.external_tool_execution.frappe.db.get_value", return_value=self.enabled_status),
			patch("afaa.ai.external_tool_execution.frappe.get_attr", return_value=proxy_test_tool),
			self.assertRaises(ValidationError),
		):
			execute_external_tool("frappe_get_count", {"value": 3, "unexpected": True})
		self.assertEqual(proxy_calls, [])

	def test_rejects_invalid_tool_output(self):
		original_definition = invalid_result_tool.__afaa_tool_definition__
		definition = replace(
			original_definition,
			method=EXTERNAL_READ_TOOL_METHODS["frappe_get_count"],
		)
		with (
			patch.object(invalid_result_tool, "__afaa_tool_definition__", definition),
			patch("afaa.ai.external_tool_execution.get_tool_definition", return_value=definition),
			patch("afaa.ai.external_tool_execution.frappe.db.get_value", return_value=self.enabled_status),
			patch("afaa.ai.external_tool_execution.frappe.get_attr", return_value=invalid_result_tool),
			self.assertRaises(ValidationError),
		):
			execute_external_tool("frappe_get_count", {"value": 3})

	def test_rejects_mutating_or_arbitrary_method_keys_before_registry_lookup(self):
		for tool_key in ("frappe_create_doc", "frappe.delete_doc"):
			with (
				patch("afaa.ai.external_tool_execution.get_tool_definition") as get_definition,
				self.assertRaisesRegex(frappe.ValidationError, "not supported"),
			):
				execute_external_tool(tool_key, {})
			get_definition.assert_not_called()

	def test_rejects_unregistered_disabled_and_unavailable_tools(self):
		for definition in (None, replace(self.definition, method="custom.tools.spoofed_read")):
			with (
				patch("afaa.ai.external_tool_execution.get_tool_definition", return_value=definition),
				self.assertRaisesRegex(frappe.ValidationError, "approved read-only"),
			):
				execute_external_tool("frappe_get_count", {})

		for status in (
			SimpleNamespace(disabled=1, available=1),
			SimpleNamespace(disabled=0, available=0),
			None,
		):
			with (
				patch("afaa.ai.external_tool_execution.get_tool_definition", return_value=self.definition),
				patch("afaa.ai.external_tool_execution.frappe.db.get_value", return_value=status),
				self.assertRaisesRegex(frappe.ValidationError, "disabled or unavailable"),
			):
				execute_external_tool("frappe_get_count", {})

	def test_read_tool_enforces_current_user_document_permissions(self):
		doc = frappe.get_doc({"doctype": "ToDo", "description": "Private proxy test"}).insert()

		with self.set_create_user():
			with self.assertRaises(frappe.PermissionError):
				execute_external_tool("frappe_get_doc", {"doctype": "ToDo", "name": doc.name})

	def test_rejects_guest_execution(self):
		frappe.set_user("Guest")
		with self.assertRaisesRegex(frappe.PermissionError, "authenticated Frappe user"):
			execute_external_tool("frappe_get_count", {"value": 1})
