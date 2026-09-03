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
		self.assertIn("google", providers)
		self.assertNotIn("anthropic", providers)
