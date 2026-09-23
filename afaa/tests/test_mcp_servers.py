# Copyright (c) 2026, SpaceCode and Contributors
# See license.txt

import socket
from unittest.mock import patch

import frappe
from pydantic import SecretStr

from afaa.ai.external_runtime import (
	MCPAwareCodexExternalRuntimeDescriptor,
	MCPAwareExternalRuntimeConfig,
	resolve_external_runtime,
)
from afaa.ai.mcp import (
	AUTH_TYPE_BEARER,
	effective_mcp_key,
	ensure_mcp_host_allowed,
	get_allowed_mcp_hosts,
	resolve_mcp_account,
	test_connection,
	validate_mcp_url,
)
from afaa.ai.runtime import resolve_ai_agent
from afaa.tests.data.factories import AIAgentFactory
from afaa.tests.utils import AFAATestSuite, boot_strap_test_master_data


def _unique(prefix: str) -> str:
	return f"{prefix}-{frappe.generate_hash(length=8).lower()}"


class MCPTestMixin:
	def make_server(self, server_key: str | None = None, **kwargs):
		doc = frappe.get_doc(
			{
				"doctype": "AI MCP Server",
				"server_key": server_key or _unique("server"),
				"server_name": kwargs.pop("server_name", "Test Server"),
				"url": kwargs.pop("url", "https://mcp.example.test/mcp"),
				"transport": kwargs.pop("transport", "auto"),
				"connect_timeout": kwargs.pop("connect_timeout", 10),
				"read_timeout": kwargs.pop("read_timeout", 60),
				"allowed_tools": kwargs.pop("allowed_tools", [{"tool_name": "get_file", "enabled": 1}]),
			}
		)
		doc.insert(ignore_permissions=True)
		return doc

	def make_account(self, server, account_key: str | None = None, **kwargs):
		doc = frappe.get_doc(
			{
				"doctype": "AI MCP Server Account",
				"mcp_server": server.name,
				"account_key": account_key or _unique("account"),
				"account_name": kwargs.pop("account_name", "Test Account"),
				"auth_type": kwargs.pop("auth_type", AUTH_TYPE_BEARER),
				"bearer_token": kwargs.pop("bearer_token", "secret-token"),
				"is_default": kwargs.pop("is_default", 0),
			}
		)
		doc.insert(ignore_permissions=True)
		return doc

	def make_agent(self, agent_key: str | None = None, mcp_rows: list | None = None, **kwargs):
		return AIAgentFactory.create(
			agent_name=kwargs.pop("agent_name", "MCP Agent"),
			agent_key=agent_key or _unique("agent"),
			model=boot_strap_test_master_data.model,
			provider_account=boot_strap_test_master_data.provider_account,
			system_prompt="Use MCP tools carefully.",
			mcp_servers=mcp_rows or [],
			**kwargs,
		)


class TestAIMCPServerDocType(MCPTestMixin, AFAATestSuite):
	def test_valid_server_and_key_is_immutable(self):
		server = self.make_server()
		server.description = "Updated."
		server.save(ignore_permissions=True)

		server.server_key = "changed-key"
		self.assertRaises(frappe.PermissionError, server.save)

	def test_url_must_be_plain_https(self):
		for url in (
			"http://mcp.example.test/mcp",
			"https://user:pass@mcp.example.test/mcp",
			"https://mcp.example.test/mcp?token=1",
			"https://mcp.example.test/mcp#frag",
			"ftp://mcp.example.test",
			"https://mcp.example.test/mcp other",
			"",
		):
			doc = frappe.new_doc("AI MCP Server")
			doc.update({"server_key": _unique("server"), "server_name": "Bad URL", "url": url})
			self.assertRaises(frappe.ValidationError, doc.insert)

	def test_timeouts_are_bounded(self):
		doc = frappe.new_doc("AI MCP Server")
		doc.update(
			{
				"server_key": _unique("server"),
				"server_name": "Timeouts",
				"url": "https://mcp.example.test/mcp",
				"connect_timeout": 999,
				"read_timeout": 0,
			}
		)
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_tool_allowlist_rejects_duplicates_and_bad_names(self):
		for tools in (
			[{"tool_name": "get_file", "enabled": 1}, {"tool_name": "get_file", "enabled": 1}],
			[{"tool_name": "bad name", "enabled": 1}],
			[{"tool_name": "", "enabled": 1}],
		):
			doc = frappe.new_doc("AI MCP Server")
			doc.update(
				{
					"server_key": _unique("server"),
					"server_name": "Tools",
					"url": "https://mcp.example.test/mcp",
					"allowed_tools": tools,
				}
			)
			self.assertRaises(frappe.ValidationError, doc.insert)

	def test_trash_is_blocked_while_referenced(self):
		server = self.make_server()
		self.make_account(server, is_default=1)
		self.make_agent(mcp_rows=[{"mcp_server": server.name}])

		self.assertRaises(frappe.ValidationError, server.delete)
		frappe.db.delete("AI Agent MCP Server", {"mcp_server": server.name})
		self.assertRaises(frappe.ValidationError, server.delete)  # accounts remain
		frappe.db.delete("AI MCP Server Account", {"mcp_server": server.name})
		server.delete(ignore_permissions=True)


