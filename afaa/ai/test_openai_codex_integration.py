# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

"""Integration coverage for the complete Codex subscription account lifecycle.

OpenAI is replaced at the HTTP boundary. All AFAA/Frappe provider, OAuth, DocType,
Token Cache encryption, model synchronization, refresh, and disconnect code is real.
"""

import asyncio
import base64
import json
import uuid
from unittest.mock import Mock, patch

import frappe

from afaa.afaa_setup.patches.provision_openai_codex_connected_app import execute as provision_connected_app
from afaa.ai.oauth.openai_codex import CONNECTED_APP_NAME
from afaa.ai.oauth.openai_codex_service import FLOW_KEY_PREFIX
from afaa.ai.provider import build_model, get_provider_class
from afaa.ai.provider.openai_codex_provider import CODEX_BASE_URL
from afaa.tests.utils import AFAATestSuite


class IntegrationTestOpenAICodexSubscription(AFAATestSuite):
	def setUp(self):
		self.user_context = self.set_create_user(["AI Manager"])
		self.user = self.user_context.__enter__()
		provision_connected_app()
		if not frappe.db.exists("AI Provider", "openai_codex"):
			frappe.get_doc(
				{
					"doctype": "AI Provider",
					"provider_type": "openai_codex",
					"disabled": 0,
				}
			).insert(ignore_permissions=True)
		self.account = frappe.get_doc(
			{
				"doctype": "AI Provider Account",
				"account_name": f"Codex Integration {uuid.uuid4().hex}",
				"provider": "openai_codex",
				"disabled": 0,
			}
		).insert(ignore_permissions=True)
		self.flow_id = None

	def tearDown(self):
		if self.flow_id:
			frappe.cache.delete_value(FLOW_KEY_PREFIX + self.flow_id)
		# A serialized refresh commits its Token Cache rotation before releasing the
		# lock, so explicitly remove any test records made durable by that commit.
		frappe.db.rollback()
		self.user_context.__exit__(None, None, None)
		token_cache = frappe.get_doc("Connected App", CONNECTED_APP_NAME).get_token_cache(self.user)
		if token_cache:
			token_cache.delete(ignore_permissions=True, force=True)
		model_name = frappe.db.get_value(
			"AI Model", {"provider": "openai_codex", "model_id": "codex-integration-model"}
		)
		if model_name:
			frappe.delete_doc("AI Model", model_name, ignore_permissions=True, force=True)
		if frappe.db.exists("AI Provider Account", self.account.name):
			frappe.delete_doc("AI Provider Account", self.account.name, ignore_permissions=True, force=True)
		if frappe.db.exists("User", self.user):
			frappe.delete_doc("User", self.user, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDown()

	def test_connect_sync_build_refresh_and_disconnect(self):
		initial_tokens = {
			"access_token": "integration-access-1",
			"refresh_token": "integration-refresh-1",
			"id_token": make_jwt(
				{
					"email": "subscriber@example.test",
					"https://api.openai.com/auth": {
						"chatgpt_account_id": "integration-account",
						"chatgpt_plan_type": "pro",
					},
				}
			),
			"expires_in": 3600,
			"scope": "openid profile email offline_access",
		}
		provider_responses = iter(
			[
				response(
					200,
					{
						"device_auth_id": "integration-device-secret",
						"user_code": "ABCD-EFGH",
						"verification_uri": "https://auth.openai.com/codex/device",
						"interval": 5,
						"expires_in": 900,
					},
				),
				response(
					200,
					{
						"authorization_code": "integration-authorization-secret",
						"code_verifier": "integration-verifier-secret",
					},
				),
				response(200, initial_tokens),
				response(
					200,
					{
						"access_token": "integration-access-2",
						"refresh_token": "integration-refresh-2",
						"expires_in": 3600,
					},
				),
				response(200, {}),
			]
		)
		provider_requests = []

		def request(_session, method, url, **kwargs):
			provider_requests.append((method, url, kwargs))
			return next(provider_responses)

		catalog_response = response(
			200,
			{
				"models": [
					{
						"slug": "codex-integration-model",
						"supported_in_api": True,
						"visibility": "list",
					}
				]
			},
		)

		with patch(
			"afaa.ai.oauth.openai_codex.requests.Session.request",
			autospec=True,
			side_effect=request,
		):
			started = self.account.start_oauth_login()
			self.flow_id = started["flow_id"]
			self.assertNotIn("integration-device-secret", repr(started))
			allow_next_poll(self.flow_id)
			completed = self.account.poll_oauth_login(self.flow_id)

			self.assertEqual(completed, {"status": "complete"})
			self.assertNotIn("integration-access-1", repr(completed))
			self.account.reload()
			self.assertEqual(self.account.oauth_status, "Connected")
			self.assertEqual(self.account.external_account_id, "integration-account")
			self.assertEqual(self.account.connected_user, self.user)

			token_cache = frappe.get_doc("Connected App", CONNECTED_APP_NAME).get_token_cache(self.user)
			self.assertEqual(token_cache.get_password("access_token"), "integration-access-1")
			self.assertNotEqual(token_cache.access_token, "integration-access-1")

			with patch(
				"afaa.ai.provider.openai_codex_provider.requests.get",
				return_value=catalog_response,
			) as get_catalog:
				report = self.account.sync_models()

			self.assertEqual(report["created"], ["codex-integration-model"])
			get_catalog.assert_called_once()
			self.assertEqual(get_catalog.call_args.args[0], f"{CODEX_BASE_URL}/models")

			model_doc = frappe.get_doc(
				"AI Model",
				{"provider": "openai_codex", "model_id": "codex-integration-model"},
			)
			model = build_model(model_doc, self.account)
			self.assertEqual(model.model_name, "codex-integration-model")
			self.assertFalse(model.settings["openai_store"])
			asyncio.run(model.client.close())

			token_cache.db_set("expires_in", 1)
			adapter = get_provider_class("openai_codex")(self.account)
			self.assertEqual(adapter.resolve_access_token(), "integration-access-2")
			token_cache.reload()
			self.assertEqual(token_cache.get_password("refresh_token"), "integration-refresh-2")

			disconnected = self.account.disconnect_oauth()

		self.assertEqual(disconnected, {"status": "disconnected"})
		self.assertFalse(frappe.db.exists("Token Cache", token_cache.name))
		self.account.reload()
		self.assertEqual(self.account.oauth_status, "Not Connected")
		self.assertFalse(self.account.external_account_id)

		self.assertEqual(
			[url for _method, url, _kwargs in provider_requests],
			[
				"https://auth.openai.com/api/accounts/deviceauth/usercode",
				"https://auth.openai.com/api/accounts/deviceauth/token",
				"https://auth.openai.com/oauth/token",
				"https://auth.openai.com/oauth/token",
				"https://auth.openai.com/oauth/revoke",
			],
		)
		self.assertEqual(
			provider_requests[3][2]["json"]["refresh_token"],
			"integration-refresh-1",
		)
		self.assertEqual(
			provider_requests[4][2]["json"]["token"],
			"integration-refresh-2",
		)


def response(status_code, payload):
	result = Mock(status_code=status_code, ok=200 <= status_code < 300)
	result.json.return_value = payload
	return result


def allow_next_poll(flow_id):
	key = FLOW_KEY_PREFIX + flow_id
	state = frappe.cache.get_value(key, expires=True, use_local_cache=False)
	state["next_allowed_poll_at"] = 0
	frappe.cache.set_value(key, state, expires_in_sec=900)


def make_jwt(payload):
	def encode(value):
		return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

	return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"
