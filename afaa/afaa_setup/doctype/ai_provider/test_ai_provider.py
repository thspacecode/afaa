# Copyright (c) 2026, SpaceCode and Contributors
# See license.txt

import frappe

from afaa.afaa_setup.doctype.ai_provider.ai_provider_dashboard import get_data, get_open_count
from afaa.ai.provider import get_available_provider_types
from afaa.tests.utils import AFAATestSuite, boot_strap_test_master_data


class TestAIProvider(AFAATestSuite):
	def test_installed_provider_types_are_available(self):
		providers = {item["value"] for item in get_available_provider_types()}
		self.assertIn("openai", providers)
		self.assertIn("openai_codex", providers)
		self.assertIn("google", providers)
		self.assertIn("zai", providers)
		self.assertIn("moonshot", providers)
		self.assertNotIn("anthropic", providers)

	def test_only_openai_compatible_pinned_providers_advertise_base_url_override(self):
		providers = {item["value"]: item for item in get_available_provider_types()}
		self.assertTrue(providers["zai"]["supports_base_url_override"])
		self.assertTrue(providers["moonshot"]["supports_base_url_override"])
		self.assertFalse(providers["openai"]["supports_base_url_override"])
		self.assertFalse(providers["openai_codex"]["supports_base_url_override"])
		self.assertFalse(providers["google"]["supports_base_url_override"])

	def test_base_url_requires_supported_provider_and_https(self):
		unsupported = frappe.new_doc("AI Provider")
		unsupported.update(
			{"provider_type": "google", "disabled": 1, "base_url": "https://proxy.example.com/v1"}
		)
		self.assertRaises(frappe.ValidationError, unsupported.insert)

		insecure = frappe.new_doc("AI Provider")
		insecure.update(
			{
				"provider_type": "zai",
				"disabled": 1,
				"base_url": "http://api.z.ai/api/coding/paas/v4",
			}
		)
		self.assertRaises(frappe.ValidationError, insecure.insert)

		spaced = frappe.new_doc("AI Provider")
		spaced.update(
			{"provider_type": "zai", "disabled": 1, "base_url": "https://api.z.ai/api extra"}
		)
		self.assertRaises(frappe.ValidationError, spaced.insert)

	def test_valid_coding_plan_base_url_is_accepted(self):
		provider = frappe.new_doc("AI Provider")
		provider.update(
			{
				"provider_type": "zai",
				"disabled": 1,
				"base_url": " https://api.z.ai/api/coding/paas/v4 ",
			}
		)
		provider.insert(ignore_permissions=True)

		self.assertEqual(provider.base_url, "https://api.z.ai/api/coding/paas/v4")