class TestAIMCPServerAccountDocType(MCPTestMixin, AFAATestSuite):
	def test_account_key_is_unique_per_server_and_immutable(self):
		server = self.make_server()
		self.make_account(server, account_key="primary", is_default=1)

		duplicate = frappe.get_doc(
			{
				"doctype": "AI MCP Server Account",
				"mcp_server": server.name,
				"account_key": "primary",
				"account_name": "Duplicate",
			}
		)
		self.assertRaises(frappe.ValidationError, duplicate.insert)

		account = frappe.get_doc("AI MCP Server Account", f"{server.name}-primary")
		account.account_key = "renamed"
		self.assertRaises(frappe.PermissionError, account.save)

	def test_exactly_one_default_per_server(self):
		server = self.make_server()
		self.make_account(server, account_key="primary", is_default=1)
		second = self.make_account(server, account_key="secondary", is_default=0)

		second.is_default = 1
		self.assertRaises(frappe.ValidationError, second.save)

	def test_removing_the_only_default_is_rejected(self):
		server = self.make_server()
		first = self.make_account(server, account_key="primary", is_default=1)
		self.make_account(server, account_key="secondary", is_default=0)

		first.is_default = 0
		self.assertRaises(frappe.ValidationError, first.save)

	def test_bearer_accounts_require_a_token(self):
		server = self.make_server()
		doc = frappe.get_doc(
			{
				"doctype": "AI MCP Server Account",
				"mcp_server": server.name,
				"account_key": "no-token",
				"account_name": "No Token",
				"auth_type": AUTH_TYPE_BEARER,
				"is_default": 1,
			}
		)
		self.assertRaises(frappe.ValidationError, doc.insert)

	def test_unauthenticated_account_needs_no_token(self):
		server = self.make_server()
		account = self.make_account(server, auth_type="none", bearer_token=None, is_default=1)
		self.assertEqual(account.auth_type, "none")


class TestAIAgentMCPAttachment(MCPTestMixin, AFAATestSuite):
	def test_attachment_requires_a_resolvable_account(self):
		server = self.make_server()
		agent = self.make_agent()
		agent.append("mcp_servers", {"mcp_server": server.name})
		self.assertRaises(frappe.ValidationError, agent.save)

	def test_account_must_belong_to_the_linked_server(self):
		server = self.make_server()
		other = self.make_server()
		other_account = self.make_account(other, is_default=1)

		agent = self.make_agent()
		agent.append("mcp_servers", {"mcp_server": server.name, "mcp_server_account": other_account.name})
		self.assertRaises(frappe.ValidationError, agent.save)

	def test_duplicate_pairs_are_rejected(self):
		server = self.make_server()
		account = self.make_account(server, is_default=1)

		agent = self.make_agent()
		agent.append("mcp_servers", {"mcp_server": server.name, "mcp_server_account": account.name})
		agent.append("mcp_servers", {"mcp_server": server.name, "mcp_server_account": account.name})
		self.assertRaises(frappe.ValidationError, agent.save)

	def test_default_and_named_account_of_one_server_are_both_allowed(self):
		server = self.make_server(server_key=_unique("figma"))
		named = self.make_account(server, account_key="acme")
		self.make_account(server, account_key="default", is_default=1)

		agent = self.make_agent(
			mcp_rows=[
				{"mcp_server": server.name, "mcp_server_account": named.name},
				{"mcp_server": server.name},
			]
		)
		self.assertEqual(len(agent.mcp_servers), 2)

	def test_effective_key_collision_fails_validation(self):
		suffix = frappe.generate_hash(length=6).lower()
		figma = self.make_server(server_key=f"figma-{suffix}")
		self.make_account(figma, account_key="default", is_default=1)
		acme = self.make_account(figma, account_key="acme")
		figma_acme = self.make_server(server_key=f"figma-{suffix}-acme")
		self.make_account(figma_acme, account_key="default", is_default=1)

		agent = self.make_agent()
		agent.append("mcp_servers", {"mcp_server": figma.name, "mcp_server_account": acme.name})
		agent.append("mcp_servers", {"mcp_server": figma_acme.name})
		self.assertRaises(frappe.ValidationError, agent.save)

	def test_level_two_agents_cannot_configure_mcp(self):
		server = self.make_server()
		self.make_account(server, is_default=1)
		self.assertRaises(
			frappe.ValidationError,
			self.make_agent,
			mcp_rows=[{"mcp_server": server.name}],
			agent_level="2",
		)

	def test_attachment_limit_is_enforced(self):
		server = self.make_server()
		self.make_account(server, is_default=1)
		self.assertRaises(
			frappe.ValidationError,
			self.make_agent,
			mcp_rows=[{"mcp_server": server.name} for _ in range(11)],
		)


