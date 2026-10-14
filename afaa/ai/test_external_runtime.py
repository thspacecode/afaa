# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import logging
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe
from pydantic import ValidationError

from afaa.afaa_setup.doctype.ai_agent.ai_agent import AIAgent
from afaa.ai.agent_levels import runtime_tool_definition
from afaa.ai.external_runtime import (
	BundleExternalRuntimeSkill,
	ExternalRuntimeConfig,
	ExternalRuntimeSkill,
	ExternalSubAgent,
	MCPAwareCodexExternalRuntimeDescriptor,
	MCPAwareExternalRuntimeConfig,
	StructuredExternalRuntimeConfig,
	SubAgentAwareExternalRuntimeConfig,
	build_structured_runtime_capabilities,
	configuration_fingerprint,
	resolve_external_runtime,
)
from afaa.ai.runtime import ResolvedModel, _resolve_sub_agents
from afaa.ai.skill_bundles import EMPTY_BUNDLE_DIGEST, SkillBundleReference
from afaa.ai.tools import EXTERNAL_READ_TOOL_METHODS
from afaa.utils.data import parse_bounded_seconds, validate_deadline_order


class TestStructuredExternalRuntime(TestCase):
	def test_structured_contract_keeps_skills_and_tools_separate(self):
		resolved = make_resolved_agent()
		account = SimpleNamespace(
			name="provider-account",
			get_password=lambda *_args, **_kwargs: "server-only-secret",
		)

		with (
			patch("afaa.ai.external_runtime.resolve_ai_agent", return_value=resolved),
			patch("afaa.ai.external_runtime.frappe.get_doc", return_value=account),
			patch(
				"afaa.ai.external_runtime.get_tool_definition",
				side_effect=lambda key: make_tool_definition(key),
			),
		):
			config = resolve_external_runtime("reviewer")

		self.assertIsInstance(config, StructuredExternalRuntimeConfig)
		self.assertEqual(config.instructions, ("Review the workspace.",))
		self.assertEqual([skill.key for skill in config.skills], ["data-reader"])
		self.assertEqual(config.skills[0].description, "Read Frappe records.")
		self.assertEqual(config.skills[0].required_tools, ("frappe_get_doc", "frappe_get_list"))
		self.assertEqual(
			[tool.key for tool in config.tools],
			["frappe_get_doc", "frappe_get_list"],
		)
		payload = config.private_payload()
		self.assertNotIn("method", str(payload["tools"]).lower())
		self.assertEqual(payload["model"]["apiKey"], "server-only-secret")

	def test_payload_carries_provider_base_url_when_configured(self):
		resolved = make_resolved_agent()
		resolved.model.base_url = "https://api.z.ai/api/coding/paas/v4"
		resolved.model.provider_type = "zai"
		account = SimpleNamespace(
			name="provider-account",
			get_password=lambda *_args, **_kwargs: "server-only-secret",
		)

		with (
			patch("afaa.ai.external_runtime.resolve_ai_agent", return_value=resolved),
			patch("afaa.ai.external_runtime.frappe.get_doc", return_value=account),
			patch(
				"afaa.ai.external_runtime.get_tool_definition",
				side_effect=lambda key: make_tool_definition(key),
			),
		):
			config = resolve_external_runtime("reviewer")

		payload = config.private_payload()
		self.assertEqual(payload["model"]["baseUrl"], "https://api.z.ai/api/coding/paas/v4")
		self.assertEqual(payload["model"]["providerType"], "zai")

	def test_payload_omits_base_url_when_unset(self):
		resolved = make_resolved_agent()
		account = SimpleNamespace(
			name="provider-account", get_password=lambda *_args, **_kwargs: "server-only-secret"
		)

		with (
			patch("afaa.ai.external_runtime.resolve_ai_agent", return_value=resolved),
			patch("afaa.ai.external_runtime.frappe.get_doc", return_value=account),
			patch(
				"afaa.ai.external_runtime.get_tool_definition",
				side_effect=lambda key: make_tool_definition(key),
			),
		):
			config = resolve_external_runtime("reviewer")

		payload = config.private_payload()
		self.assertNotIn("baseUrl", payload["model"])

	def test_fingerprint_changes_when_base_url_changes(self):
		def fingerprint_for(base_url: str | None) -> str:
			resolved = make_resolved_agent()
			resolved.model.base_url = base_url
			resolved.model.provider_type = "zai"
			return configuration_fingerprint(
				{
					"schemaVersion": 1,
					"agentId": f"afaa:{resolved.key}",
					"name": resolved.name,
					"instructions": ("Review the workspace.",),
					"model": {
						"providerType": resolved.model.provider_type,
						"modelId": resolved.model.model_id,
						"settings": resolved.model.settings,
						"timeout": resolved.timeout,
						"retries": resolved.retries,
						**({"baseUrl": resolved.model.base_url} if resolved.model.base_url else {}),
					},
				}
			)

		without = fingerprint_for(None)
		with_url = fingerprint_for("https://api.z.ai/api/coding/paas/v4")
		self.assertNotEqual(without, with_url)
		self.assertEqual(with_url, fingerprint_for("https://api.z.ai/api/coding/paas/v4"))

	def test_legacy_contract_flattens_skill_instructions_when_explicitly_requested(self):
		resolved = make_resolved_agent()
		account = SimpleNamespace(name="provider-account", get_password=lambda *_args, **_kwargs: "secret")

		with (
			patch("afaa.ai.external_runtime.resolve_ai_agent", return_value=resolved),
			patch("afaa.ai.external_runtime.frappe.get_doc", return_value=account),
		):
			config = resolve_external_runtime("reviewer", legacy_skill_instructions=True)

		self.assertIs(type(config), ExternalRuntimeConfig)
		self.assertEqual(config.instructions, ("Review the workspace.", "Inspect records carefully."))
		self.assertNotIn("skills", config.private_payload())
		self.assertNotIn("tools", config.private_payload())

	def test_fingerprints_are_canonical_and_exclude_api_credentials(self):
		resolved = make_resolved_agent()
		account = SimpleNamespace(name="provider-account")
		account.get_password = lambda *_args, **_kwargs: "first-secret"

		with (
			patch("afaa.ai.external_runtime.resolve_ai_agent", return_value=resolved),
			patch("afaa.ai.external_runtime.frappe.get_doc", return_value=account),
			patch(
				"afaa.ai.external_runtime.get_tool_definition",
				side_effect=lambda key: make_tool_definition(key),
			),
		):
			first = resolve_external_runtime("reviewer")
			account.get_password = lambda *_args, **_kwargs: "rotated-secret"
			second = resolve_external_runtime("reviewer")

		self.assertEqual(first.configuration_fingerprint, second.configuration_fingerprint)
		self.assertEqual(first.skills[0].fingerprint, second.skills[0].fingerprint)
		snapshot = first.skills[0].model_dump(mode="json", by_alias=True, exclude={"fingerprint"})
		self.assertEqual(first.skills[0].fingerprint, configuration_fingerprint(snapshot))

		with (
			patch("afaa.ai.external_runtime.resolve_ai_agent", return_value=resolved),
			patch("afaa.ai.external_runtime.frappe.get_doc", return_value=account),
			patch(
				"afaa.ai.external_runtime.get_tool_definition",
				side_effect=lambda key: make_tool_definition(key, description="Changed definition."),
			),
		):
			changed_tool = resolve_external_runtime("reviewer")
		self.assertNotEqual(first.configuration_fingerprint, changed_tool.configuration_fingerprint)
		self.assertEqual(first.skills[0].fingerprint, changed_tool.skills[0].fingerprint)

	def test_bundle_aware_contract_pins_reference_and_fingerprints_it(self):
		resolved = make_resolved_agent()
		bundle = SkillBundleReference(
			versionId="1" * 64,
			digest=EMPTY_BUNDLE_DIGEST,
			fileCount=0,
			totalBytes=0,
		)
		with (
			patch(
				"afaa.ai.external_runtime.get_tool_definition",
				side_effect=lambda key: make_tool_definition(key),
			),
			patch("afaa.ai.skill_bundles.create_skill_bundle_version", return_value=bundle),
		):
			skills, _tools = build_structured_runtime_capabilities(resolved, include_bundles=True)

		self.assertIsInstance(skills[0], BundleExternalRuntimeSkill)
		self.assertEqual(skills[0].bundle, bundle)
		snapshot = skills[0].model_dump(mode="json", by_alias=True, exclude={"fingerprint"})
		self.assertEqual(skills[0].fingerprint, configuration_fingerprint(snapshot))

	def test_skill_dto_rejects_a_fingerprint_for_different_content(self):
		with self.assertRaisesRegex(ValueError, "fingerprint does not match"):
			ExternalRuntimeSkill.model_validate(
				{
					"key": "data-reader",
					"name": "Data Reader",
					"description": "Read Frappe records.",
					"instructions": "Tampered instructions.",
					"requiredTools": ["frappe_get_doc"],
					"fingerprint": "0" * 64,
				}
			)

	def test_structured_contract_rejects_skill_tools_outside_read_allowlist(self):
		resolved = make_resolved_agent(
			skill_required_tools=("frappe_create_doc",),
			tool_keys=("frappe_create_doc",),
		)

		with self.assertRaisesRegex(frappe.ValidationError, "unsupported by external runtimes"):
			build_structured_runtime_capabilities(resolved)


