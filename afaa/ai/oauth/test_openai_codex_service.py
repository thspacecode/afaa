# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import base64
import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from afaa.afaa_setup.patches.provision_openai_codex_connected_app import execute as provision_connected_app
from afaa.ai.oauth.openai_codex import (
	CONNECTED_APP_NAME,
	AuthorizationGrant,
	CodexOAuthError,
	CodexOAuthSlowDown,
	DeviceAuthorization,
	OAuthTokens,
)
from afaa.ai.oauth.openai_codex_service import (
	FLOW_KEY_PREFIX,
	CodexAuthenticationError,
	CodexReconnectRequiredError,
	CodexTemporaryRefreshError,
	CodexTokenResult,
	CodexUsageLimitError,
	CodexWorkspaceAuthorizationError,
	_persist_token_cache,
	report_codex_authentication_failure,
	resolve_codex_access_token,
)
from afaa.tests.utils import AFAATestSuite


class FakeOAuthClient:
	def __init__(self):
		self.poll_result = AuthorizationGrant("authorization-code", "pkce-verifier")
		self.refresh_result = OAuthTokens("new-access", "new-refresh", None, 3600, "openid email")
		self.exchange_result = make_tokens()
		self.revocation_error = None
		self.poll_calls = 0
		self.refresh_calls = 0
		self.revoked_tokens = []

	def request_device_authorization(self):
		return DeviceAuthorization(
			verification_url="https://issuer.test/codex/device",
			user_code="ABCD-EFGH",
			device_auth_id="provider-device-secret",
			interval=5,
			expires_in=900,
		)

	def poll_device_authorization(self, device):
		self.poll_calls += 1
		if isinstance(self.poll_result, Exception):
			raise self.poll_result
		return self.poll_result

	def exchange_authorization_code(self, grant):
		return self.exchange_result

	def refresh(self, refresh_token):
		self.refresh_calls += 1
		if isinstance(self.refresh_result, Exception):
			raise self.refresh_result
		return self.refresh_result

	def revoke(self, refresh_token):
		self.revoked_tokens.append(refresh_token)
		if self.revocation_error:
			raise self.revocation_error