class TestMCPResolution(MCPTestMixin, AFAATestSuite):
	def test_effective_key_derivation(self):
		self.assertEqual(effective_mcp_key("figma", "acme", True), "figma")
		self.assertEqual(effective_mcp_key("figma", None, True), "figma")
		self.assertEqual(effective_mcp_key("figma", "acme", False), "figma-acme")

	def test_resolution_expands_accounts_in_effective_key_order(self):
		server = self.make_server(server_key=_unique("zeta"))
		self.make_account(server, account_key="zulu", is_default=1)
		alpha = self.make_account(server, account_key="alpha", account_name="Alpha")
		agent = self.make_agent(
			mcp_rows=[
				{"mcp_server": server.name, "mcp_server_account": alpha.name},
				{"mcp_server": server.name},
			]
		)

		resolved = resolve_ai_agent(agent.agent_key, include_mcp_servers=True)
		self.assertEqual(
			[item.effective_key for item in resolved.mcp_servers],
			[server.server_key, f"{server.server_key}-alpha"],
		)
		self.assertEqual([item.account_key for item in resolved.mcp_servers], ["zulu", "alpha"])
		self.assertEqual(resolved.mcp_servers[0].name, "Test Server")
		self.assertEqual(resolved.mcp_servers[1].name, "Test Server (Alpha)")
		self.assertEqual(resolved.mcp_servers[1].token.get_secret_value(), "secret-token")
		self.assertEqual(resolved.mcp_servers[0].allowed_tools, ("get_file",))

	def test_resolution_is_opt_in_and_disabled_dependencies_fail_closed(self):
		server = self.make_server()
		account = self.make_account(server, is_default=1)
		agent = self.make_agent(mcp_rows=[{"mcp_server": server.name}])

		self.assertEqual(resolve_ai_agent(agent.agent_key).mcp_servers, ())
		self.assertEqual(len(resolve_ai_agent(agent.agent_key, include_mcp_servers=True).mcp_servers), 1)

		server.disabled = 1
		server.save(ignore_permissions=True)
		self.assertRaises(
			frappe.ValidationError,
			lambda: resolve_ai_agent(agent.agent_key, require_enabled=True, include_mcp_servers=True),
		)

		server.disabled = 0
		server.save(ignore_permissions=True)
		account.disabled = 1
		account.save(ignore_permissions=True)
		self.assertRaises(
			frappe.ValidationError,
			lambda: resolve_ai_agent(agent.agent_key, require_enabled=True, include_mcp_servers=True),
		)

	def test_explicit_default_account_collides_with_omitted_account(self):
		server = self.make_server()
		default = self.make_account(server, is_default=1)
		self.assertRaises(
			frappe.ValidationError,
			self.make_agent,
			mcp_rows=[
				{"mcp_server": server.name},
				{"mcp_server": server.name, "mcp_server_account": default.name},
			],
		)  # identical effective keys are rejected already at validation time