def make_resolved_agent(
	*,
	skill_required_tools: tuple[str, ...] = ("frappe_get_list", "frappe_get_doc"),
	tool_keys: tuple[str, ...] = ("frappe_get_list", "frappe_get_doc", "frappe_create_doc"),
):
	return SimpleNamespace(
		key="reviewer",
		name="Reviewer",
		prompt=" Review the workspace. ",
		skills=(
			SimpleNamespace(
				key="data-reader",
				name="Data Reader",
				description="Read Frappe records.",
				instructions="Inspect records carefully.",
				required_tools=skill_required_tools,
			),
		),
		tools=tuple(
			SimpleNamespace(
				key=key,
				name=key,
				description=key,
				input_schema={},
				output_schema={},
			)
			for key in tool_keys
		),
		model=SimpleNamespace(
			provider_type="openai",
			provider_account="provider-account",
			model_id="test-model",
			settings={"temperature": 0.2},
			base_url=None,
		),
		timeout=120.0,
		retries=2,
		mcp_servers=(),
	)


def make_tool_definition(key: str, *, description: str | None = None):
	return SimpleNamespace(
		key=key,
		name=key.replace("_", " ").title(),
		description=description or f"Safely execute {key}.",
		method=EXTERNAL_READ_TOOL_METHODS[key],
		input_schema={"type": "object", "additionalProperties": False},
		output_schema={"type": "object"},
	)