class TestOpenAICodexOAuthService(AFAATestSuite):
	def setUp(self):
		self.user_context = self.set_create_user(["AI Manager"])
		self.user = self.user_context.__enter__()
		provision_connected_app()
		if not frappe.db.exists("AI Provider", "openai_codex"):
			frappe.get_doc({"doctype": "AI Provider", "provider_type": "openai_codex", "disabled": 0}).insert(
				ignore_permissions=True
			)
		self.account = frappe.get_doc(
			{
				"doctype": "AI Provider Account",
				"account_name": f"AFAA Codex OAuth Test {uuid.uuid4().hex}",
				"provider": "openai_codex",
				"disabled": 0,
			}
		).insert(ignore_permissions=True)
		self.flow_ids = []

	def tearDown(self):
		for flow_id in self.flow_ids:
			frappe.cache.delete_value(FLOW_KEY_PREFIX + flow_id)
		# Token refresh commits while holding its cross-process lock. Roll back first,
		# then remove any test records made durable by a refresh.
		frappe.db.rollback()
		self.user_context.__exit__(None, None, None)
		token_cache = frappe.get_doc("Connected App", CONNECTED_APP_NAME).get_token_cache(self.user)
		if token_cache:
			token_cache.delete(ignore_permissions=True, force=True)
		if frappe.db.exists("AI Provider Account", self.account.name):
			frappe.delete_doc("AI Provider Account", self.account.name, ignore_permissions=True, force=True)
		if frappe.db.exists("User", self.user):
			frappe.delete_doc("User", self.user, ignore_permissions=True, force=True)
		frappe.db.commit()
		super().tearDown()

	def test_device_authorization_maps_account_permission_error(self):
		client = FakeOAuthClient()
		client.request_device_authorization = Mock(
			side_effect=CodexOAuthError("device authorization request", 403)
		)

		with (
			patch("afaa.ai.oauth.openai_codex_service.get_codex_oauth_client", return_value=client),
			self.assertRaisesRegex(CodexAuthenticationError, "not authorized for Codex"),
		):
			self.account.start_oauth_login()

	def test_device_flow_completion_is_user_bound_and_persists_encrypted_tokens(self):
		client = FakeOAuthClient()
		with patch("afaa.ai.oauth.openai_codex_service.get_codex_oauth_client", return_value=client):
			started = self.account.start_oauth_login()
			self.flow_ids.append(started["flow_id"])

			self.assertEqual(
				set(started),
				{"flow_id", "verification_url", "user_code", "poll_interval", "expires_in"},
			)
			self.assertNotIn("provider-device-secret", repr(started))
			pending = self.account.poll_oauth_login(started["flow_id"])
			self.assertEqual(pending["status"], "pending")
			self.assertEqual(client.poll_calls, 0)

			allow_next_poll(started["flow_id"])
			completed = self.account.poll_oauth_login(started["flow_id"])

		self.assertEqual(completed, {"status": "complete"})
		self.assertNotIn("access-secret", repr(completed))
		self.assertIsNone(frappe.cache.get_value(FLOW_KEY_PREFIX + started["flow_id"], expires=True))

		self.account.reload()
		self.assertEqual(self.account.oauth_status, "Connected")
		self.assertEqual(self.account.connected_user, frappe.session.user)
		self.assertEqual(self.account.external_account_id, "account-123")
		self.assertEqual(self.account.external_account_email, "user@example.test")
		self.assertEqual(self.account.subscription_plan, "pro")

		token_cache = frappe.get_doc("Connected App", self.account.connected_app).get_token_cache(
			frappe.session.user
		)
		self.assertEqual(token_cache.get_password("access_token"), "access-secret")
		self.assertEqual(token_cache.get_password("refresh_token"), "refresh-secret")
		self.assertNotEqual(token_cache.access_token, "access-secret")

	def test_flow_cannot_be_polled_by_another_user_or_account(self):
		client = FakeOAuthClient()
		with patch("afaa.ai.oauth.openai_codex_service.get_codex_oauth_client", return_value=client):
			started = self.account.start_oauth_login()
			self.flow_ids.append(started["flow_id"])

			other_account = frappe.copy_doc(self.account)
			other_account.account_name = f"Other Codex Account {uuid.uuid4().hex}"
			other_account.insert(ignore_permissions=True)
			with self.assertRaises(frappe.PermissionError):
				other_account.poll_oauth_login(started["flow_id"])

			with self.set_create_user(["AI Manager"]):
				with self.assertRaises(frappe.PermissionError):
					self.account.poll_oauth_login(started["flow_id"])

	def test_provider_pending_and_flow_timeout(self):
		client = FakeOAuthClient()
		client.poll_result = None
		with patch("afaa.ai.oauth.openai_codex_service.get_codex_oauth_client", return_value=client):
			started = self.account.start_oauth_login()
			self.flow_ids.append(started["flow_id"])
			allow_next_poll(started["flow_id"])
			pending = self.account.poll_oauth_login(started["flow_id"])
			self.assertEqual(pending["status"], "pending")
			self.assertEqual(client.poll_calls, 1)

			expire_flow(started["flow_id"])
			expired = self.account.poll_oauth_login(started["flow_id"])

		self.assertEqual(
			expired,
			{
				"status": "expired",
				"message": "Authorization expired; restart connection.",
			},
		)
		self.assertEqual(client.poll_calls, 1)

	def test_slow_down_increases_poll_interval(self):
		client = FakeOAuthClient()
		client.poll_result = CodexOAuthSlowDown("device authorization poll", 400)
		with patch("afaa.ai.oauth.openai_codex_service.get_codex_oauth_client", return_value=client):
			started = self.account.start_oauth_login()
			self.flow_ids.append(started["flow_id"])
			allow_next_poll(started["flow_id"])
			result = self.account.poll_oauth_login(started["flow_id"])

		self.assertEqual(result["status"], "pending")
		self.assertEqual(result["poll_interval"], 10)
		self.assertEqual(client.poll_calls, 1)

	def test_missing_account_claim_fails_without_persisting_tokens(self):
		client = FakeOAuthClient()
		client.exchange_result = OAuthTokens(
			"access-secret",
			"refresh-secret",
			make_jwt({"email": "user@example.test"}),
			3600,
		)
		with patch("afaa.ai.oauth.openai_codex_service.get_codex_oauth_client", return_value=client):
			started = self.account.start_oauth_login()
			self.flow_ids.append(started["flow_id"])
			allow_next_poll(started["flow_id"])
			result = self.account.poll_oauth_login(started["flow_id"])

		self.assertEqual(result["status"], "failed")
		self.assertEqual(result["message"], "Unable to determine ChatGPT account.")
		self.account.reload()
		self.assertEqual(self.account.oauth_status, "Error")
		self.assertIsNone(
			frappe.get_doc("Connected App", self.account.connected_app).get_token_cache(frappe.session.user)
		)

	def test_proactive_refresh_enforces_minimum_validity_and_rechecks_after_lock(self):
		token_cache = frappe.get_doc(
			{
				"doctype": "Token Cache",
				"connected_app": self.account.connected_app,
				"user": frappe.session.user,
			}
		)
		_persist_token_cache(token_cache, OAuthTokens("old-access", "old-refresh", None, 200))
		self.account.db_set(
			{
				"oauth_status": "Connected",
				"connected_user": frappe.session.user,
				"external_account_id": "account-123",
			}
		)

		client = FakeOAuthClient()
		with patch("afaa.ai.oauth.openai_codex_service.get_codex_oauth_client", return_value=client):
			first = resolve_codex_access_token(self.account, minimum_validity=0)
			second = resolve_codex_access_token(self.account)

		self.assertIsInstance(first, CodexTokenResult)
		self.assertEqual(first.access_token.get_secret_value(), "new-access")
		self.assertEqual(second.access_token.get_secret_value(), "new-access")
		self.assertGreater(first.expires_at, int(__import__("time").time()) + 3500)
		self.assertNotIn("new-access", repr(first))
		self.assertEqual(client.refresh_calls, 1)
		token_cache.reload()
		self.assertEqual(token_cache.get_password("refresh_token"), "new-refresh")

	def test_forced_refresh_rotates_token_even_when_cached_token_is_valid(self):
		token_cache = frappe.get_doc(
			{
				"doctype": "Token Cache",
				"connected_app": self.account.connected_app,
				"user": frappe.session.user,
			}
		)
		_persist_token_cache(token_cache, OAuthTokens("old-access", "old-refresh", None, 3600))
		self.account.db_set(
			{
				"oauth_status": "Connected",
				"connected_user": frappe.session.user,
				"external_account_id": "account-123",
			}
		)
		client = FakeOAuthClient()

		with patch("afaa.ai.oauth.openai_codex_service.get_codex_oauth_client", return_value=client):
			result = resolve_codex_access_token(self.account, minimum_validity=0, force_refresh=True)

		self.assertEqual(result.access_token.get_secret_value(), "new-access")
		self.assertEqual(client.refresh_calls, 1)
		token_cache.reload()
		self.assertEqual(token_cache.get_password("refresh_token"), "new-refresh")

	def test_forced_waiter_reuses_token_rotated_by_another_waiter(self):
		now = frappe.utils.now_datetime()
		token_cache = SimpleNamespace(
			name="codex-token-cache",
			modified=now,
			expires_in=3600,
			reload=Mock(),
			get_expires_in=Mock(return_value=3600),
			get_password=Mock(return_value="rotated-access"),
		)
		account = SimpleNamespace(
			name="codex-account",
			disabled=0,
			oauth_status="Connected",
			connected_user="codex-user",
			reload=Mock(),
		)

		class ConcurrentRotation:
			def __enter__(self):
				token_cache.modified = frappe.utils.add_to_date(now, seconds=1)

			def __exit__(self, *args):
				return False

		with (
			patch("afaa.ai.oauth.openai_codex_service._validate_codex_account"),
			patch("afaa.ai.oauth.openai_codex_service._get_token_cache", return_value=token_cache),
			patch("afaa.ai.oauth.openai_codex_service.filelock", return_value=ConcurrentRotation()),
			patch("afaa.ai.oauth.openai_codex_service.get_codex_oauth_client") as oauth_client,
		):
			result = resolve_codex_access_token(account, force_refresh=True)

		self.assertEqual(result.access_token.get_secret_value(), "rotated-access")
		oauth_client.assert_not_called()

	def test_refresh_errors_have_sanitized_categories(self):
		token_cache = frappe.get_doc(
			{
				"doctype": "Token Cache",
				"connected_app": self.account.connected_app,
				"user": frappe.session.user,
			}
		)
		_persist_token_cache(token_cache, OAuthTokens("old-access", "old-refresh", None, 1))
		self.account.db_set(
			{
				"oauth_status": "Connected",
				"connected_user": frappe.session.user,
				"external_account_id": "account-123",
			}
		)

		cases = (
			(403, CodexWorkspaceAuthorizationError, "workspace_authorization"),
			(429, CodexUsageLimitError, "usage_limit"),
			(503, CodexTemporaryRefreshError, "temporary_refresh_failure"),
		)
		for status_code, error_type, category in cases:
			self.account.db_set("oauth_status", "Connected")
			client = FakeOAuthClient()
			client.refresh_result = CodexOAuthError("token refresh", status_code)
			with (
				self.subTest(status_code=status_code),
				patch("afaa.ai.oauth.openai_codex_service.get_codex_oauth_client", return_value=client),
				self.assertRaises(error_type) as raised,
			):
				resolve_codex_access_token(self.account)
			self.assertEqual(raised.exception.category, category)

	def test_disconnect_deletes_local_credentials_when_revocation_fails(self):
		token_cache = frappe.get_doc(
			{
				"doctype": "Token Cache",
				"connected_app": self.account.connected_app,
				"user": frappe.session.user,
			}
		)
		_persist_token_cache(token_cache, OAuthTokens("old-access", "old-refresh", None, 3600))
		self.account.db_set(
			{
				"oauth_status": "Connected",
				"connected_user": frappe.session.user,
				"external_account_id": "account-123",
			}
		)
		client = FakeOAuthClient()
		client.revocation_error = CodexOAuthError("token revocation", 503)

		with (
			patch("afaa.ai.oauth.openai_codex_service.get_codex_oauth_client", return_value=client),
			patch("afaa.ai.oauth.openai_codex_service.frappe.log_error") as log_error,
			patch("afaa.ai.external_runtime.notify_provider_account_invalidated") as invalidated,
		):
			result = self.account.disconnect_oauth()

		self.assertEqual(result, {"status": "disconnected"})
		self.assertFalse(frappe.db.exists("Token Cache", token_cache.name))
		self.account.reload()
		self.assertEqual(self.account.oauth_status, "Not Connected")
		self.assertFalse(self.account.connected_user)
		self.assertNotIn("old-refresh", repr(log_error.call_args))
		invalidated.assert_called_once_with(self.account.name)

	def test_missing_oauth_token_never_falls_back_to_environment_api_key(self):
		self.account.db_set(
			{
				"oauth_status": "Connected",
				"connected_user": frappe.session.user,
				"external_account_id": "account-123",
			}
		)
		with (
			patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-be-used"}),
			self.assertRaises(CodexReconnectRequiredError),
		):
			resolve_codex_access_token(self.account)

	def test_guest_cannot_start_oauth(self):
		with self.set_user("Guest"):
			with self.assertRaises(frappe.PermissionError):
				self.account.start_oauth_login()


