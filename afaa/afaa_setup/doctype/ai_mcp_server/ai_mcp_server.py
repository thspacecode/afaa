# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from afaa.ai.mcp import (
	MAX_ALLOWED_TOOLS_PER_SERVER,
	MAX_CONNECT_TIMEOUT,
	MAX_READ_TIMEOUT,
	MCP_TOOL_NAME_PATTERN,
	MIN_CONNECT_TIMEOUT,
	MIN_READ_TIMEOUT,
	validate_mcp_url,
)
from afaa.utils.data import validate_key


class AIMCPServer(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from afaa.afaa_setup.doctype.ai_mcp_server_tool.ai_mcp_server_tool import AIMCPServerTool

		allowed_tools: DF.Table[AIMCPServerTool]
		connect_timeout: DF.Int
		description: DF.SmallText | None
		disabled: DF.Check
		read_timeout: DF.Int
		server_key: DF.Data
		server_name: DF.Data
		transport: DF.Literal["", "auto", "streamable_http", "sse"]
		url: DF.Data
	# end: auto-generated types

	def validate(self):
		self.validate_server_key()
		self.validate_url()
		self.validate_timeouts()
		self.validate_allowed_tools()

	def validate_server_key(self):
		validate_key(self.server_key, _("Server Key"))
		if not self.is_new():
			previous = self.get_doc_before_save()
			if previous and previous.server_key != self.server_key:
				frappe.throw(
					_("Server Key cannot be changed after the AI MCP Server is created."),
					frappe.PermissionError,
				)

	def validate_url(self):
		self.url = validate_mcp_url(self.url)

	def validate_timeouts(self):
		self.connect_timeout = _bounded(self.connect_timeout, default=10)
		self.read_timeout = _bounded(self.read_timeout, default=60)
		if not MIN_CONNECT_TIMEOUT <= self.connect_timeout <= MAX_CONNECT_TIMEOUT:
			frappe.throw(
				_("Connect Timeout must be between {0} and {1} seconds.").format(
					MIN_CONNECT_TIMEOUT, MAX_CONNECT_TIMEOUT
				)
			)
		if not MIN_READ_TIMEOUT <= self.read_timeout <= MAX_READ_TIMEOUT:
			frappe.throw(
				_("Read Timeout must be between {0} and {1} seconds.").format(
					MIN_READ_TIMEOUT, MAX_READ_TIMEOUT
				)
			)

	def validate_allowed_tools(self):
		seen: set[str] = set()
		for row in self.allowed_tools or []:
			tool_name = (row.tool_name or "").strip()
			row.tool_name = tool_name
			if not MCP_TOOL_NAME_PATTERN.fullmatch(tool_name):
				frappe.throw(
					_("Allowed tool {0} is not a valid MCP tool name.").format(frappe.bold(tool_name or "")),
					frappe.ValidationError,
				)
			if tool_name in seen:
				frappe.throw(_("Allowed tool {0} is listed more than once.").format(frappe.bold(tool_name)))
			seen.add(tool_name)
		if len(seen) > MAX_ALLOWED_TOOLS_PER_SERVER:
			frappe.throw(_("An MCP server may allow at most {0} tools.").format(MAX_ALLOWED_TOOLS_PER_SERVER))

	def on_trash(self):
		if frappe.db.exists("AI Agent MCP Server", {"mcp_server": self.name, "parenttype": "AI Agent"}):
			parents = frappe.get_all(
				"AI Agent MCP Server",
				filters={"mcp_server": self.name, "parenttype": "AI Agent"},
				pluck="parent",
			)
			frappe.throw(
				_(
					"AI MCP Server {0} is referenced by AI Agent {1}. Remove the reference before trashing."
				).format(frappe.bold(self.name), frappe.bold(", ".join(sorted(set(parents))))),
				frappe.ValidationError,
			)
		if frappe.db.exists("AI MCP Server Account", {"mcp_server": self.name}):
			frappe.throw(
				_("AI MCP Server {0} still has accounts. Remove them before trashing the server.").format(
					frappe.bold(self.name)
				),
				frappe.ValidationError,
			)


def _bounded(value, *, default: int) -> int:
	try:
		parsed = int(value)
	except TypeError, ValueError:
		return default
	if parsed <= 0:
		return default
	return parsed
