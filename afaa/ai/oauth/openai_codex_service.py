# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

"""Frappe persistence and request lifecycle for OpenAI Codex device OAuth."""

import datetime
import hashlib
import math
import secrets
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, get_system_timezone, now_datetime
from frappe.utils.synchronization import filelock
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from afaa.ai.oauth.openai_codex import (
	CONNECTED_APP_NAME,
	DEFAULT_ISSUER,
	DEVICE_AUTHORIZATION_TTL,
	AuthorizationGrant,
	CodexOAuthError,
	CodexOAuthSlowDown,
	DeviceAuthorization,
	OAuthTokens,
	OpenAICodexOAuthClient,
	decode_chatgpt_claims,
)

FLOW_KEY_PREFIX = "afaa:openai_codex:oauth_flow:"


class CodexAuthenticationError(frappe.ValidationError):
	"""A user-facing authentication error which never includes provider response data."""


class CodexReconnectRequiredError(CodexAuthenticationError):
	category = "reconnect"


class CodexWorkspaceAuthorizationError(CodexAuthenticationError):
	category = "workspace_authorization"


class CodexUsageLimitError(CodexAuthenticationError):
	category = "usage_limit"


class CodexTemporaryRefreshError(CodexAuthenticationError):
	category = "temporary_refresh_failure"


class CodexTokenResult(BaseModel):
	"""A short-lived access token and its persisted absolute Unix expiry."""

	model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

	access_token: SecretStr = Field(repr=False)
	expires_at: int = Field(gt=0)


def start_oauth_login(account) -> dict:
	_validate_codex_account(account)
	if account.disabled:
		frappe.throw(_("Enable this AI Provider Account before connecting ChatGPT."))

	try:
		device = get_codex_oauth_client().request_device_authorization()
	except CodexOAuthError as error:
		raise CodexAuthenticationError(
			_get_oauth_failure_message(
				error,
				default=_("Unable to start ChatGPT authorization."),
			)
		) from None
	now = time.time()
	ttl = min(max(cint(device.expires_in), 1), DEVICE_AUTHORIZATION_TTL)
	flow_id = secrets.token_urlsafe(32)
	state = {
		"provider_account": account.name,
		"user": frappe.session.user,
		"device_auth_id": device.device_auth_id,
		"user_code": device.user_code,
		"verification_url": device.verification_url,
		"poll_interval": max(cint(device.interval), 1),
		"next_allowed_poll_at": now + max(cint(device.interval), 1),
		"expires_at": now + ttl,
	}
	_set_flow(flow_id, state, ttl)

	return {
		"flow_id": flow_id,
		"verification_url": device.verification_url,
		"user_code": device.user_code,
		"poll_interval": state["poll_interval"],
		"expires_in": ttl,
	}


def poll_oauth_login(account, flow_id: str) -> dict:
	_validate_codex_account(account)
	flow_id = _validate_flow_id(flow_id)

	lock_name = "afaa_codex_oauth_flow_" + hashlib.sha256(flow_id.encode()).hexdigest()
	with filelock(lock_name):
		state = _get_bound_flow(account, flow_id)
		if state is None:
			return {
				"status": "expired",
				"message": _("Authorization expired; restart connection."),
			}

		now = time.time()
		remaining = math.ceil(state["expires_at"] - now)
		if remaining <= 0:
			_delete_flow(flow_id)
			return {
				"status": "expired",
				"message": _("Authorization expired; restart connection."),
			}

		if now < state["next_allowed_poll_at"]:
			return {
				"status": "pending",
				"poll_interval": state["poll_interval"],
				"retry_after": max(math.ceil(state["next_allowed_poll_at"] - now), 1),
				"expires_in": remaining,
			}

		# Reserve this poll before making the provider request so concurrent browser
		# requests can never produce more than one OpenAI poll per interval.
		state["next_allowed_poll_at"] = now + state["poll_interval"]
		_set_flow(flow_id, state, remaining)
		device = DeviceAuthorization(
			verification_url=state["verification_url"],
			user_code=state["user_code"],
			device_auth_id=state["device_auth_id"],
			interval=state["poll_interval"],
			expires_in=remaining,
		)

		try:
			grant = get_codex_oauth_client().poll_device_authorization(device)
		except CodexOAuthSlowDown:
			state["poll_interval"] += 5
			state["next_allowed_poll_at"] = time.time() + state["poll_interval"]
			_set_flow(flow_id, state, max(math.ceil(state["expires_at"] - time.time()), 1))
			return {
				"status": "pending",
				"poll_interval": state["poll_interval"],
				"retry_after": state["poll_interval"],
				"expires_in": max(math.ceil(state["expires_at"] - time.time()), 1),
			}
		except CodexOAuthError as error:
			_delete_flow(flow_id)
			_set_account_status(account, "Error")
			return {"status": "failed", "message": _get_oauth_failure_message(error)}

		if grant is None:
			return {
				"status": "pending",
				"poll_interval": state["poll_interval"],
				"retry_after": state["poll_interval"],
				"expires_in": max(math.ceil(state["expires_at"] - time.time()), 1),
			}

		return _complete_oauth_login(account, flow_id, state, grant)


