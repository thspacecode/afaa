# Copyright (c) 2026, SpaceCode and Contributors
# See license.txt

import sys
from unittest.mock import patch

import frappe
from pydantic import BaseModel

import afaa
from afaa.ai.tools import _get_tool_models, get_tool_definition, sync_registered_tools
from afaa.tests.utils import AFAATestSuite

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class MockToolInput(BaseModel):
	required_value: str
	optional_flag: bool = False


class MockToolOutput(BaseModel):
	success: bool


@afaa.tool(name="Mock Tool")
def mock_tool(request: MockToolInput) -> MockToolOutput:
	"""Mock tool used by the AI Tool tests."""
	return MockToolOutput(success=False)


class IntegrationTestAITool(AFAATestSuite):
	"""
	Integration tests for AITool.
	Use this class for testing interactions between multiple components.
	"""

	def test_tool_decorator_builds_definition_from_function(self):
		definition = mock_tool.__afaa_tool_definition__

		self.assertEqual(definition.key, "mock_tool")
		self.assertEqual(definition.name, "Mock Tool")
		self.assertEqual(definition.description, "Mock tool used by the AI Tool tests.")
		self.assertEqual(definition.method, f"{__name__}.mock_tool")
		self.assertEqual(definition.input_schema["required"], ["required_value"])
		self.assertEqual(definition.input_schema["properties"]["required_value"]["type"], "string")
		self.assertEqual(definition.input_schema["properties"]["optional_flag"]["type"], "boolean")
		self.assertEqual(definition.output_schema["properties"]["success"]["type"], "boolean")

	def test_tool_decorator_requires_pydantic_input_and_output_models(self):
		def invalid_input(value: str) -> MockToolOutput:
			pass

		def invalid_output(value: MockToolInput) -> str:
			pass

		with self.assertRaisesRegex(TypeError, "input must be a Pydantic BaseModel"):
			_get_tool_models(invalid_input)

		with self.assertRaisesRegex(TypeError, "output must be a Pydantic BaseModel"):
			_get_tool_models(invalid_output)

	def test_decorated_function_is_discovered_from_app_tools_module(self):
		with (
			patch("afaa.ai.tools.frappe.get_installed_apps", return_value=["afaa"]),
			patch("afaa.ai.tools.importlib.import_module", return_value=sys.modules[__name__]),
		):
			definition = get_tool_definition("mock_tool")

		self.assertEqual(definition, mock_tool.__afaa_tool_definition__)

	def test_registered_tools_are_enabled_when_first_created(self):
		tool_key = "frappe_create_doc"
		frappe.delete_doc("AI Tool", tool_key, ignore_permissions=True)

		report = sync_registered_tools()

		self.assertIn(tool_key, report["created"])
		tool = frappe.get_doc("AI Tool", tool_key)
		self.assertFalse(tool.disabled)
		self.assertTrue(tool.available)