def make_sub_agent_payload(**overrides):
	"""Minimal valid v4 delegate payload for DTO-level soft deadline tests."""
	values = {
		"agentId": "afaa:researcher",
		"name": "Researcher",
		"delegateName": "Researcher",
		"instructions": ("Help the parent run.",),
		"model": {
			"providerType": "openai",
			"modelId": "test-model",
			"settings": {"temperature": 0.2},
			"timeout": 60.0,
			"retries": 1,
		},
		"tools": [],
		"skills": [],
		"timeoutSeconds": 90,
	}
	values.update(overrides)
	return values


def make_mcp_sub_agent_payload(**overrides):
	values = {
		**make_sub_agent_payload(),
		"mcpServers": (
			{
				"key": "docs",
				"name": "Docs",
				"url": "https://mcp.example.com",
				"connectTimeout": 5,
				"readTimeout": 30,
				"authorizationToken": "secret-token",
			},
		),
	}
	values.update(overrides)
	return values


def make_mcp_aware_config_payload(sub_agents):
	return {
		"schemaVersion": 5,
		"mcpContractVersion": 1,
		"agentLevel": "1",
		"agentId": "afaa:parent",
		"name": "Parent",
		"instructions": ("Run the parent task.",),
		"model": {
			"providerType": "openai",
			"modelId": "test-model",
			"settings": {},
			"timeout": 60.0,
			"retries": 1,
			"apiKey": "parent-secret",
		},
		"skills": [],
		"tools": [],
		"subAgents": sub_agents,
		"mcpServers": [],
		"configurationFingerprint": "0" * 64,
	}


def make_codex_descriptor_payload(sub_agents):
	return {
		"schemaVersion": 5,
		"mcpContractVersion": 1,
		"agentLevel": "1",
		"agentId": "afaa:parent",
		"name": "Parent",
		"instructions": ("Run the parent task.",),
		"model": {
			"providerType": "openai_codex",
			"providerAccount": "chatgpt-account",
			"accountId": "acc-123",
			"modelId": "gpt-5-codex",
			"settings": {"openai_store": False},
			"timeout": 120.0,
			"retries": 1,
		},
		"skills": (),
		"tools": (),
		"subAgents": tuple(sub_agents),
		"mcpServers": (),
		"configurationFingerprint": "0" * 64,
	}


