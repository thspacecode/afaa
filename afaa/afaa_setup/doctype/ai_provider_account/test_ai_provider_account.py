# Copyright (c) 2026, SpaceCode and Contributors
# See license.txt

import frappe

from afaa.ai.provider import get_provider_account
from afaa.tests.utils import AFAATestSuite, boot_strap_test_master_data


class TestAIProviderAccount(AFAATestSuite):
	def test_bootstrap_account_resolves_for_provider(self):
		account = get_provider_account(
			boot_strap_test_master_data.provider,
			boot_strap_test_master_data.provider_account,
		)

		self.assertEqual(account.name, boot_strap_test_master_data.provider_account)
		self.assertEqual(account.provider, boot_strap_test_master_data.provider)
		self.assertFalse(account.disabled)

	def test_rejects_non_object_account_settings(self):
		account = frappe.get_doc("AI Provider Account", boot_strap_test_master_data.provider_account)
		account.account_settings = "[]"

		with self.assertRaisesRegex(frappe.ValidationError, "must be a JSON object"):
			account.save()
