# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from afaa.ai.external_runtime import (
	BundleExternalRuntimeSkill,
	ExternalRuntimeConfig,
	ExternalRuntimeSkill,
	StructuredExternalRuntimeConfig,
	build_structured_runtime_capabilities,
	configuration_fingerprint,
	resolve_external_runtime,
)
from afaa.ai.skill_bundles import EMPTY_BUNDLE_DIGEST, SkillBundleReference
from afaa.ai.tools import EXTERNAL_READ_TOOL_METHODS


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
						**( {"baseUrl": resolved.model.base_url} if resolved.model.base_url else {} ),
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
