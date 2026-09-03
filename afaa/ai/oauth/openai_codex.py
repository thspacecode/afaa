# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

"""OpenAI Codex OAuth protocol and lazy facade for Frappe-managed credentials.

The protocol client remains independent of Frappe persistence so it can also be used by
the opt-in Phase 0 spike. Phase 2 persistence lives in ``openai_codex_service``.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
	from afaa.ai.oauth.openai_codex_service import CodexTokenResult

CONNECTED_APP_NAME = "AFAA OpenAI Codex"
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEFAULT_ISSUER = "https://auth.openai.com"
DEFAULT_SCOPES = ("openid", "profile", "email", "offline_access")
DEVICE_AUTHORIZATION_TTL = 15 * 60


class CodexOAuthError(Exception):
	"""A sanitized OAuth failure safe to show without a provider response body."""

	def __init__(self, operation: str, status_code: int | None = None):
		self.operation = operation
		self.status_code = status_code
		message = f"OpenAI Codex OAuth {operation} failed"
		if status_code is not None:
			message += f" with HTTP status {status_code}"
		super().__init__(message)


class CodexOAuthSlowDown(CodexOAuthError):
	"""The provider asked the device client to increase its polling interval."""


@dataclass(frozen=True)
class DeviceAuthorization:
	verification_url: str
	user_code: str = dataclass_field(repr=False)
	device_auth_id: str = dataclass_field(repr=False)
	interval: int
	expires_in: int = DEVICE_AUTHORIZATION_TTL


@dataclass(frozen=True)
class AuthorizationGrant:
	authorization_code: str = dataclass_field(repr=False)
	code_verifier: str = dataclass_field(repr=False)


@dataclass(frozen=True)
class OAuthTokens:
	access_token: str = dataclass_field(repr=False)
	refresh_token: str = dataclass_field(repr=False)
	id_token: str | None = dataclass_field(repr=False)
	expires_in: int
	scope: str | None = None


@dataclass(frozen=True)
class ChatGPTClaims:
	account_id: str
	email: str | None
	plan_type: str | None


class OpenAICodexOAuthClient:
	def __init__(
		self,
		*,
		client_id: str = DEFAULT_CLIENT_ID,
		issuer: str = DEFAULT_ISSUER,
		token_endpoint: str | None = None,
		revocation_endpoint: str | None = None,
		session: requests.Session | None = None,
		timeout: float = 30,
	):
		self.client_id = client_id
		self.issuer = issuer.rstrip("/")
		self._token_endpoint = token_endpoint
		self._revocation_endpoint = revocation_endpoint
		self.session = session or requests.Session()
		self.timeout = timeout

	@property
	def authorization_endpoint(self) -> str:
		return f"{self.issuer}/oauth/authorize"

	@property
	def token_endpoint(self) -> str:
		return self._token_endpoint or f"{self.issuer}/oauth/token"

	@property
	def revocation_endpoint(self) -> str:
		return self._revocation_endpoint or f"{self.issuer}/oauth/revoke"

	def request_device_authorization(self) -> DeviceAuthorization:
		data = self.post_json(
			f"{self.issuer}/api/accounts/deviceauth/usercode",
			{"client_id": self.client_id},
			operation="device authorization request",
		)
		try:
			device_auth_id = require_string(data, "device_auth_id")
			user_code = require_string(data, "user_code", alias="usercode")
			interval = max(int(data.get("interval", 5)), 1)
			expires_in = max(int(data.get("expires_in", DEVICE_AUTHORIZATION_TTL)), 1)
		except (TypeError, ValueError) as error:
			raise CodexOAuthError("device authorization response validation") from error
		return DeviceAuthorization(
			verification_url=(
				optional_string(data, "verification_uri")
				or optional_string(data, "verification_url")
				or f"{self.issuer}/codex/device"
			),
			user_code=user_code,
			device_auth_id=device_auth_id,
			interval=interval,
			expires_in=expires_in,
		)

	def poll_device_authorization(self, device: DeviceAuthorization) -> AuthorizationGrant | None:
		response = self.request(
			"POST",
			f"{self.issuer}/api/accounts/deviceauth/token",
			operation="device authorization poll",
			json={"device_auth_id": device.device_auth_id, "user_code": device.user_code},
		)
		if response.status_code in {403, 404}:
			return None
		if not response.ok:
			error_code = get_oauth_error_code(response)
			if error_code == "authorization_pending":
				return None
			if error_code == "slow_down":
				raise CodexOAuthSlowDown("device authorization poll", response.status_code)
			raise CodexOAuthError("device authorization poll", response.status_code)
		try:
			data = response.json()
			if not isinstance(data, dict):
				raise ValueError("invalid poll response")
			return AuthorizationGrant(
				authorization_code=require_string(data, "authorization_code"),
				code_verifier=require_string(data, "code_verifier"),
			)
		except (TypeError, ValueError, requests.JSONDecodeError) as error:
			raise CodexOAuthError("device authorization poll response validation") from error

	def exchange_authorization_code(self, grant: AuthorizationGrant) -> OAuthTokens:
		response = self.request(
			"POST",
			self.token_endpoint,
			operation="authorization code exchange",
			headers={"Content-Type": "application/x-www-form-urlencoded"},
			data={
				"grant_type": "authorization_code",
				"code": grant.authorization_code,
				"redirect_uri": f"{self.issuer}/deviceauth/callback",
				"client_id": self.client_id,
				"code_verifier": grant.code_verifier,
			},
		)
		return self.parse_token_response(
			response,
			operation="authorization code exchange",
			require_id_token=True,
		)

	def refresh(self, refresh_token: str, *, previous_id_token: str = "") -> OAuthTokens:
		response = self.request(
			"POST",
			self.token_endpoint,
			operation="token refresh",
			headers={"Content-Type": "application/json"},
			json={
				"client_id": self.client_id,
				"grant_type": "refresh_token",
				"refresh_token": refresh_token,
			},
		)
		return self.parse_token_response(
			response,
			operation="token refresh",
			previous_refresh_token=refresh_token,
			previous_id_token=previous_id_token,
		)

	def revoke(self, refresh_token: str) -> None:
		response = self.request(
			"POST",
			self.revocation_endpoint,
			operation="token revocation",
			headers={"Content-Type": "application/json"},
			json={
				"token": refresh_token,
				"token_type_hint": "refresh_token",
				"client_id": self.client_id,
			},
		)
		if not response.ok:
			raise CodexOAuthError("token revocation", response.status_code)

	def parse_token_response(
		self,
		response: requests.Response,
		*,
		operation: str,
		previous_refresh_token: str = "",
		previous_id_token: str = "",
		require_id_token: bool = False,
	) -> OAuthTokens:
		if not response.ok:
			raise CodexOAuthError(operation, response.status_code)
		try:
			data = response.json()
			if not isinstance(data, dict):
				raise ValueError("invalid token response")
			access_token = require_string(data, "access_token")
			refresh_token = optional_string(data, "refresh_token") or previous_refresh_token
			id_token = optional_string(data, "id_token") or previous_id_token or None
			token_type = optional_string(data, "token_type")
			if token_type and token_type.lower() != "bearer":
				raise ValueError("invalid token type")
			if not refresh_token or (require_id_token and not id_token):
				raise ValueError("missing required token field")
			expires_in = int(data.get("expires_in") or seconds_until_jwt_expiry(access_token))
			if expires_in <= 0:
				raise ValueError("invalid token expiry")
			return OAuthTokens(
				access_token=access_token,
				refresh_token=refresh_token,
				id_token=id_token,
				expires_in=expires_in,
				scope=optional_string(data, "scope"),
			)
		except (TypeError, ValueError, requests.JSONDecodeError) as error:
			raise CodexOAuthError(f"{operation} response validation") from error

	def post_json(self, url: str, payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
		response = self.request("POST", url, operation=operation, json=payload)
		if not response.ok:
			raise CodexOAuthError(operation, response.status_code)
		try:
			data = response.json()
		except requests.JSONDecodeError as error:
			raise CodexOAuthError(f"{operation} response validation") from error
		if not isinstance(data, dict):
			raise CodexOAuthError(f"{operation} response validation")
		return data

	def request(self, method: str, url: str, *, operation: str, **kwargs) -> requests.Response:
		try:
			return self.session.request(method, url, timeout=self.timeout, **kwargs)
		except requests.RequestException as error:
			raise CodexOAuthError(operation) from error


def decode_chatgpt_claims(id_token: str) -> ChatGPTClaims:
	payload = decode_jwt_payload(id_token)
	auth_claims = payload.get("https://api.openai.com/auth") or {}
	profile_claims = payload.get("https://api.openai.com/profile") or {}
	if not isinstance(auth_claims, dict) or not isinstance(profile_claims, dict):
		raise CodexOAuthError("ID token claims validation")
	account_id = auth_claims.get("chatgpt_account_id")
	if not isinstance(account_id, str) or not account_id.strip():
		raise CodexOAuthError("ID token account claim validation")
	email = payload.get("email") or profile_claims.get("email")
	plan_type = auth_claims.get("chatgpt_plan_type")
	return ChatGPTClaims(
		account_id=account_id.strip(),
		email=email if isinstance(email, str) and email else None,
		plan_type=plan_type if isinstance(plan_type, str) and plan_type else None,
	)


def seconds_until_jwt_expiry(jwt: str) -> int:
	from time import time

	expires_at = decode_jwt_payload(jwt).get("exp")
	if not isinstance(expires_at, int | float):
		raise CodexOAuthError("access token expiry validation")
	return max(int(expires_at - time()), 0)


def decode_jwt_payload(jwt: str) -> dict[str, Any]:
	"""Decode claims without treating them as cryptographically verified identity."""
	try:
		parts = jwt.split(".")
		if len(parts) != 3 or any(not part for part in parts):
			raise ValueError("invalid JWT format")
		payload = parts[1] + "=" * (-len(parts[1]) % 4)
		claims = json.loads(base64.urlsafe_b64decode(payload))
		if not isinstance(claims, dict):
			raise ValueError("invalid JWT payload")
		return claims
	except (ValueError, UnicodeError, binascii.Error, json.JSONDecodeError) as error:
		raise CodexOAuthError("JWT validation") from error


def start_oauth_login(account) -> dict:
	"""Start the Frappe-managed device flow without importing Frappe in this protocol module."""
	from afaa.ai.oauth.openai_codex_service import start_oauth_login as start

	return start(account)


def poll_oauth_login(account, flow_id: str) -> dict:
	from afaa.ai.oauth.openai_codex_service import poll_oauth_login as poll

	return poll(account, flow_id)


def resolve_codex_access_token(
	account, minimum_validity: int = 300, *, force_refresh: bool = False
) -> "CodexTokenResult":
	from afaa.ai.oauth.openai_codex_service import resolve_codex_access_token as resolve

	return resolve(account, minimum_validity, force_refresh=force_refresh)


def report_codex_authentication_failure(account) -> None:
	from afaa.ai.oauth.openai_codex_service import report_codex_authentication_failure as report

	report(account)


def disconnect_oauth(account) -> None:
	from afaa.ai.oauth.openai_codex_service import disconnect_oauth as disconnect

	disconnect(account)


def get_oauth_error_code(response: requests.Response) -> str | None:
	"""Read only a standard OAuth error code; never retain provider response details."""
	try:
		data = response.json()
		if isinstance(data, dict) and isinstance(data.get("error"), str):
			return data["error"]
	except ValueError, requests.JSONDecodeError:
		pass
	return None


def require_string(data: dict[str, Any], key: str, *, alias: str | None = None) -> str:
	value = data.get(key)
	if value is None and alias:
		value = data.get(alias)
	if not isinstance(value, str) or not value.strip():
		raise ValueError(f"missing {key}")
	return value.strip()


def optional_string(data: dict[str, Any], key: str) -> str | None:
	value = data.get(key)
	return value if isinstance(value, str) and value else None
