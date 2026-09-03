# Copyright (c) 2026, SpaceCode and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

import frappe

from afaa.ai.external_runtime import resolve_external_runtime
from afaa.ai.runtime import resolve_ai_agent
from afaa.tests.data.factories import AIAgentFactory, AISkillFactory, AITaskDefinitionFactory
from afaa.tests.utils import AFAATestSuite, boot_strap_test_master_data


class TestAIAgent(AFAATestSuite):
	def test_resolves_valid_agent(self):
		key = f"agent-{frappe.generate_hash(length=8).lower()}"
		agent = AIAgentFactory.create(
			agent_name="Test Agent",
			agent_key=key,
			model=boot_strap_test_master_data.model,
			provider_account=boot_strap_test_master_data.provider_account,
			system_prompt="You are a {{ role }} agent.",
			use_temperature=1,
			temperature=0.4,
			max_tokens=500,
			model_overrides='{"seed": 42}',
		)

		self.assertEqual(agent.provider, boot_strap_test_master_data.provider)
		resolved = resolve_ai_agent(key, {"role": "test"})
		self.assertEqual(resolved.key, key)
		self.assertEqual(resolved.model.provider, boot_strap_test_master_data.provider)
		self.assertEqual(resolved.model.provider_account, boot_strap_test_master_data.provider_account)
		self.assertEqual(resolved.model.settings, {"seed": 42, "temperature": 0.4, "max_tokens": 500})
		self.assertEqual(resolved.prompt, "You are a test agent.")

	def test_external_runtime_contract_keeps_api_key_secret(self):
		key = f"agent-{frappe.generate_hash(length=8).lower()}"
		AIAgentFactory.create(
			agent_name="External Agent",
			agent_key=key,
			model=boot_strap_test_master_data.model,
			provider_account=boot_strap_test_master_data.provider_account,
			system_prompt="Review the workspace.",
		)

		account = frappe.get_doc("AI Provider Account", boot_strap_test_master_data.provider_account)
		account.api_key = "server-only-test-key"
		account.save(ignore_permissions=True)
		resolved = resolve_external_runtime(key)
		public_dump = resolved.model_dump(mode="json", by_alias=True)
		private_dump = resolved.private_payload()

		self.assertEqual(resolved.agent_id, f"afaa:{key}")
		self.assertEqual(public_dump["model"]["apiKey"], "**********")
		self.assertNotEqual(private_dump["model"]["apiKey"], "**********")
		self.assertEqual(len(resolved.configuration_fingerprint), 64)

	def test_external_runtime_rejects_openai_codex_without_reading_credentials(self):
		resolved_agent = SimpleNamespace(model=SimpleNamespace(provider_type="openai_codex"))

		with (
			patch("afaa.ai.external_runtime.resolve_ai_agent", return_value=resolved_agent),
			patch("afaa.ai.external_runtime.frappe.get_doc") as get_doc,
			self.assertRaisesRegex(
				frappe.ValidationError, "openai_codex.*not supported by external runtimes"
			),
		):
			resolve_external_runtime("codex-agent")

		get_doc.assert_not_called()

	def test_skill_cannot_escalate_tool_access(self):
		tool_key = self.create_tool()

		skill_key = f"skill-{frappe.generate_hash(length=8).lower()}"
		AISkillFactory.create(
			skill_name="Test Skill",
			skill_key=skill_key,
			instructions="Use the test tool.",
			required_tools=[{"tool": tool_key}],
		)

		with self.assertRaises(frappe.ValidationError):
			AIAgentFactory.create(
				agent_name="Escalating Agent",
				agent_key=f"agent-{frappe.generate_hash(length=8).lower()}",
				model=boot_strap_test_master_data.model,
				provider_account=boot_strap_test_master_data.provider_account,
				system_prompt="Use available skills.",
				tasks=[],
				skills=[{"skill": skill_key}],
				allowed_tools=[],
			)

	def test_task_cannot_escalate_tool_access(self):
		tool_key = self.create_tool()
		suffix = frappe.generate_hash(length=8).lower()
		task = AITaskDefinitionFactory.create(
			task_name="Test Task",
			task_key=f"task-{suffix}",
			description="Handle a test request.",
			instructions="Use the test tool.",
			required_tools=[{"tool": tool_key}],
			expected_output=[{"field_name": "message", "field_type": "String", "required": 1}],
		)

		with self.assertRaisesRegex(frappe.ValidationError, "requires tools not allowed"):
			AIAgentFactory.create(
				agent_name="Escalating Agent",
				agent_key=f"agent-{frappe.generate_hash(length=8).lower()}",
				model=boot_strap_test_master_data.model,
				provider_account=boot_strap_test_master_data.provider_account,
				system_prompt="Complete the selected task.",
				tasks=[{"task": task.name}],
				skills=[],
				allowed_tools=[],
			)

	def create_tool(self):
		return "frappe_get_count"