def resolve_codex_access_token(
	account,
	minimum_validity: int = 300,
	*,
	force_refresh: bool = False,
) -> CodexTokenResult:
	"""Resolve a cached token or perform one serialized refresh.

	Every caller enters the same lock. A forced waiter reuses a token rotated while it
	was waiting, but otherwise refreshes even when the cached expiry is still valid.
	"""
	_validate_codex_account(account)
	if type(force_refresh) is not bool:
		raise CodexTemporaryRefreshError(_("Unable to refresh ChatGPT authorization; try again."))
	minimum_validity = max(cint(minimum_validity), 300)
	if account.disabled or account.oauth_status != "Connected" or not account.connected_user:
		raise CodexReconnectRequiredError(_("ChatGPT authorization expired; reconnect account."))

	token_cache = _get_token_cache(account.connected_user)
	if not token_cache:
		_require_reconnection(account)
	initial_modified = str(token_cache.modified)

	lock_name = "afaa_codex_token_" + hashlib.sha256(token_cache.name.encode()).hexdigest()
	with filelock(lock_name):
		account.reload()
		_validate_codex_account(account)
		if account.disabled or account.oauth_status != "Connected" or not account.connected_user:
			raise CodexReconnectRequiredError(_("ChatGPT authorization expired; reconnect account."))

		token_cache = _get_token_cache(account.connected_user)
		if not token_cache:
			_require_reconnection(account)
		token_cache.reload()
		access_token = token_cache.get_password("access_token", raise_exception=False)
		if not access_token:
			_require_reconnection(account)

		rotated_while_waiting = str(token_cache.modified) != initial_modified
		if token_cache.get_expires_in() > minimum_validity and (not force_refresh or rotated_while_waiting):
			return _token_result(token_cache, access_token)

		refresh_token = token_cache.get_password("refresh_token", raise_exception=False)
		if not refresh_token:
			_require_reconnection(account)

		try:
			tokens = get_codex_oauth_client().refresh(refresh_token)
			_validate_refreshed_identity(account, tokens)
		except CodexOAuthError as error:
			if _is_permanent_refresh_failure(error):
				if error.status_code == 403:
					_require_reconnection(
						account,
						_("Account or workspace is not authorized for Codex."),
						error_type=CodexWorkspaceAuthorizationError,
					)
				_require_reconnection(account)
			if error.status_code == 429:
				raise CodexUsageLimitError(_("ChatGPT/Codex subscription usage limit reached.")) from None
			raise CodexTemporaryRefreshError(
				_("Unable to refresh ChatGPT authorization; try again.")
			) from None

		_persist_token_cache(token_cache, tokens)
		# Make the rotation visible before releasing the cross-process lock. Otherwise
		# a waiter can acquire the lock before Frappe's request-end commit and refresh
		# the same old refresh token a second time.
		frappe.db.commit()
		return _token_result(token_cache, tokens.access_token)


def disconnect_oauth(account) -> None:
	_validate_codex_account(account)
	token_cache = _get_token_cache(account.connected_user) if account.connected_user else None
	try:
		if token_cache:
			refresh_token = token_cache.get_password("refresh_token", raise_exception=False)
			if refresh_token:
				try:
					get_codex_oauth_client().revoke(refresh_token)
				except Exception as error:
					_log_sanitized_revocation_error(error)
	finally:
		try:
			if token_cache:
				token_cache.delete(ignore_permissions=True, force=True)
		finally:
			account.db_set(
				{
					"connected_user": None,
					"oauth_status": "Not Connected",
					"external_account_id": None,
					"external_account_email": None,
					"subscription_plan": None,
					"oauth_connected_on": None,
				},
				notify=True,
			)
			_notify_provider_account_invalidated(account.name)


