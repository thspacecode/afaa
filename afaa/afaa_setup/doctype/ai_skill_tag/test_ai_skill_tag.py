# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe

from afaa.tests.data.factories import AISkillTagFactory
from afaa.tests.utils import AFAATestSuite


class TestAISkillTag(AFAATestSuite):
	def make_tag(self, **overrides):
		suffix = frappe.generate_hash(length=8).lower()
		values = {
			"tag_name": f"Probe Tag {suffix}",
			"tag_key": f"probe-tag-{suffix}",
			"description": "Skills for probing tag behaviour.",
		}
		values.update(overrides)
		return AISkillTagFactory.create(**values)

	def test_tag_is_named_by_its_key(self):
		tag = self.make_tag()
		self.assertEqual(tag.name, tag.tag_key)
		self.assertEqual(frappe.get_meta("AI Skill Tag").title_field, "tag_name")
		self.assertEqual(tag.disabled, 0)

	def test_rejects_invalid_tag_keys(self):
		for invalid in ("Frontend", "ab", "frontend tag", "1frontend", "-frontend", "", "x" * 51):
			with self.subTest(invalid=invalid), self.assertRaises(frappe.ValidationError):
				self.make_tag(tag_key=invalid or None)

	def test_tag_key_is_immutable_after_creation(self):
		tag = self.make_tag()
		tag.tag_key = f"renamed-{frappe.generate_hash(length=6).lower()}"
		with self.assertRaises(frappe.PermissionError):
			tag.save()

	def test_tag_key_must_stay_unique(self):
		self.make_tag(tag_key="unique-probe-tag")
		with self.assertRaises(frappe.DuplicateEntryError):
			self.make_tag(tag_key="unique-probe-tag")
