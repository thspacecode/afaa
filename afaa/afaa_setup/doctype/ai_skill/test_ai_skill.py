# Copyright (c) 2026, SpaceCode and Contributors
# See license.txt

import frappe

from afaa.ai.runtime import resolve_ai_agent
from afaa.tests.utils import AFAATestSuite


class TestAISkill(AFAATestSuite):
	def test_bootstrap_skill_is_resolved_with_its_tools(self):
		agent = resolve_ai_agent("frappe-data-assistant")
		skill = next(item for item in agent.skills if item.key == "frappe-data-reader")

		self.assertEqual(skill.name, "Frappe Data Reader")
		self.assertIn("frappe_get_doc", skill.required_tools)
		self.assertTrue(set(skill.required_tools).issubset({tool.key for tool in agent.tools}))

	def test_rejects_duplicate_required_tools(self):
		suffix = frappe.generate_hash(length=8).lower()
		with self.assertRaisesRegex(frappe.ValidationError, "listed more than once"):
			frappe.get_doc(
				{
					"doctype": "AI Skill",
					"skill_name": f"Duplicate Tool Skill {suffix}",
					"skill_key": f"duplicate-tool-skill-{suffix}",
					"instructions": "Use the configured tools.",
					"required_tools": [
						{"tool": "frappe_get_count"},
						{"tool": "frappe_get_count"},
					],
				}
			).insert()