class TestExternalRuntimeSchemaV5(MCPTestMixin, AFAATestSuite):
	def setUp(self):
		account = frappe.get_doc("AI Provider Account", boot_strap_test_master_data.provider_account)
		account.api_key = "server-only-test-key"
		account.save(ignore_permissions=True)

	def make_mcp_agent(self):
		server = self.make_server(server_key=_unique("figma"))
		self.make_account(server, account_key="default", is_default=1)
		acme = self.make_account(server, account_key="acme", account_name="Acme")
		return self.make_agent(
			mcp_rows=[
				{"mcp_server": server.name},
				{"mcp_server": server.name, "mcp_server_account": acme.name},
			]
		), server

	def test_v5_payload_keeps_tokens_secret_and_private_transport_works(self):
		agent, server = self.make_mcp_agent()
		config = resolve_external_runtime(agent.agent_key, include_mcp_servers=True)

		self.assertIsInstance(config, MCPAwareExternalRuntimeConfig)
		self.assertEqual(config.schema_version, 5)
		self.assertEqual(config.mcp_contract_version, 1)
		self.assertEqual(
			sorted(item.key for item in config.mcp_servers),
			sorted([server.server_key, f"{server.server_key}-acme"]),
		)

		public = config.model_dump(mode="json", by_alias=True)
		for entry in public["mcpServers"]:
			self.assertEqual(entry["authorizationToken"], "**********")
		private = config.private_payload()
		tokens = [entry.get("authorizationToken") for entry in private["mcpServers"]]
		self.assertEqual(sorted(tokens), ["secret-token", "secret-token"])

	def test_v5_fingerprint_is_token_rotation_stable(self):
		agent, server = self.make_mcp_agent()
		first = resolve_external_runtime(agent.agent_key, include_mcp_servers=True)

		default_name = frappe.db.get_value(
			"AI MCP Server Account", {"mcp_server": server.name, "is_default": 1}, "name"
		)
		account = frappe.get_doc("AI MCP Server Account", default_name)
		account.bearer_token = "rotated-token"
		account.save(ignore_permissions=True)

		second = resolve_external_runtime(agent.agent_key, include_mcp_servers=True)
		self.assertEqual(first.configuration_fingerprint, second.configuration_fingerprint)

	def test_mcp_is_never_resolved_without_the_flag(self):
		agent, _server = self.make_mcp_agent()
		config = resolve_external_runtime(agent.agent_key)
		self.assertNotIsInstance(config, MCPAwareExternalRuntimeConfig)

	def test_mcp_dto_validation(self):
		from afaa.ai.external_runtime import ExternalRuntimeMCPServer

		values = {
			"key": "figma",
			"name": "Figma",
			"url": "https://mcp.example.test/mcp",
			"transport": "auto",
			"allowedTools": ["get_file"],
			"connectTimeout": 10,
			"readTimeout": 60,
		}
		entry = ExternalRuntimeMCPServer.model_validate({**values, "authorizationToken": "secret"})
		self.assertEqual(entry.authorization_token.get_secret_value(), "secret")
		self.assertRaises(
			Exception, ExternalRuntimeMCPServer.model_validate, {**values, "url": "http://mcp.example.test"}
		)
		self.assertRaises(
			Exception,
			ExternalRuntimeMCPServer.model_validate,
			{**values, "url": "https://mcp.example.test/a?b=1"},
		)
		self.assertRaises(
			Exception,
			ExternalRuntimeMCPServer.model_validate,
			{**values, "allowedTools": ["get_file", "get_file"]},
		)

	def test_codex_v5_descriptor_carries_private_mcp_servers(self):
		from types import SimpleNamespace

		from afaa.ai.external_runtime import (
			build_external_mcp_servers,
			resolve_codex_external_runtime,
		)

		resolved = SimpleNamespace(
			key="codex-agent",
			name="Codex Agent",
			prompt="Use MCP.",
			skills=(),
			tools=(),
			sub_agents=(),
			mcp_servers=(),
			agent_level="1",
			timeout=120.0,
			retries=2,
			model=SimpleNamespace(
				provider_type="openai_codex",
				provider_account="codex-provider-account",
				model_id="gpt-codex-test",
				settings={"openai_store": True, "temperature": 0.2},
			),
		)
		account = SimpleNamespace(
			name="codex-provider-account",
			disabled=0,
			oauth_status="Connected",
			connected_user="owner@example.test",
			external_account_id="account-123",
		)
		mcp_servers = build_external_mcp_servers(
			SimpleNamespace(
				mcp_servers=[
					SimpleNamespace(
						server_key="figma",
						account_key="default",
						effective_key="figma",
						name="Figma",
						url="https://mcp.example.test/mcp",
						transport="auto",
						allowed_tools=("get_file",),
						connect_timeout=10,
						read_timeout=60,
						auth_type=AUTH_TYPE_BEARER,
						token=SecretStr("secret-token"),
					)
				]
			)
		)
		with patch("frappe.get_doc", return_value=account):
			config = resolve_codex_external_runtime(
				resolved,
				("Use MCP.",),
				legacy_skill_instructions=False,
				mcp_servers=mcp_servers,
			)
		self.assertIsInstance(config, MCPAwareCodexExternalRuntimeDescriptor)
		self.assertEqual(config.mcp_servers[0].key, "figma")
		private = config.private_mcp_servers()
		self.assertEqual(private[0]["authorizationToken"], "secret-token")
		public = config.model_dump(mode="json", by_alias=True)
		self.assertEqual(public["mcpServers"][0]["authorizationToken"], "**********")


