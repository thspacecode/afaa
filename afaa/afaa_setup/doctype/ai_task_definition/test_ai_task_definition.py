# Copyright (c) 2026, SpaceCode and Contributors
# See license.txt

from types import SimpleNamespace

import frappe
from pydantic import ValidationError

from afaa.ai.outputs import build_task_output_model
from afaa.tests.data.factories import AITaskDefinitionFactory
from afaa.tests.utils import AFAATestSuite


class TestAITaskDefinition(AFAATestSuite):
	def test_validates_and_builds_strict_output_model(self):
		task = self.create_task(
			expected_output=[
				{"field_name": "width", "field_type": "Integer", "required": 1},
				{"field_name": "units", "field_type": "String", "required": 1},
				{"field_name": "verified", "field_type": "Boolean", "required": 0},
			]
		)
		resolved_task = SimpleNamespace(
			key=task.task_key,
			expected_output=[
				SimpleNamespace(
					field_name=row.field_name,
					field_type=row.field_type,
					description=row.description,
					required=bool(row.required),
				)
				for row in task.expected_output
			],
		)
		output_model = build_task_output_model(resolved_task)

		output = output_model(width=10, units="cm")
		self.assertEqual(output.model_dump(), {"width": 10, "units": "cm", "verified": None})
		self.assertTrue(output_model(width=10, units="cm", verified=True).verified)
		with self.assertRaises(ValidationError):
			output_model(width=10, units="cm", extra="not allowed")

	def test_rejects_duplicate_output_fields(self):
		with self.assertRaisesRegex(frappe.ValidationError, "listed more than once"):
			self.create_task(
				expected_output=[
					{"field_name": "value", "field_type": "String", "required": 1},
					{"field_name": "value", "field_type": "Integer", "required": 1},
				]
			)

	def test_rejects_duplicate_required_tools(self):
		with self.assertRaisesRegex(frappe.ValidationError, "listed more than once"):
			self.create_task(
				expected_output=[{"field_name": "value", "field_type": "String", "required": 1}],
				required_tools=[
					{"tool": "frappe_get_count"},
					{"tool": "frappe_get_count"},
				],
			)

	def create_task(self, expected_output, required_tools=None):
		suffix = frappe.generate_hash(length=8).lower()
		return AITaskDefinitionFactory.create(
			task_name=f"Task {suffix}",
			task_key=f"task-{suffix}",
			description="Handle a test request.",
			instructions="Return the requested values.",
			required_tools=required_tools or [],
			expected_output=expected_output,
		)