def report_codex_authentication_failure(account) -> None:
	"""Fail closed after a freshly forced token is rejected by the provider."""
	_validate_codex_account(account)
	_set_account_status(account, "Expired", commit=True)
	_notify_provider_account_invalidated(account.name)


def get_codex_oauth_client() -> OpenAICodexOAuthClient:
	if not frappe.db.exists("Connected App", CONNECTED_APP_NAME):
		frappe.throw(_("Connected App {0} is not configured.").format(frappe.bold(CONNECTED_APP_NAME)))
	connected_app = frappe.get_doc("Connected App", CONNECTED_APP_NAME)
	if not connected_app.client_id:
		frappe.throw(_("Connected App {0} has no client ID.").format(frappe.bold(CONNECTED_APP_NAME)))

	issuer = frappe.conf.get("afaa_openai_codex_issuer") or _get_endpoint_origin(
		connected_app.authorization_uri
	)
	return OpenAICodexOAuthClient(
		client_id=connected_app.client_id,
		issuer=issuer,
		token_endpoint=connected_app.token_uri,
		revocation_endpoint=connected_app.revocation_uri,
	)


def _complete_oauth_login(account, flow_id: str, state: dict, grant: AuthorizationGrant) -> dict:
	try:
		tokens = get_codex_oauth_client().exchange_authorization_code(grant)
		if not tokens.id_token:
			raise CodexOAuthError("ID token validation")
		claims = decode_chatgpt_claims(tokens.id_token)
		token_cache = _get_token_cache(state["user"])
		if token_cache is None:
			token_cache = frappe.get_doc(
				{
					"doctype": "Token Cache",
					"connected_app": CONNECTED_APP_NAME,
					"user": state["user"],
				}
			)
		_persist_token_cache(token_cache, tokens)
		account.db_set(
			{
				"connected_app": CONNECTED_APP_NAME,
				"connected_user": state["user"],
				"oauth_status": "Connected",
				"external_account_id": claims.account_id,
				"external_account_email": claims.email,
				"subscription_plan": claims.plan_type,
				"oauth_connected_on": now_datetime(),
			},
			notify=True,
		)
	except CodexOAuthError as error:
		_delete_flow(flow_id)
		_set_account_status(account, "Error")
		return {"status": "failed", "message": _get_oauth_completion_failure_message(error)}

	_delete_flow(flow_id)
	return {"status": "complete"}


def _token_result(token_cache, access_token: str) -> CodexTokenResult:
	modified = get_datetime(token_cache.modified)
	if modified.tzinfo is None:
		modified = modified.replace(tzinfo=ZoneInfo(get_system_timezone()))
	expires_at = int(modified.astimezone(datetime.UTC).timestamp() + max(cint(token_cache.expires_in), 0))
	return CodexTokenResult(access_token=SecretStr(access_token), expires_at=expires_at)


def _persist_token_cache(token_cache, tokens: OAuthTokens) -> None:
	token_cache.token_type = "Bearer"
	token_cache.access_token = tokens.access_token
	token_cache.refresh_token = tokens.refresh_token
	token_cache.expires_in = tokens.expires_in
	if tokens.scope:
		token_cache.set("scopes", [])
		for scope in tokens.scope.split():
			token_cache.append("scopes", {"scope": scope})
	if token_cache.is_new():
		token_cache.insert(ignore_permissions=True)
	else:
		token_cache.save(ignore_permissions=True)


def _validate_refreshed_identity(account, tokens: OAuthTokens) -> None:
	if not tokens.id_token:
		return
	claims = decode_chatgpt_claims(tokens.id_token)
	if claims.account_id != account.external_account_id:
		raise CodexOAuthError("refreshed account claim validation")


def _get_token_cache(user: str | None):
	if not user:
		return None
	connected_app = frappe.get_doc("Connected App", CONNECTED_APP_NAME)
	return connected_app.get_token_cache(user)