class TestMCPHostPolicy(MCPTestMixin, AFAATestSuite):
	def test_validate_mcp_url_normalizes(self):
		self.assertEqual(validate_mcp_url(" https://mcp.example.test/mcp "), "https://mcp.example.test/mcp")

	def test_allowlist_fails_closed_when_unset(self):
		with patch("afaa.ai.mcp.get_allowed_mcp_hosts", return_value=[]):
			self.assertEqual(get_allowed_mcp_hosts(), [])
			self.assertRaises(frappe.PermissionError, ensure_mcp_host_allowed, "https://mcp.example.test/mcp")

	def test_wildcard_accepts_public_hosts_and_rejects_private_destinations(self):
		def fake_getaddrinfo(host, port, **kwargs):
			addresses = {
				"mcp.example.test": "93.184.216.34",
				"internal.example.test": "169.254.169.254",
				"lan.example.test": "192.168.1.10",
				"loopback.example.test": "127.0.0.1",
			}
			address = addresses.get(host)
			if address is None:
				raise OSError("not found")
			return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

		with (
			patch("afaa.ai.mcp.get_allowed_mcp_hosts", return_value=["*"]),
			patch("afaa.ai.mcp.socket.getaddrinfo", side_effect=fake_getaddrinfo),
		):
			self.assertEqual(ensure_mcp_host_allowed("https://mcp.example.test/mcp"), "mcp.example.test")
			for host in ("internal", "lan", "loopback"):
				self.assertRaises(
					frappe.ValidationError,
					ensure_mcp_host_allowed,
					f"https://{host}.example.test/mcp",
				)

	def test_explicit_allowlist_is_enforced(self):
		with (
			patch("afaa.ai.mcp.get_allowed_mcp_hosts", return_value=["mcp.allowed.test"]),
			patch(
				"afaa.ai.mcp.socket.getaddrinfo",
				return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
			),
		):
			self.assertEqual(ensure_mcp_host_allowed("https://mcp.allowed.test/mcp"), "mcp.allowed.test")


class TestMCPDiscovery(MCPTestMixin, AFAATestSuite):
	def test_discovery_is_sanitized_and_never_persists(self):
		server = self.make_server()
		account = self.make_account(server, is_default=1)

		discovered = {
			"transport": "streamable_http",
			"serverName": "Test MCP",
			"serverVersion": "1.2.3",
			"toolCount": 1,
			"tools": [{"name": "get_file", "description": "Reads a file."}],
		}

		async def fake_discover(url, headers, transport, connect_timeout, read_timeout):
			self.assertEqual(url, server.url)
			self.assertEqual(headers, {"Authorization": "Bearer secret-token"})
			self.assertEqual(transport, "auto")
			return discovered

		with (
			patch("afaa.ai.mcp.ensure_mcp_host_allowed", return_value="mcp.example.test"),
			patch("afaa.ai.mcp._discover_tools", side_effect=fake_discover),
		):
			result = test_connection(server.name, account.name)

		self.assertTrue(result["ok"])
		self.assertEqual(result["effectiveKey"], server.server_key)
		self.assertEqual(result["tools"], [{"name": "get_file", "description": "Reads a file."}])
		self.assertEqual(frappe.db.count("AI MCP Server Tool", {"parent": server.name}), 1)

	def test_discovery_requires_manager_role(self):
		server = self.make_server()
		self.make_account(server, is_default=1)
		with (
			self.set_user("nobody@example.test"),
			self.assertRaises(frappe.PermissionError),
		):
			test_connection(server.name)

	def test_discovery_rejects_servers_without_a_default_account(self):
		server = self.make_server()
		self.assertRaises(frappe.ValidationError, test_connection, server.name)


class TestMCPAccountResolution(MCPTestMixin, AFAATestSuite):
	def test_resolve_account_requires_enabled_default_or_explicit(self):
		server = self.make_server()
		self.assertRaises(frappe.ValidationError, resolve_mcp_account, server.name, None)

		default = self.make_account(server, is_default=1)
		self.assertEqual(resolve_mcp_account(server.name, None).name, default.name)
		self.assertEqual(resolve_mcp_account(server.name, default.name).name, default.name)

		other = self.make_server()
		foreign = self.make_account(other, is_default=1)
		self.assertRaises(frappe.ValidationError, resolve_mcp_account, server.name, foreign.name)

		default.disabled = 1
		default.save(ignore_permissions=True)
		self.assertRaises(frappe.ValidationError, resolve_mcp_account, server.name, None)
