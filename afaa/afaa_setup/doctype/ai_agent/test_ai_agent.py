# Copyright (c) 2026, SpaceCode and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from afaa.ai.external_runtime import (
	CodexExternalRuntimeConfig,
	notify_provider_account_invalidated,
	resolve_external_runtime,
)
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

	def test_external_runtime_returns_non_secret_codex_descriptor(self):
		resolved_agent = SimpleNamespace(
			key="codex-agent",
			name="Codex Agent",
			prompt="Keep credentials private.",
			skills=(),
			timeout=120.0,
			retries=2,
			model=SimpleNamespace(
				provider_type="openai_codex",
				provider_account="codex-provider-account",
				model_id="gpt-codex-test",
				settings={"openai_store": True, "temperature": 0.2},
			),
		)
		account = SimpleNamespace(
			name="codex-provider-account",
			disabled=0,
			oauth_status="Connected",
			connected_user="codex-user@example.test",
			external_account_id="account-123",
		)

		with (
			patch("afaa.ai.external_runtime.resolve_ai_agent", return_value=resolved_agent),
			patch("afaa.ai.external_runtime.frappe.get_doc", return_value=account),
		):
			resolved = resolve_external_runtime("codex-agent")

		self.assertIsInstance(resolved, CodexExternalRuntimeConfig)
		self.assertEqual(resolved.model.provider_account, "codex-provider-account")
		self.assertEqual(resolved.model.account_id, "account-123")
		self.assertFalse(resolved.model.settings["openai_store"])
		payload = resolved.model_dump(mode="json", by_alias=True)
		self.assertNotIn("access", repr(payload).lower())
		self.assertNotIn("refresh", repr(payload).lower())
		self.assertNotIn("apiKey", repr(payload))

	def test_provider_account_invalidation_hooks_are_best_effort_and_sanitized(self):
		successful_hook = Mock()

		def resolve_hook(path):
			if path == "broken.hook":
				raise RuntimeError("credential-bearing callback detail")
			return successful_hook

		with (
			patch("afaa.ai.external_runtime.frappe.get_hooks", return_value=["broken.hook", "ok.hook"]),
			patch("afaa.ai.external_runtime.frappe.get_attr", side_effect=resolve_hook),
			patch("afaa.ai.external_runtime.frappe.log_error") as log_error,
		):
			notify_provider_account_invalidated("codex-provider-account")

		successful_hook.assert_called_once_with("codex-provider-account")
		self.assertNotIn("credential-bearing callback detail", repr(log_error.call_args))

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
