# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import base64
import json
import time
import unittest

from afaa.ai.oauth.openai_codex import (
	AuthorizationGrant,
	CodexOAuthSlowDown,
	DeviceAuthorization,
	OpenAICodexOAuthClient,
	decode_chatgpt_claims,
)


class FakeResponse:
	def __init__(self, status_code: int, data=None):
		self.status_code = status_code
		self.data = data

	@property
	def ok(self):
		return 200 <= self.status_code < 300

	def json(self):
		return self.data


class FakeSession:
	def __init__(self, responses):
		self.responses = list(responses)
		self.requests = []

	def request(self, method, url, **kwargs):
		self.requests.append((method, url, kwargs))
		return self.responses.pop(0)


class TestOpenAICodexOAuthClient(unittest.TestCase):
	def test_device_request_and_pending_poll(self):
		session = FakeSession(
			[
				FakeResponse(
					200,
					{"device_auth_id": "secret-device-id", "user_code": "ABCD-EFGH", "interval": "5"},
				),
				FakeResponse(404),
			]
		)
		client = OpenAICodexOAuthClient(
			client_id="public-client", issuer="https://issuer.test", session=session
		)

		device = client.request_device_authorization()
		self.assertEqual(device.verification_url, "https://issuer.test/codex/device")
		self.assertEqual(device.interval, 5)
		self.assertNotIn("secret-device-id", repr(device))
		self.assertIsNone(client.poll_device_authorization(device))
		self.assertEqual(
			session.requests[0][2]["json"],
			{"client_id": "public-client"},
		)
		self.assertEqual(
			session.requests[1][2]["json"],
			{"device_auth_id": "secret-device-id", "user_code": "ABCD-EFGH"},
		)

	def test_exchange_refresh_and_revoke(self):
		initial_access = make_jwt({"exp": int(time.time()) + 3600})
		id_token = make_jwt(
			{
				"email": "user@example.test",
				"https://api.openai.com/auth": {
					"chatgpt_account_id": "account-123",
					"chatgpt_plan_type": "pro",
				},
			}
		)
		refreshed_access = make_jwt({"exp": int(time.time()) + 7200})
		session = FakeSession(
			[
				FakeResponse(
					200,
					{
						"access_token": initial_access,
						"refresh_token": "refresh-1",
						"id_token": id_token,
					},
				),
				FakeResponse(
					200,
					{
						"access_token": refreshed_access,
						"refresh_token": "refresh-2",
					},
				),
				FakeResponse(200, {}),
			]
		)
		client = OpenAICodexOAuthClient(
			client_id="public-client", issuer="https://issuer.test", session=session
		)

		tokens = client.exchange_authorization_code(
			AuthorizationGrant(authorization_code="secret-code", code_verifier="secret-verifier")
		)
		claims = decode_chatgpt_claims(tokens.id_token)
		self.assertEqual(claims.account_id, "account-123")
		self.assertEqual(claims.plan_type, "pro")
		self.assertNotIn("refresh-1", repr(tokens))

		refreshed = client.refresh(tokens.refresh_token, previous_id_token=tokens.id_token)
		self.assertEqual(refreshed.refresh_token, "refresh-2")
		self.assertEqual(refreshed.id_token, id_token)
		client.revoke(refreshed.refresh_token)

		exchange_request = session.requests[0][2]
		self.assertEqual(exchange_request["data"]["redirect_uri"], "https://issuer.test/deviceauth/callback")
		self.assertEqual(exchange_request["data"]["code_verifier"], "secret-verifier")
		self.assertEqual(
			session.requests[1][2]["json"],
			{
				"client_id": "public-client",
				"grant_type": "refresh_token",
				"refresh_token": "refresh-1",
			},
		)
		self.assertEqual(
			session.requests[2][2]["json"],
			{
				"token": "refresh-2",
				"token_type_hint": "refresh_token",
				"client_id": "public-client",
			},
		)

	def test_refresh_accepts_rotation_response_without_id_token(self):
		access_token = make_jwt({"exp": int(time.time()) + 3600})
		session = FakeSession(
			[
				FakeResponse(
					200,
					{
						"access_token": access_token,
						"refresh_token": "rotated-refresh",
					},
				)
			]
		)
		client = OpenAICodexOAuthClient(session=session)

		tokens = client.refresh("previous-refresh")

		self.assertIsNone(tokens.id_token)
		self.assertEqual(tokens.refresh_token, "rotated-refresh")

	def test_standard_pending_and_slow_down_errors_are_not_generic_failures(self):
		session = FakeSession(
			[
				FakeResponse(400, {"error": "authorization_pending"}),
				FakeResponse(400, {"error": "slow_down", "secret_detail": "must-not-leak"}),
			]
		)
		client = OpenAICodexOAuthClient(session=session)
		device = DeviceAuthorization("https://issuer.test/device", "CODE", "device-secret", 5)

		self.assertIsNone(client.poll_device_authorization(device))
		with self.assertRaises(CodexOAuthSlowDown) as raised:
			client.poll_device_authorization(device)
		self.assertNotIn("must-not-leak", str(raised.exception))

	def test_successful_poll_returns_pkce_grant_without_repr_secrets(self):
		session = FakeSession(
			[
				FakeResponse(
					200,
					{
						"authorization_code": "authorization-secret",
						"code_challenge": "unused-challenge",
						"code_verifier": "verifier-secret",
					},
				)
			]
		)
		client = OpenAICodexOAuthClient(issuer="https://issuer.test", session=session)
		grant = client.poll_device_authorization(
			DeviceAuthorization(
				verification_url="https://issuer.test/codex/device",
				user_code="ABCD-EFGH",
				device_auth_id="device-secret",
				interval=5,
			)
		)

		self.assertIsInstance(grant, AuthorizationGrant)
		self.assertNotIn("authorization-secret", repr(grant))
		self.assertNotIn("verifier-secret", repr(grant))


def make_jwt(payload):
	def encode(value):
		return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

	return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"