def make_resolved_sub_agent(**overrides):
	values = {
		"name": "Researcher",
		"key": "researcher",
		"description": None,
		"prompt": "Help the parent run.",
		"model": SimpleNamespace(
			provider_type="openai",
			provider_account="provider-account",
			model_id="test-model",
			settings={"temperature": 0.2},
			base_url=None,
		),
		"tools": (
			SimpleNamespace(
				key="read_file",
				name="read_file",
				description="Read a text file from the project workspace.",
				method=runtime_tool_definition("read_file")["method"],
				input_schema={},
				output_schema={},
			),
		),
		"skills": (),
		"mcp_servers": (),
		"max_calls": 2,
		"request_limit": None,
		"timeout_seconds": 90.0,
		"soft_timeout_seconds": None,
		"timeout": 60.0,
		"retries": 1,
	}
	values.update(overrides)
	return SimpleNamespace(**values)


def make_sub_agent_row(**overrides):
	values = {
		"sub_agent": "researcher-child",
		"max_calls": 2,
		"request_limit": 0,
		"timeout_seconds": 90,
		"soft_timeout_seconds": None,
	}
	values.update(overrides)
	return SimpleNamespace(**values)


class TestExternalSubAgentSoftDeadline(TestCase):
	"""Focused soft-deadline coverage: DTO strictness, payload omission,
	config save ordering, and resolution parsing/propagation.

	The tests run without a Frappe site; fail-closed guards raise through
	``frappe.throw``, so a minimal ``frappe.local`` is bound and the
	translation fallback logger is muted for the class duration.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		logger_patcher = patch("frappe.logger", return_value=logging.getLogger("afaa.tests"))
		logger_patcher.start()
		cls.addClassCleanup(logger_patcher.stop)
		frappe.local.message_log = []
		frappe.local.error_log = []
		frappe.local.debug_log = []
		frappe.local.flags = frappe._dict()
		frappe.local.lang = "en"

	# ------------------------------------------------------------------ DTO

	def test_soft_deadline_dto_accepts_only_real_ints(self):
		for soft, hard in ((60, 90), (1, 2), (86399, 86400)):
			with self.subTest(soft=soft, hard=hard):
				delegate = ExternalSubAgent.model_validate(
					{**make_sub_agent_payload(), "timeoutSeconds": hard, "softTimeoutSeconds": soft}
				)
				self.assertIsInstance(delegate.soft_timeout_seconds, int)
				self.assertEqual(delegate.soft_timeout_seconds, soft)

	def test_soft_deadline_dto_rejects_lossy_representations(self):
		for value, error_type in (
			("60", "int_type"),
			(" 60 ", "int_type"),
			("060", "int_type"),
			(60.0, "int_type"),
			(60.5, "int_type"),
			(True, "int_type"),
			(False, "int_type"),
		):
			with self.subTest(value=value):
				with self.assertRaises(ValidationError) as raised:
					ExternalSubAgent.model_validate({**make_sub_agent_payload(), "softTimeoutSeconds": value})
				self.assertEqual(raised.exception.errors()[0]["type"], error_type)

	def test_soft_deadline_dto_rejects_out_of_range_values(self):
		for value, error_type in (
			(0, "greater_than_equal"),
			(-60, "greater_than_equal"),
			(86401, "less_than_equal"),
		):
			with self.subTest(value=value):
				with self.assertRaises(ValidationError) as raised:
					ExternalSubAgent.model_validate({**make_sub_agent_payload(), "softTimeoutSeconds": value})
				self.assertEqual(raised.exception.errors()[0]["type"], error_type)

	def test_soft_deadline_dto_requires_a_hard_deadline_strictly_above_it(self):
		missing_hard = make_sub_agent_payload(softTimeoutSeconds=60)
		missing_hard.pop("timeoutSeconds")
		with self.assertRaisesRegex(ValueError, "requires a timeoutSeconds hard deadline"):
			ExternalSubAgent.model_validate(missing_hard)

		for soft, hard in ((90, 90), (120, 90)):
			with self.subTest(soft=soft, hard=hard):
				with self.assertRaisesRegex(ValueError, "must be less than the timeoutSeconds"):
					ExternalSubAgent.model_validate(
						{**make_sub_agent_payload(), "timeoutSeconds": hard, "softTimeoutSeconds": soft}
					)

	def test_soft_deadline_config_contract_rejects_lossy_nested_values(self):
		payload = {
			"schemaVersion": 4,
			"agentLevel": "1",
			"agentId": "afaa:parent",
			"name": "Parent",
			"instructions": ("Run the parent task.",),
			"model": {
				"providerType": "openai",
				"modelId": "test-model",
				"settings": {},
				"timeout": 60.0,
				"retries": 1,
				"apiKey": "parent-secret",
			},
			"skills": [],
			"tools": [],
			"subAgents": [make_sub_agent_payload(softTimeoutSeconds="60")],
			"configurationFingerprint": "0" * 64,
		}
		with self.assertRaises(ValidationError):
			SubAgentAwareExternalRuntimeConfig.model_validate(payload)

	# ------------------------------------------------------- serializer shape

	def test_soft_deadline_is_omitted_when_unset_and_emitted_as_int_when_set(self):
		unset = ExternalSubAgent.model_validate(make_sub_agent_payload())
		payload = unset.model_dump(mode="json", by_alias=True)
		self.assertNotIn("softTimeoutSeconds", payload)
		self.assertNotIn("softTimeoutSeconds", unset.model_dump_json(by_alias=True))
		self.assertNotIn("soft_timeout_seconds", unset.model_dump(mode="python"))
		self.assertEqual(
			sorted(payload.keys()),
			[
				"agentId",
				"delegateName",
				"description",
				"instructions",
				"maxCalls",
				"model",
				"name",
				"requestLimit",
				"skills",
				"timeoutSeconds",
				"tools",
			],
			"the unset soft deadline must keep the pre-soft-timeout payload shape",
		)

		configured = ExternalSubAgent.model_validate({**make_sub_agent_payload(), "softTimeoutSeconds": 60})
		self.assertEqual(configured.model_dump(mode="json", by_alias=True)["softTimeoutSeconds"], 60)

	def test_soft_deadline_is_omitted_from_nested_v4_and_v5_contracts(self):
		v4 = SubAgentAwareExternalRuntimeConfig.model_validate(
			{
				"schemaVersion": 4,
				"agentLevel": "1",
				"agentId": "afaa:parent",
				"name": "Parent",
				"instructions": ("Run the parent task.",),
				"model": {
					"providerType": "openai",
					"modelId": "test-model",
					"settings": {},
					"timeout": 60.0,
					"retries": 1,
					"apiKey": "parent-secret",
				},
				"skills": [],
				"tools": [],
				"subAgents": [make_sub_agent_payload()],
				"configurationFingerprint": "0" * 64,
			}
		)
		self.assertNotIn("softTimeoutSeconds", v4.model_dump(mode="json", by_alias=True)["subAgents"][0])

		v5 = MCPAwareExternalRuntimeConfig.model_validate(
			make_mcp_aware_config_payload([make_mcp_sub_agent_payload()])
		)
		private = v5.private_payload()
		self.assertNotIn("softTimeoutSeconds", private["subAgents"][0])

		configured_v5 = MCPAwareExternalRuntimeConfig.model_validate(
			make_mcp_aware_config_payload([make_mcp_sub_agent_payload(softTimeoutSeconds=60)])
		)
		private = configured_v5.private_payload()
		self.assertEqual(private["subAgents"][0]["softTimeoutSeconds"], 60)
		self.assertEqual(private["subAgents"][0]["mcpServers"][0]["authorizationToken"], "secret-token")

	def test_soft_deadline_is_omitted_from_codex_descriptors(self):
		unset = MCPAwareCodexExternalRuntimeDescriptor.model_validate(
			make_codex_descriptor_payload([make_mcp_sub_agent_payload()])
		)
		self.assertNotIn("softTimeoutSeconds", unset.private_sub_agents()[0])

		configured = MCPAwareCodexExternalRuntimeDescriptor.model_validate(
			make_codex_descriptor_payload([make_mcp_sub_agent_payload(softTimeoutSeconds=60)])
		)
		self.assertEqual(configured.private_sub_agents()[0]["softTimeoutSeconds"], 60)

	# ------------------------------------------------------------- parsing

	def test_deadline_parser_normalizes_lossless_values(self):
		for value, expected in ((90, 90), (90.0, 90), ("90", 90), (" 90 ", 90)):
			with self.subTest(value=value):
				self.assertEqual(parse_bounded_seconds(value, label="Timeout (Seconds)"), expected)

		for value in (None, "", "   ", 0, "0", 0.0):
			with self.subTest(value=value):
				self.assertEqual(parse_bounded_seconds(value, label="Timeout (Seconds)"), 0)
				self.assertIsNone(
					parse_bounded_seconds(value, label="Timeout (Seconds)", unset=None),
					"resolution must map unset deadlines to None",
				)

	def test_deadline_parser_fails_closed_on_lossy_values(self):
		for value in ("abc", "90.5", 90.5, 90_000_000_000_000_000_000.0, True, False, -1, "-90", 86401, [90]):
			with self.subTest(value=value):
				with self.assertRaises(frappe.ValidationError):
					parse_bounded_seconds(value, label="Timeout (Seconds)")

	def test_deadline_order_requires_a_bounded_positive_hard_timeout(self):
		validate_deadline_order(
			60, 90, soft_label="Soft Timeout (Seconds)", timeout_label="Timeout (Seconds)"
		)
		for hard in (None, 0):
			with self.subTest(hard=hard):
				with self.assertRaisesRegex(
					frappe.ValidationError, "requires Timeout \\(Seconds\\) to be set"
				):
					validate_deadline_order(
						60, hard, soft_label="Soft Timeout (Seconds)", timeout_label="Timeout (Seconds)"
					)
		for soft, hard in ((60, 60), (90, 60)):
			with self.subTest(soft=soft, hard=hard):
				with self.assertRaisesRegex(
					frappe.ValidationError, "must be less than Timeout \\(Seconds\\)"
				):
					validate_deadline_order(
						soft, hard, soft_label="Soft Timeout (Seconds)", timeout_label="Timeout (Seconds)"
					)

	# ------------------------------------------------- agent config (save)

	def make_agent_config(self, row):
		return SimpleNamespace(agent_level="1", name="parent-agent", sub_agents=[row])

	def save_sub_agent_row(self, row):
		# frappe.db is request-scoped; bind it only for the child lookup.
		frappe.local.db = SimpleNamespace(
			get_value=lambda *_args, **_kwargs: frappe._dict(agent_level="2", disabled=0)
		)
		try:
			AIAgent.validate_sub_agents(self.make_agent_config(row))
		finally:
			del frappe.local.db
		return row

	def test_agent_config_save_normalizes_soft_deadline_rows(self):
		row = self.save_sub_agent_row(make_sub_agent_row(soft_timeout_seconds="60"))
		self.assertEqual(row.soft_timeout_seconds, 60)
		self.assertEqual(row.timeout_seconds, 90)

		for value in (None, "", "0", 0):
			with self.subTest(value=value):
				row = self.save_sub_agent_row(make_sub_agent_row(soft_timeout_seconds=value))
				self.assertEqual(row.soft_timeout_seconds, 0, "unset deadlines normalize to 0 on save")

	def test_agent_config_save_enforces_strict_soft_deadline_order(self):
		without_hard = make_sub_agent_row(soft_timeout_seconds=60, timeout_seconds=0)
		with self.assertRaisesRegex(frappe.ValidationError, "requires Timeout \\(Seconds\\) to be set"):
			self.save_sub_agent_row(without_hard)

		for soft, hard in ((90, 90), (120, 90)):
			with self.subTest(soft=soft, hard=hard):
				with self.assertRaisesRegex(
					frappe.ValidationError, "must be less than Timeout \\(Seconds\\)"
				):
					self.save_sub_agent_row(
						make_sub_agent_row(soft_timeout_seconds=soft, timeout_seconds=hard)
					)

		for soft, hard, message in (
			("60.5", 90, "whole number"),
			(True, 90, "whole number"),
			(86401, 86400, "at most 86400"),
			(60, "90.5", "whole number"),
		):
			with self.subTest(soft=soft, hard=hard):
				with self.assertRaisesRegex(frappe.ValidationError, message):
					self.save_sub_agent_row(
						make_sub_agent_row(soft_timeout_seconds=soft, timeout_seconds=hard)
					)

	# --------------------------------------------------------- resolution

	def resolve_rows(self, rows):
		child = SimpleNamespace(
			agent_name="Researcher",
			agent_key="researcher",
			description="Probe child.",
			agent_level="2",
		)
		resolved_child = SimpleNamespace(
			prompt="Help the parent run.",
			model=ResolvedModel(
				name="Test Model",
				provider="openai",
				provider_account="provider-account",
				provider_type="openai",
				model_id="test-model",
				settings={},
			),
			tools=(),
			skills=(),
			mcp_servers=(),
			timeout=60.0,
			retries=1,
		)
		with (
			patch("afaa.ai.runtime.frappe.get_doc", return_value=child),
			patch("afaa.ai.runtime.resolve_ai_agent", return_value=resolved_child),
		):
			return _resolve_sub_agents(
				SimpleNamespace(name="parent-agent", sub_agents=rows), None, require_enabled=True
			)

	def test_resolution_normalizes_and_propagates_soft_deadlines(self):
		delegates = self.resolve_rows([make_sub_agent_row(soft_timeout_seconds="60")])
		self.assertEqual(delegates[0].soft_timeout_seconds, 60)
		self.assertIsInstance(delegates[0].soft_timeout_seconds, int)
		self.assertEqual(delegates[0].timeout_seconds, 90.0)

		self.assertIsNone(self.resolve_rows([make_sub_agent_row()])[0].soft_timeout_seconds)

		legacy_row = SimpleNamespace(
			sub_agent="researcher-child", max_calls=2, request_limit=0, timeout_seconds=90
		)
		self.assertIsNone(self.resolve_rows([legacy_row])[0].soft_timeout_seconds)

	def test_resolution_fails_closed_on_drifted_soft_deadlines(self):
		for row in (
			make_sub_agent_row(soft_timeout_seconds=120, timeout_seconds=90),
			make_sub_agent_row(soft_timeout_seconds=60, timeout_seconds=0),
			make_sub_agent_row(soft_timeout_seconds="60.5"),
			make_sub_agent_row(soft_timeout_seconds=60, timeout_seconds=86401),
		):
			with self.subTest(soft=row.soft_timeout_seconds, hard=row.timeout_seconds):
				with self.assertRaises(frappe.ValidationError):
					self.resolve_rows([row])

	# ------------------------------------------------- v4 contract builder

	def resolve_contract_with_sub_agent(self, sub_agent):
		resolved = make_resolved_agent()
		resolved.agent_level = "1"
		resolved.sub_agents = (sub_agent,)
		account = SimpleNamespace(
			name="provider-account", get_password=lambda *_args, **_kwargs: "server-only-secret"
		)
		with (
			patch("afaa.ai.external_runtime.resolve_ai_agent", return_value=resolved),
			patch("afaa.ai.external_runtime.frappe.get_doc", return_value=account),
			patch(
				"afaa.ai.external_runtime.get_tool_definition",
				side_effect=lambda key: make_tool_definition(key),
			),
		):
			return resolve_external_runtime("reviewer", include_sub_agents=True)

	def test_builder_propagates_soft_deadline_into_the_v4_contract(self):
		config = self.resolve_contract_with_sub_agent(
			make_resolved_sub_agent(soft_timeout_seconds=60, timeout_seconds=90.0)
		)
		self.assertIsInstance(config, SubAgentAwareExternalRuntimeConfig)
		self.assertEqual(config.schema_version, 4)
		delegate = config.sub_agents[0]
		self.assertEqual(delegate.soft_timeout_seconds, 60)
		self.assertIsInstance(delegate.soft_timeout_seconds, int)
		payload = config.model_dump(mode="json", by_alias=True)
		self.assertEqual(payload["subAgents"][0]["softTimeoutSeconds"], 60)

		unset = self.resolve_contract_with_sub_agent(make_resolved_sub_agent())
		self.assertIsNone(unset.sub_agents[0].soft_timeout_seconds)
		self.assertNotIn("softTimeoutSeconds", unset.model_dump(mode="json", by_alias=True)["subAgents"][0])

	def test_soft_deadline_mutates_fingerprint_and_absent_keeps_legacy_shape(self):
		configured = self.resolve_contract_with_sub_agent(
			make_resolved_sub_agent(soft_timeout_seconds=60, timeout_seconds=90.0)
		)
		unset = self.resolve_contract_with_sub_agent(make_resolved_sub_agent())
		self.assertNotEqual(configured.configuration_fingerprint, unset.configuration_fingerprint)

		payload = unset.model_dump(mode="json", by_alias=True)
		self.assertNotIn("softTimeoutSeconds", payload["subAgents"][0])
		payload.pop("configurationFingerprint")
		payload["model"].pop("apiKey")
		if payload["model"].get("baseUrl") is None:
			payload["model"].pop("baseUrl")
		self.assertEqual(
			unset.configuration_fingerprint,
			configuration_fingerprint(payload),
			"the unset soft deadline must keep the legacy payload shape and fingerprint",
		)
