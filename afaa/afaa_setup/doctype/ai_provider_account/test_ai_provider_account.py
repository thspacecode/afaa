# Copyright (c) 2026, SpaceCode and Contributors
# See license.txt

import frappe

from afaa.afaa_setup.patches.provision_openai_codex_connected_app import execute as provision_connected_app
from afaa.ai.oauth.openai_codex import CONNECTED_APP_NAME, DEFAULT_CLIENT_ID, DEFAULT_SCOPES
from afaa.ai.provider import get_provider_account, get_provider_class
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

	def test_api_key_provider_capabilities_remain_unchanged(self):
		account = frappe.get_doc("AI Provider Account", boot_strap_test_master_data.provider_account)
		provider_class = get_provider_class("openai")

		self.assertEqual(provider_class.supported_auth_methods, ("API Key",))
		self.assertFalse(provider_class.supports_oauth_connection)
		self.assertEqual(account.authentication_method, "API Key")

	def test_codex_account_uses_managed_oauth_connected_app(self):
		provision_connected_app()
		if not frappe.db.exists("AI Provider", "openai_codex"):
			frappe.get_doc(
				{
					"doctype": "AI Provider",
					"provider_type": "openai_codex",
					"disabled": 0,
				}
			).insert(ignore_permissions=True)

		account = frappe.get_doc(
			{
				"doctype": "AI Provider Account",
				"account_name": "AFAA Test - OpenAI Codex",
				"provider": "openai_codex",
				"disabled": 0,
			}
		).insert(ignore_permissions=True)

		self.assertEqual(account.authentication_method, "OAuth Subscription")
		self.assertEqual(account.connected_app, CONNECTED_APP_NAME)
		self.assertEqual(account.oauth_status, "Not Connected")
		self.assertFalse(get_provider_class("openai_codex")(account).is_authenticated())
		with self.assertRaisesRegex(frappe.ValidationError, "Authenticate this AI Provider Account"):
			account.sync_models()

	def test_managed_connected_app_is_idempotent_and_has_no_secret(self):
		provision_connected_app()
		provision_connected_app()
		connected_app = frappe.get_doc("Connected App", CONNECTED_APP_NAME)

		self.assertEqual(connected_app.client_id, DEFAULT_CLIENT_ID)
		self.assertEqual(connected_app.token_uri, "https://auth.openai.com/oauth/token")
		self.assertEqual(connected_app.revocation_uri, "https://auth.openai.com/oauth/revoke")
		self.assertEqual(tuple(row.scope for row in connected_app.scopes), DEFAULT_SCOPES)
		self.assertFalse(connected_app.get_password("client_secret", raise_exception=False))

	def test_rejects_non_object_account_settings(self):
		account = frappe.get_doc("AI Provider Account", boot_strap_test_master_data.provider_account)
		account.account_settings = "[]"

		with self.assertRaisesRegex(frappe.ValidationError, "must be a JSON object"):
			account.save()
