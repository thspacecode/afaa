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
		self.assertEqual(skill.description, "Inspect Frappe metadata and records without changing them.")
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

	def test_accepts_multiple_distinct_tags_and_rejects_duplicates(self):
		suffix = frappe.generate_hash(length=8).lower()
		for tag_key in ("tag-one-probe", "tag-two-probe"):
			if not frappe.db.exists("AI Skill Tag", tag_key):
				frappe.get_doc(
					{
						"doctype": "AI Skill Tag",
						"tag_key": tag_key,
						"tag_name": tag_key.replace("-", " ").title(),
					}
				).insert(ignore_permissions=True)

		skill = frappe.get_doc(
			{
				"doctype": "AI Skill",
				"skill_name": f"Tagged Skill {suffix}",
				"skill_key": f"tagged-skill-{suffix}",
				"instructions": "Use the configured tools.",
				"tags": [{"tag": "tag-one-probe"}, {"tag": "tag-two-probe"}],
			}
		).insert(ignore_permissions=True)
		self.assertEqual(
			[row.tag for row in frappe.get_doc("AI Skill", skill.name).tags],
			["tag-one-probe", "tag-two-probe"],
		)

		with self.assertRaisesRegex(frappe.ValidationError, "listed more than once"):
			skill.append("tags", {"tag": "tag-one-probe"})
			skill.save(ignore_permissions=True)