class TestOpenAICodexRefreshFailure(AFAATestSuite):
	def test_permanent_refresh_failure_marks_account_expired(self):
		provision_connected_app()
		if not frappe.db.exists("AI Provider", "openai_codex"):
			frappe.get_doc({"doctype": "AI Provider", "provider_type": "openai_codex", "disabled": 0}).insert(
				ignore_permissions=True
			)
		account = frappe.get_doc(
			{
				"doctype": "AI Provider Account",
				"account_name": f"AFAA Codex Refresh Failure {uuid.uuid4().hex}",
				"provider": "openai_codex",
				"disabled": 0,
			}
		).insert(ignore_permissions=True)
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": f"codex_refresh_{uuid.uuid4().hex}@afaa.test",
				"first_name": "Codex Refresh Test",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
		token_cache = frappe.get_doc(
			{
				"doctype": "Token Cache",
				"connected_app": account.connected_app,
				"user": user.name,
			}
		)
		_persist_token_cache(token_cache, OAuthTokens("old-access", "old-refresh", None, 1))
		account.db_set(
			{
				"oauth_status": "Connected",
				"connected_user": user.name,
				"external_account_id": "account-123",
			}
		)
		client = FakeOAuthClient()
		client.refresh_result = CodexOAuthError("token refresh", 401)

		try:
			with (
				patch("afaa.ai.oauth.openai_codex_service.get_codex_oauth_client", return_value=client),
				patch("afaa.ai.external_runtime.notify_provider_account_invalidated") as invalidated,
			):
				with self.assertRaisesRegex(CodexReconnectRequiredError, "reconnect account"):
					resolve_codex_access_token(account)
			account.reload()
			self.assertEqual(account.oauth_status, "Expired")
			invalidated.assert_called_once_with(account.name)

			account.db_set("oauth_status", "Connected")
			with patch("afaa.ai.external_runtime.notify_provider_account_invalidated") as invalidated:
				report_codex_authentication_failure(account)
			invalidated.assert_called_once_with(account.name)
			account.reload()
			self.assertEqual(account.oauth_status, "Expired")
		finally:
			frappe.delete_doc("Token Cache", token_cache.name, ignore_permissions=True, force=True)
			frappe.delete_doc("AI Provider Account", account.name, ignore_permissions=True, force=True)
			frappe.delete_doc("User", user.name, ignore_permissions=True, force=True)
			frappe.db.commit()


def allow_next_poll(flow_id):
	key = FLOW_KEY_PREFIX + flow_id
	state = frappe.cache.get_value(key, expires=True, use_local_cache=False)
	state["next_allowed_poll_at"] = 0
	frappe.cache.set_value(key, state, expires_in_sec=900)


def expire_flow(flow_id):
	key = FLOW_KEY_PREFIX + flow_id
	state = frappe.cache.get_value(key, expires=True, use_local_cache=False)
	state["expires_at"] = 0
	frappe.cache.set_value(key, state, expires_in_sec=900)


def make_tokens():
	return OAuthTokens(
		"access-secret",
		"refresh-secret",
		make_jwt(
			{
				"email": "user@example.test",
				"https://api.openai.com/auth": {
					"chatgpt_account_id": "account-123",
					"chatgpt_plan_type": "pro",
				},
			}
		),
		3600,
		"openid profile email offline_access",
	)


def make_jwt(payload):
	def encode(value):
		return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

	return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"
