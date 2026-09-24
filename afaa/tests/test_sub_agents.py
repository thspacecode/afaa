# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

"""Sub-agent hierarchy tests: doctype validation, resolution, and the v4 contract."""

from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase

from afaa.ai.agent_levels import AGENT_LEVEL_SUB_AGENT, AGENT_LEVEL_WORKER
from afaa.ai.external_runtime import (
	StructuredExternalRuntimeConfig,
	SubAgentAwareExternalRuntimeConfig,
	build_sub_agent_skill_capabilities,
	configuration_fingerprint,
	resolve_external_runtime,
)
from afaa.ai.runtime import resolve_ai_agent
from afaa.tests.data.factories import (
	AIAgentFactory,
	AIModelFactory,
	AIProviderAccountFactory,
	AIProviderFactory,
	AISkillFactory,
)


class TestSubAgentHierarchy(FrappeTestCase):
	def setUp(self):
		super().setUp()
		# The site already ships an enabled "openai" provider; reuse it when
		# present so the suite never races an existing master-data row. Each
		# test gets its own uniquely named model to avoid uniqueness clashes.
		if frappe.db.exists("AI Provider", "openai"):
			self.provider = frappe.get_doc("AI Provider", "openai")
		else:
			self.provider = AIProviderFactory.create(provider_name="openai")
		suffix = frappe.generate_hash("", 8)
		self.model = AIModelFactory.create(
			model_name=f"afaa-subagent-probe-{suffix}",
			model_id=f"afaa-subagent-probe-{suffix}",
			provider=self.provider.name,
		)
		self.account = AIProviderAccountFactory.create(
			account_name=f"SubAgent Probe {suffix}", provider=self.provider.name, api_key="test-secret-key"
		)

	def make_agent(self, **overrides):
		defaults = {
			"agent_name": "Sub Agent Probe",
			"agent_key": f"probe-{frappe.generate_hash('', 10)}",
			"model": self.model.name,
			"provider_account": self.account.name,
			"system_prompt": "Answer carefully.",
			"timeout": 60,
			"retries": 1,
			"tasks": [],
			"skills": [],
			"allowed_tools": [],
		}
		defaults.update(overrides)
		return AIAgentFactory.create(**defaults)

	def make_child(self, **overrides):
		defaults = {
			"agent_name": "Researcher",
			"agent_key": f"researcher-{frappe.generate_hash('', 8)}",
			"agent_level": AGENT_LEVEL_SUB_AGENT,
			"allowed_tools": [{"tool": "read_file"}],
		}
		defaults.update(overrides)
		return self.make_agent(**defaults)

	def test_new_agents_default_to_worker_level(self):
		agent = self.make_agent()
		self.assertEqual(agent.agent_level, AGENT_LEVEL_WORKER)

	def test_worker_may_reference_enabled_level_two_sub_agents(self):
		child = self.make_child()
		parent = self.make_agent(
			sub_agents=[{"sub_agent": child.name, "max_calls": 3, "timeout_seconds": 120}]
		)
		self.assertEqual(parent.sub_agents[0].sub_agent, child.name)
		self.assertEqual(parent.sub_agents[0].max_calls, 3)
		self.assertEqual(parent.sub_agents[0].timeout_seconds, 120)

	def test_non_worker_parents_cannot_configure_sub_agents(self):
		child = self.make_child()
		for level in ("0", "2"):
			with self.subTest(level=level), self.assertRaises(frappe.ValidationError):
				self.make_agent(agent_level=level, sub_agents=[{"sub_agent": child.name}])

	def test_self_duplicate_disabled_and_wrong_level_targets_are_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_agent(agent_key="self-ref-parent-x1", sub_agents=[{"sub_agent": "self-ref-parent-x1"}])

		worker = self.make_agent(agent_key="wrong-level-child-x1", allowed_tools=[{"tool": "frappe_get_doc"}])
		with self.assertRaises(frappe.ValidationError):
			self.make_agent(sub_agents=[{"sub_agent": worker.name}])

		child = self.make_child()
		parent = self.make_agent(sub_agents=[{"sub_agent": child.name}])
		with self.assertRaises(frappe.ValidationError):
			parent.append("sub_agents", {"sub_agent": child.name})
			parent.save()

		frappe.db.set_value("AI Agent", child.name, "disabled", 1, update_modified=False)
		other = self.make_agent(agent_key="second-parent-x1")
		with self.assertRaises(frappe.ValidationError):
			other.append("sub_agents", {"sub_agent": child.name})
			other.save()

	def test_level_two_agent_may_be_shared_and_trash_requires_all_references_removed(self):
		child = self.make_child()
		first_parent = self.make_agent(sub_agents=[{"sub_agent": child.name}])
		second_parent = self.make_agent(sub_agents=[{"sub_agent": child.name}])

		self.assertEqual(first_parent.sub_agents[0].sub_agent, child.name)
		self.assertEqual(second_parent.sub_agents[0].sub_agent, child.name)
		with self.assertRaises(frappe.ValidationError):
			child.delete()

		first_parent.sub_agents = []
		first_parent.save()
		with self.assertRaises(frappe.ValidationError):
			child.delete()

		second_parent.sub_agents = []
		second_parent.save()
		child.delete()
		self.assertFalse(frappe.db.exists("AI Agent", child.name))

	def test_runtime_workspace_tools_are_reserved_for_level_two(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_agent(allowed_tools=[{"tool": "read_file"}, {"tool": "frappe_get_doc"}])
		child = self.make_child(allowed_tools=[{"tool": "read_file"}, {"tool": "execute"}])
		self.assertEqual({row.tool for row in child.allowed_tools}, {"read_file", "execute"})

	def test_resolution_includes_fail_closed_children(self):
		child = self.make_child()
		parent = self.make_agent(sub_agents=[{"sub_agent": child.name, "max_calls": 2}])

		resolved = resolve_ai_agent(parent.name, include_sub_agents=True)
		self.assertEqual(resolved.agent_level, AGENT_LEVEL_WORKER)
		self.assertEqual(len(resolved.sub_agents), 1)
		delegate = resolved.sub_agents[0]
		self.assertEqual(delegate.key, child.agent_key)
		self.assertEqual(delegate.max_calls, 2)
		self.assertIsNone(delegate.timeout_seconds)
		self.assertEqual(delegate.model.provider_type, "openai")
		self.assertEqual({tool.key for tool in delegate.tools}, {"read_file"})

		frappe.db.set_value("AI Agent", child.name, "disabled", 1)
		with self.assertRaises(frappe.ValidationError):
			resolve_ai_agent(parent.name, include_sub_agents=True)

	def test_external_runtime_emits_credential_free_v4_contract(self):
		child = self.make_child(allowed_tools=[{"tool": "read_file"}, {"tool": "frappe_get_doc"}])
		self.make_agent(
			agent_key="parent-agent-x1",
			sub_agents=[{"sub_agent": child.name, "max_calls": 4, "timeout_seconds": 90}],
		)

		legacy = resolve_external_runtime("parent-agent-x1")
		self.assertIsInstance(legacy, StructuredExternalRuntimeConfig)
		self.assertNotIsInstance(legacy, SubAgentAwareExternalRuntimeConfig)

		config = resolve_external_runtime("parent-agent-x1", include_sub_agents=True)
		self.assertIsInstance(config, SubAgentAwareExternalRuntimeConfig)
		self.assertEqual(config.schema_version, 4)
		self.assertEqual(config.agent_level, "1")
		self.assertEqual(len(config.sub_agents), 1)

		delegate = config.sub_agents[0]
		self.assertEqual(delegate.agent_id, f"afaa:{child.agent_key}")
		self.assertEqual(delegate.delegate_name, "Researcher")
		self.assertEqual(delegate.max_calls, 4)
		self.assertEqual(delegate.timeout_seconds, 90.0)
		self.assertEqual(delegate.model.provider_type, "openai")
		self.assertNotIn("apiKey", delegate.model.model_dump(by_alias=True))
		runtime_tools = {tool.key: tool.runtime for tool in delegate.tools}
		self.assertEqual(runtime_tools, {"read_file": True, "frappe_get_doc": False})

		payload = config.model_dump(mode="json", by_alias=True)
		payload.pop("configurationFingerprint")
		payload["model"].pop("apiKey")
		if payload["model"].get("baseUrl") is None:
			payload["model"].pop("baseUrl")
		self.assertEqual(
			config.configuration_fingerprint,
			configuration_fingerprint(payload),
			"the fingerprint must cover agentLevel and every sub-agent descriptor",
		)

	def make_skill(self, *, required_tools, instructions="Search before answering."):
		suffix = frappe.generate_hash("", 8)
		return AISkillFactory.create(
			skill_name=f"Workspace Probe {suffix}",
			skill_key=f"workspace-probe-{suffix}",
			description="Probe the parent run's workspace.",
			instructions=instructions,
			required_tools=[{"tool": tool} for tool in required_tools],
		)

	def test_child_skills_ride_the_v4_contract_bundle_free(self):
		"""A skilled delegate's skills ride the v4 contract, fingerprinted and sorted.

		afaa ships credential-free skill content only; Porch is the one that
		pins immutable bundle versions, so the child DTO must never carry a
		bundle and the contract fingerprint must cover every child skill.
		"""
		first = self.make_skill(required_tools=["read_file", "grep"])
		second = self.make_skill(required_tools=["execute"])
		child = self.make_child(
			agent_key="skilled-child-x1",
			skills=[{"skill": first.name}, {"skill": second.name}],
			allowed_tools=[{"tool": "read_file"}, {"tool": "grep"}, {"tool": "execute"}],
		)
		parent = self.make_agent(agent_key="skilled-parent-x1", sub_agents=[{"sub_agent": child.name}])

		config = resolve_external_runtime(parent.agent_key, include_sub_agents=True)
		self.assertIsInstance(config, SubAgentAwareExternalRuntimeConfig)
		delegate = config.sub_agents[0]

		self.assertEqual(
			[skill.key for skill in delegate.skills],
			sorted(skill.skill_key for skill in (first, second)),
			"child skills are emitted in deterministic key order",
		)
		emitted = next(skill for skill in delegate.skills if skill.key == first.skill_key)
		self.assertEqual(emitted.name, first.skill_name)
		self.assertEqual(emitted.description, first.description)
		self.assertEqual(emitted.instructions, first.instructions)
		self.assertEqual(
			emitted.required_tools,
			("grep", "read_file"),
			"required tools are normalized to a sorted, deduplicated tuple",
		)
		self.assertNotIn(
			"bundle", emitted.model_dump(mode="json", by_alias=True), "afaa child skills stay bundle-free"
		)
		self.assertEqual(
			emitted.fingerprint,
			configuration_fingerprint(
				{
					"key": emitted.key,
					"name": emitted.name,
					"description": emitted.description,
					"instructions": emitted.instructions,
					"requiredTools": list(emitted.required_tools),
				}
			),
		)

		payload = config.model_dump(mode="json", by_alias=True)
		payload.pop("configurationFingerprint")
		payload["model"].pop("apiKey")
		if payload["model"].get("baseUrl") is None:
			payload["model"].pop("baseUrl")
		self.assertEqual(
			config.configuration_fingerprint,
			configuration_fingerprint(payload),
			"the contract fingerprint must cover every child skill",
		)

	def test_child_without_skills_emits_an_empty_skills_tuple(self):
		child = self.make_child()
		parent = self.make_agent(agent_key="skillless-parent-x1", sub_agents=[{"sub_agent": child.name}])

		config = resolve_external_runtime(parent.agent_key, include_sub_agents=True)
		delegate = config.sub_agents[0]
		self.assertEqual(delegate.skills, ())
		payload = config.model_dump(mode="json", by_alias=True)
		self.assertEqual(payload["subAgents"][0]["skills"], [], "the DTO always emits the child skills key")

	def test_drifted_child_skill_tools_fail_closed(self):
		"""A skill that later requires unadvertised tools fails the whole contract.

		The doctype blocks the mismatch at save time, so the realistic drift is
		an AI Skill edited after the child was configured; resolution must
		fail closed instead of emitting a contract the child cannot honor.
		"""
		skill = self.make_skill(required_tools=["read_file"])
		child = self.make_child(
			agent_key="drift-child-x1",
			skills=[{"skill": skill.name}],
			allowed_tools=[{"tool": "read_file"}],
		)
		parent = self.make_agent(agent_key="drift-parent-x1", sub_agents=[{"sub_agent": child.name}])
		# Sanity: the consistent configuration resolves before the drift.
		resolve_external_runtime(parent.agent_key, include_sub_agents=True)

		skill.append("required_tools", {"tool": "execute"})
		skill.save()
		with self.assertRaises(frappe.ValidationError):
			resolve_external_runtime(parent.agent_key, include_sub_agents=True)

	def test_sub_agent_skill_capabilities_enforce_required_tools(self):
		"""The DTO builder itself fails closed on an unadvertised required tool.

		Doctype and resolution validation reject this state for persisted
		agents; this guard still covers resolved trees assembled by callers
		(e.g. Porch pinning a thread snapshot from a resolved delegate).
		"""
		skill = SimpleNamespace(
			key="probe-skill",
			name="Probe",
			description=None,
			instructions="Probe the workspace.",
			required_tools=("read_file", "execute"),
		)
		child = SimpleNamespace(
			name="Researcher",
			tools=(SimpleNamespace(key="read_file"),),
			skills=(skill,),
		)
		with self.assertRaises(frappe.ValidationError):
			build_sub_agent_skill_capabilities(child)

		child.tools = (SimpleNamespace(key="read_file"), SimpleNamespace(key="execute"))
		emitted = build_sub_agent_skill_capabilities(child)
		self.assertEqual(len(emitted), 1)
		self.assertEqual(emitted[0].key, "probe-skill")
		self.assertEqual(emitted[0].required_tools, ("execute", "read_file"))

	def test_sub_agent_contract_requires_structured_skills(self):
		child = self.make_child()
		self.make_agent(agent_key="legacy-parent-x1", sub_agents=[{"sub_agent": child.name}])
		with self.assertRaises(frappe.ValidationError):
			resolve_external_runtime(
				"legacy-parent-x1", legacy_skill_instructions=True, include_sub_agents=True
			)
