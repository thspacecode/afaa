# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

"""MCP client support: policy helpers and the AI Manager discovery action.

The administrator-approved master data (``AI MCP Server`` and its accounts)
lives in ``afaa_setup``; this module owns everything shared between doctype
validation, runtime resolution, and the bench-side discovery client:

- URL and tool-name validation for remote HTTPS MCP servers,
- the deployment host allowlist plus public-address egress policy,
- effective runtime key derivation (``<server_key>`` / ``<server_key>-<account_key>``),
- account resolution for one (server, account) pair, and
- the sanitized ``test_connection`` discovery action.

Everything here fails closed: an unset allowlist denies every host, and a
hostname that resolves to any non-global address (loopback, private, link
local, multicast, unspecified, or cloud metadata) is rejected even when the
allowlist is ``*``. DNS-level checks are best effort; production deployments
should additionally enforce egress with a firewall or proxy to mitigate DNS
rebinding.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from datetime import timedelta
from typing import Any

import frappe
from frappe import _

MCP_ALLOWED_HOSTS_CONFIG = "afaa_mcp_allowed_hosts"
MCP_ALLOWED_HOSTS_FALLBACK_CONFIG = "porch_mcp_allowed_hosts"
MCP_URL_MAX_LENGTH = 1000
MCP_HOSTNAME_MAX_LENGTH = 253
MCP_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
MCP_EFFECTIVE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,99}$")

MAX_ALLOWED_TOOLS_PER_SERVER = 100
MAX_MCP_SERVERS_PER_AGENT = 10

MIN_CONNECT_TIMEOUT = 1
MAX_CONNECT_TIMEOUT = 120
MIN_READ_TIMEOUT = 1
MAX_READ_TIMEOUT = 600

DISCOVERY_MAX_TOOLS = 200
DISCOVERY_MAX_DESCRIPTION_CHARS = 1000
DISCOVERY_MAX_ERROR_CHARS = 300

AUTH_TYPE_NONE = "none"
AUTH_TYPE_BEARER = "bearer_token"


def validate_mcp_url(value: str | None, label: str | None = None) -> str:
	"""Validate one remote MCP server URL and return its normalized form.

	Only plain HTTPS URLs are accepted: no userinfo, query string, or
	fragment, no whitespace or control characters, and a bounded port.
	"""
	label = label or _("URL")
	url = (value or "").strip()
	if not url:
		frappe.throw(_("{0} is required.").format(label), frappe.ValidationError)
	if len(url) > MCP_URL_MAX_LENGTH:
		frappe.throw(_("{0} is too long.").format(label), frappe.ValidationError)
	if any(ord(character) < 32 or ord(character) == 127 for character in url):
		frappe.throw(_("{0} contains control characters.").format(label), frappe.ValidationError)
	if len(url.split()) != 1:
		frappe.throw(_("{0} must not contain whitespace.").format(label), frappe.ValidationError)

	from urllib.parse import urlsplit

	parts = urlsplit(url)
	if parts.scheme != "https":
		frappe.throw(_("{0} must use HTTPS.").format(label), frappe.ValidationError)
	hostname = (parts.hostname or "").lower()
	if not hostname or len(hostname) > MCP_HOSTNAME_MAX_LENGTH:
		frappe.throw(_("{0} must include a valid hostname.").format(label), frappe.ValidationError)
	if parts.username or parts.password:
		frappe.throw(_("{0} must not embed credentials.").format(label), frappe.ValidationError)
	if parts.query or parts.fragment:
		frappe.throw(
			_("{0} must not include a query string or fragment.").format(label), frappe.ValidationError
		)
	try:
		port = parts.port
	except ValueError:
		frappe.throw(_("{0} has an invalid port.").format(label), frappe.ValidationError)
	if port is not None and not 1 <= port <= 65535:
		frappe.throw(_("{0} has an invalid port.").format(label), frappe.ValidationError)
	return url


def get_allowed_mcp_hosts() -> list[str]:
	"""Read the deployment MCP host allowlist, failing closed when unset.

	Supports a comma/space/newline separated list. The explicit value ``*``
	permits any *public* HTTPS hostname; private, loopback, link-local,
	multicast, unspecified, and metadata destinations remain denied.
	"""
	raw = frappe.conf.get(MCP_ALLOWED_HOSTS_CONFIG)
	if raw in (None, ""):
		raw = frappe.conf.get(MCP_ALLOWED_HOSTS_FALLBACK_CONFIG)
	if not isinstance(raw, str):
		return []
	hosts: list[str] = []
	for item in re.split(r"[\s,]+", raw.strip()):
		item = item.strip().strip(".").lower()
		if item and item not in hosts:
			hosts.append(item)
	return hosts


def ensure_mcp_host_allowed(url: str) -> str:
	"""Enforce the host allowlist and public-address egress policy for one URL.

	Returns the lowercased hostname. Raises ``frappe.PermissionError`` when the
	deployment allowlist is unset or does not cover the host, and
	``frappe.ValidationError`` when the host resolves to a non-global address
	or does not resolve at all.
	"""
	url = validate_mcp_url(url)
	from urllib.parse import urlsplit

	hostname = (urlsplit(url).hostname or "").lower()
	allowed = get_allowed_mcp_hosts()
	if not allowed:
		frappe.throw(
			_("MCP egress is not configured. Set the site config {0} (or {1}) to allow MCP hosts.").format(
				MCP_ALLOWED_HOSTS_CONFIG, MCP_ALLOWED_HOSTS_FALLBACK_CONFIG
			),
			frappe.PermissionError,
		)
	if "*" not in allowed and hostname not in allowed:
		frappe.throw(
			_("MCP host {0} is not allowed by the deployment allowlist.").format(frappe.bold(hostname)),
			frappe.PermissionError,
		)

	port = urlsplit(url).port or 443
	try:
		infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
	except OSError:
		frappe.throw(
			_("MCP host {0} does not resolve.").format(frappe.bold(hostname)),
			frappe.ValidationError,
		)
	for info in infos:
		address = info[4][0]
		try:
			parsed = ipaddress.ip_address(address.split("%", 1)[0])
		except ValueError:
			frappe.throw(
				_("MCP host {0} resolves to an invalid address.").format(frappe.bold(hostname)),
				frappe.ValidationError,
			)
		if not parsed.is_global:
			frappe.throw(
				_("MCP host {0} resolves to a private or otherwise non-public address and is denied.").format(
					frappe.bold(hostname)
				),
				frappe.ValidationError,
			)
	return hostname


def effective_mcp_key(server_key: str, account_key: str | None, is_default: bool) -> str:
	"""Derive the model-visible runtime key for one (server, account) pair.

	The server's default account keeps the plain ``<server_key>`` namespace;
	any other account extends it to ``<server_key>-<account_key>``.
	"""
	if is_default or not account_key:
		return server_key
	return f"{server_key}-{account_key}"


def validate_effective_mcp_key(effective_key: str, label: str) -> None:
	if not MCP_EFFECTIVE_KEY_PATTERN.fullmatch(effective_key or ""):
		frappe.throw(
			_("{0} {1} is invalid.").format(label, frappe.bold(effective_key or "")),
			frappe.ValidationError,
		)


def resolve_mcp_account(mcp_server: str, mcp_server_account: str | None, *, require_enabled: bool = True):
	"""Resolve the credential account for one MCP server attachment.

	An explicit account must belong to the server. An omitted account resolves
	the server's single enabled default account.
	"""
	if mcp_server_account:
		account = frappe.get_doc("AI MCP Server Account", mcp_server_account)
		if account.mcp_server != mcp_server:
			frappe.throw(
				_("AI MCP Server Account {0} does not belong to MCP server {1}.").format(
					frappe.bold(account.name), frappe.bold(mcp_server)
				),
				frappe.ValidationError,
			)
		if require_enabled and account.disabled:
			frappe.throw(
				_("AI MCP Server Account {0} is disabled.").format(frappe.bold(account.name)),
				frappe.ValidationError,
			)
		return account

	accounts = frappe.get_all(
		"AI MCP Server Account",
		filters={"mcp_server": mcp_server, "is_default": 1},
		pluck="name",
	)
	if not accounts:
		frappe.throw(
			_("AI MCP Server {0} has no default account.").format(frappe.bold(mcp_server)),
			frappe.ValidationError,
		)
	if len(accounts) > 1:
		frappe.throw(
			_("AI MCP Server {0} has multiple default accounts.").format(frappe.bold(mcp_server)),
			frappe.ValidationError,
		)
	account = frappe.get_doc("AI MCP Server Account", accounts[0])
	if require_enabled and account.disabled:
		frappe.throw(
			_("AI MCP Server {0} has no enabled default account.").format(frappe.bold(mcp_server)),
			frappe.ValidationError,
		)
	return account


def mcp_account_display_name(server, account) -> str:
	"""Model-visible connection name; non-default accounts carry their label."""
	if account.is_default:
		return server.server_name
	return f"{server.server_name} ({account.account_name})"


def _sanitize_error(error: BaseException) -> str:
	message = " ".join(str(error).split())
	if len(message) > DISCOVERY_MAX_ERROR_CHARS:
		message = message[: DISCOVERY_MAX_ERROR_CHARS - 1] + "…"
	return message or type(error).__name__


def _discovery_headers(account) -> dict[str, str]:
	if (account.auth_type or AUTH_TYPE_NONE) != AUTH_TYPE_BEARER:
		return {}
	token = account.get_password("bearer_token", raise_exception=False)
	if not token:
		frappe.throw(
			_("AI MCP Server Account {0} has no bearer token.").format(frappe.bold(account.name)),
			frappe.ValidationError,
		)
	return {"Authorization": f"Bearer {token}"}


def _egress_httpx_client_factory() -> Any:
	"""Build the MCP discovery HTTP client with redirects denied and no proxies."""
	import httpx

	def factory(**kwargs: Any) -> httpx.AsyncClient:
		kwargs["follow_redirects"] = False
		kwargs["trust_env"] = False
		client = httpx.AsyncClient(**kwargs)

		async def deny_redirects(response: httpx.Response) -> None:
			if response.has_redirect_location:
				raise frappe.ValidationError("MCP redirects are not allowed")

		client.event_hooks["response"] = [*client.event_hooks.get("response", []), deny_redirects]
		return client

	return factory


async def _discover_tools(
	url: str,
	headers: dict[str, str],
	transport: str,
	connect_timeout: int,
	read_timeout: int,
) -> dict[str, Any]:
	"""Connect once with the bench-side MCP client and return sanitized metadata."""
	from mcp import ClientSession
	from mcp.client.sse import sse_client
	from mcp.client.streamable_http import streamablehttp_client

	factory = _egress_httpx_client_factory()

	async def attempt(kind: str):
		if kind == "sse":
			transport_streams = sse_client(
				url,
				headers=headers,
				timeout=timedelta(seconds=connect_timeout),
				sse_read_timeout=timedelta(seconds=read_timeout),
				httpx_client_factory=factory,
			)
		else:
			transport_streams = streamablehttp_client(
				url,
				headers=headers,
				timeout=timedelta(seconds=connect_timeout),
				sse_read_timeout=timedelta(seconds=read_timeout),
				httpx_client_factory=factory,
			)
		async with transport_streams as streams:
			read_stream, write_stream, *_ = streams
			async with ClientSession(read_stream, write_stream) as session:
				init = await asyncio.wait_for(session.initialize(), timeout=connect_timeout)
				tools = await asyncio.wait_for(session.list_tools(), timeout=read_timeout)
				return kind, init, tools

	orders: list[str]
	if transport == "sse":
		orders = ["sse"]
	elif transport == "streamable_http":
		orders = ["streamable_http"]
	else:
		orders = ["streamable_http", "sse"]

	used = None
	init = None
	tools = None
	last_error: BaseException | None = None
	for kind in orders:
		try:
			used, init, tools = await attempt(kind)
			break
		except BaseException as error:  # fallback, then sanitized failure
			last_error = error
	if used is None or init is None or tools is None:
		raise frappe.ValidationError(_("MCP connection test failed: {0}").format(_sanitize_error(last_error)))

	discovered = []
	for tool in tools.tools or []:
		name = (getattr(tool, "name", None) or "").strip()
		if not MCP_TOOL_NAME_PATTERN.fullmatch(name):
			continue
		description = " ".join((getattr(tool, "description", None) or "").split())
		if len(description) > DISCOVERY_MAX_DESCRIPTION_CHARS:
			description = description[: DISCOVERY_MAX_DESCRIPTION_CHARS - 1] + "…"
		discovered.append({"name": name, "description": description})
	discovered.sort(key=lambda item: item["name"])
	server_name = (getattr(init, "serverInfo", None) and init.serverInfo.name) or ""
	server_version = str((getattr(init, "serverInfo", None) and init.serverInfo.version) or "")
	return {
		"transport": used,
		"serverName": server_name[:140],
		"serverVersion": server_version[:140],
		"toolCount": len(discovered),
		"tools": discovered[:DISCOVERY_MAX_TOOLS],
	}


@frappe.whitelist(methods=["POST"])
def test_connection(mcp_server: str, mcp_server_account: str | None = None) -> dict[str, Any]:
	"""Connect to one MCP server with a selected account and return sanitized metadata.

	Discovery is read-only: it lists the server identity and tool names so an
	AI Manager can build the exact allowlist. Nothing is persisted and no tool
	is granted. The same host/egress policy as the runtime path applies.
	"""
	frappe.only_for(["AI Manager", "System Manager"])
	if not isinstance(mcp_server, str) or not mcp_server:
		frappe.throw(_("MCP Server is required."), frappe.ValidationError)
	if mcp_server_account is not None and not isinstance(mcp_server_account, str):
		frappe.throw(_("MCP Server Account is invalid."), frappe.ValidationError)

	server = frappe.get_doc("AI MCP Server", mcp_server)
	if server.disabled:
		frappe.throw(
			_("AI MCP Server {0} is disabled.").format(frappe.bold(server.name)),
			frappe.ValidationError,
		)
	account = resolve_mcp_account(server.name, mcp_server_account or None)
	url = validate_mcp_url(server.url, _("URL"))
	ensure_mcp_host_allowed(url)
	headers = _discovery_headers(account)

	result = asyncio.run(
		_discover_tools(
			url,
			headers,
			server.transport or "auto",
			_bounded_timeout(server.connect_timeout, default=10),
			_bounded_timeout(server.read_timeout, default=60),
		)
	)
	return {
		"ok": True,
		"mcpServer": server.name,
		"serverKey": server.server_key,
		"account": account.name,
		"effectiveKey": effective_mcp_key(server.server_key, account.account_key, account.is_default),
		**result,
	}


def _bounded_timeout(value: Any, *, default: int) -> int:
	try:
		parsed = int(value)
	except TypeError, ValueError:
		return default
	if parsed <= 0:
		return default
	return parsed
