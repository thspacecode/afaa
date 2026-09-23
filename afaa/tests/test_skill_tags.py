# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

"""AI Skill Tag resolution: union, dedupe, ordering, and fail-closed guards."""

import frappe

from afaa.ai.agent_levels import AGENT_LEVEL_SUB_AGENT
from afaa.ai.external_runtime import resolve_external_runtime
from afaa.ai.runtime import resolve_ai_agent
from afaa.tests.data.factories import AIAgentFactory, AISkillFactory, AISkillTagFactory
from afaa.tests.utils import AFAATestSuite, boot_strap_test_master_data


class TestSkillTagResolution(AFAATestSuite):
	def setUp(self):
		super().setUp()
		self.suffix = frappe.generate_hash(length=8).lower()

	def make_tag(self, tag_key: str, **overrides):
		return AISkillTagFactory.create(
			tag_key=tag_key, tag_name=tag_key.replace("-", " ").title(), **overrides
		)

	def make_skill(self, skill_key: str, *, tags=(), required_tools=()):
		return AISkillFactory.create(
			skill_name=skill_key.replace("-", " ").title(),
			skill_key=skill_key,
			instructions="Follow the tagged instructions.",
			tags=[{"tag": getattr(tag, "tag_key", tag)} for tag in tags],
			required_tools=[{"tool": tool} for tool in required_tools],
		)

	def make_agent(self, **overrides):
		values = {
			"agent_name": "Tag Probe Agent",
			"agent_key": f"tag-probe-{self.suffix}",
			"model": boot_strap_test_master_data.model,
			"provider_account": boot_strap_test_master_data.provider_account,
			"system_prompt": "Answer carefully.",
			"tasks": [],
			"skills": [],
			"skill_tags": [],
			"allowed_tools": [],
		}
		values.update(overrides)
		return AIAgentFactory.create(**values)

	def test_agent_skills_expand_by_tag_with_union_dedupe_and_ordering(self):
		tag = self.make_tag(f"frontend-{self.suffix}")
		other_tag = self.make_tag(f"backend-{self.suffix}")
		explicit = self.make_skill(f"explicit-skill-{self.suffix}")
		tagged_one = self.make_skill(f"zeta-skill-{self.suffix}", tags=[tag])
		tagged_two = self.make_skill(f"alpha-skill-{self.suffix}", tags=[tag])
		# Carrying a second, unselected tag must not leak the skill in.
		self.make_skill(f"backend-skill-{self.suffix}", tags=[other_tag])

		agent = self.make_agent(
			skills=[{"skill": explicit.name}],
			skill_tags=[{"tag": tag.tag_key}],
		)

		resolved = resolve_ai_agent(agent.name)
		self.assertEqual(
			[skill.key for skill in resolved.skills],
			[explicit.skill_key, tagged_two.skill_key, tagged_one.skill_key],
			"explicit skills keep row order and tag-derived skills follow in key order",
		)

		# Tags never mutate the agent's explicit skill child table.
		persisted = frappe.get_doc("AI Agent", agent.name)
		self.assertEqual([row.skill for row in persisted.skills], [explicit.name])
		self.assertEqual([row.tag for row in persisted.skill_tags], [tag.tag_key])

	def test_tag_derived_skills_apply_to_multi_tag_unions(self):
		frontend = self.make_tag(f"frontend-{self.suffix}")
		backend = self.make_tag(f"backend-{self.suffix}")
		frontend_skill = self.make_skill(f"frontend-skill-{self.suffix}", tags=[frontend])
		backend_skill = self.make_skill(f"backend-skill-{self.suffix}", tags=[backend])

		agent = self.make_agent(skill_tags=[{"tag": frontend.tag_key}, {"tag": backend.tag_key}])

		resolved = resolve_ai_agent(agent.name)
		self.assertEqual(
			[skill.key for skill in resolved.skills],
			sorted([frontend_skill.skill_key, backend_skill.skill_key]),
		)

	def test_tag_derived_skill_requiring_unallowed_tool_fails_closed(self):
		tag = self.make_tag(f"tooling-{self.suffix}")
		self.make_skill(f"greedy-skill-{self.suffix}", tags=[tag], required_tools=["frappe_get_count"])

		agent = self.make_agent(skill_tags=[{"tag": tag.tag_key}], allowed_tools=[])

		with self.assertRaisesRegex(frappe.ValidationError, "requires tools not allowed"):
			resolve_ai_agent(agent.name)

	def test_tag_derived_disabled_skill_fails_closed_when_required(self):
		tag = self.make_tag(f"stale-{self.suffix}")
		skill = self.make_skill(f"stale-skill-{self.suffix}", tags=[tag])
		agent = self.make_agent(skill_tags=[{"tag": tag.tag_key}])

		frappe.db.set_value("AI Skill", skill.name, "disabled", 1, update_modified=False)
		with self.assertRaisesRegex(frappe.ValidationError, "is disabled"):
			resolve_ai_agent(agent.name)

		# The same agent still resolves when the guard is lifted explicitly.
		resolved = resolve_ai_agent(agent.name, require_enabled=False)
		self.assertEqual([item.key for item in resolved.skills], [skill.skill_key])

	def test_disabled_tags_resolve_to_nothing(self):
		tag = self.make_tag(f"silent-{self.suffix}", disabled=1)
		self.make_skill(f"silent-skill-{self.suffix}", tags=[tag])

		agent = self.make_agent(skill_tags=[{"tag": tag.tag_key}])

		resolved = resolve_ai_agent(agent.name)
		self.assertEqual(resolved.skills, ())
		self.assertNotIn(tag.tag_key, str(resolved.model_dump()))

	def test_tag_expanded_skills_reach_sub_agents_and_the_runtime_contract(self):
		tag = self.make_tag(f"delegate-{self.suffix}")
		tagged = self.make_skill(f"delegate-skill-{self.suffix}", tags=[tag])

		child = self.make_agent(
			agent_key=f"tagged-child-{self.suffix}",
			agent_name="Tagged Researcher",
			agent_level=AGENT_LEVEL_SUB_AGENT,
			skill_tags=[{"tag": tag.tag_key}],
			allowed_tools=[{"tool": "read_file"}],
		)
		parent = self.make_agent(
			agent_key=f"tagged-parent-{self.suffix}",
			sub_agents=[{"sub_agent": child.name}],
		)

		resolved = resolve_ai_agent(parent.name, include_sub_agents=True)
		delegate = resolved.sub_agents[0]
		self.assertEqual([skill.key for skill in delegate.skills], [tagged.skill_key])

		account = frappe.get_doc("AI Provider Account", boot_strap_test_master_data.provider_account)
		account.api_key = "server-only-test-key"
		account.save(ignore_permissions=True)
		config = resolve_external_runtime(parent.name, include_sub_agents=True)
		payload = config.model_dump(mode="json", by_alias=True)
		child_skills = [skill["key"] for skill in payload["subAgents"][0]["skills"]]
		self.assertEqual(child_skills, [tagged.skill_key])

	def test_agent_rejects_duplicate_skill_tags(self):
		tag = self.make_tag(f"duplicate-{self.suffix}")
		with self.assertRaisesRegex(frappe.ValidationError, "listed more than once"):
			self.make_agent(skill_tags=[{"tag": tag.tag_key}, {"tag": tag.tag_key}])