def _validate_codex_account(account) -> None:
	provider = frappe.get_doc("AI Provider", account.provider)
	if provider.provider_type != "openai_codex":
		frappe.throw(_("This AI Provider Account does not use OpenAI Codex OAuth."))
	if account.connected_app != CONNECTED_APP_NAME:
		frappe.throw(
			_("OpenAI Codex accounts must use Connected App {0}.").format(frappe.bold(CONNECTED_APP_NAME))
		)


def _validate_flow_id(flow_id: str) -> str:
	if not isinstance(flow_id, str) or not 20 <= len(flow_id) <= 128:
		frappe.throw(_("Invalid ChatGPT authorization flow."))
	if any(
		character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
		for character in flow_id
	):
		frappe.throw(_("Invalid ChatGPT authorization flow."))
	return flow_id


def _get_bound_flow(account, flow_id: str) -> dict | None:
	state = frappe.cache.get_value(_flow_key(flow_id), expires=True, use_local_cache=False)
	if state is None:
		return None
	if not isinstance(state, dict):
		_delete_flow(flow_id)
		return None
	if state.get("provider_account") != account.name or state.get("user") != frappe.session.user:
		raise frappe.PermissionError(_("This ChatGPT authorization flow belongs to another user or account."))
	required_fields = {
		"device_auth_id",
		"user_code",
		"verification_url",
		"poll_interval",
		"next_allowed_poll_at",
		"expires_at",
	}
	if not required_fields.issubset(state):
		_delete_flow(flow_id)
		return None
	return state


def _set_flow(flow_id: str, state: dict, ttl: int) -> None:
	key = _flow_key(flow_id)
	frappe.cache.set_value(key, state, expires_in_sec=max(cint(ttl), 1))
	stored_state = frappe.cache.get_value(key, expires=True, use_local_cache=False)
	if stored_state != state:
		frappe.cache.delete_value(key)
		frappe.throw(_("Unable to store the ChatGPT authorization flow; try again."))


def _delete_flow(flow_id: str) -> None:
	frappe.cache.delete_value(_flow_key(flow_id))


def _flow_key(flow_id: str) -> str:
	return FLOW_KEY_PREFIX + flow_id


def _set_account_status(account, status: str, *, commit: bool = False) -> None:
	account.db_set("oauth_status", status, notify=True, commit=commit)


def _require_reconnection(
	account,
	message: str | None = None,
	*,
	error_type: type[CodexAuthenticationError] = CodexReconnectRequiredError,
):
	_set_account_status(account, "Expired", commit=True)
	_notify_provider_account_invalidated(account.name)
	raise error_type(message or _("ChatGPT authorization expired; reconnect account.")) from None


def _notify_provider_account_invalidated(provider_account: str) -> None:
	from afaa.ai.external_runtime import notify_provider_account_invalidated

	notify_provider_account_invalidated(provider_account)


def _get_oauth_completion_failure_message(error: CodexOAuthError) -> str:
	if "account claim" in error.operation:
		return _("Unable to determine ChatGPT account.")
	return _get_oauth_failure_message(error, default=_("Unable to validate the ChatGPT account."))


def _get_oauth_failure_message(
	error: CodexOAuthError,
	*,
	default: str | None = None,
) -> str:
	if error.status_code == 401:
		return _("ChatGPT authorization expired; reconnect account.")
	if error.status_code == 403:
		return _("Account or workspace is not authorized for Codex.")
	if error.status_code == 429:
		return _("ChatGPT/Codex subscription usage limit reached.")
	return default or _("Unable to complete ChatGPT authorization.")


def _is_permanent_refresh_failure(error: CodexOAuthError) -> bool:
	return error.status_code in {400, 401, 403} or "validation" in error.operation


def _log_sanitized_revocation_error(error: Exception) -> None:
	status_code = error.status_code if isinstance(error, CodexOAuthError) else None
	message = "OpenAI Codex refresh-token revocation failed"
	if status_code is not None:
		message += f" with HTTP status {status_code}"
	else:
		message += f" ({type(error).__name__})"
	frappe.log_error(title=_("OpenAI Codex disconnect warning"), message=message)


def _get_endpoint_origin(endpoint: str | None) -> str:
	parts = urlsplit(endpoint or "")
	if parts.scheme == "https" and parts.netloc:
		return f"{parts.scheme}://{parts.netloc}"
	return DEFAULT_ISSUER
