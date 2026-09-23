# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from afaa.ai.mcp import AUTH_TYPE_BEARER, AUTH_TYPE_NONE
from afaa.utils.data import validate_key


class AIMCPServerAccount(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		account_key: DF.Data
		account_name: DF.Data
		auth_type: DF.Literal["", "none", "bearer_token"]
		bearer_token: DF.Password | None
		disabled: DF.Check
		is_default: DF.Check
		mcp_server: DF.Link
	# end: auto-generated types

	def validate(self):
		self.validate_identity()
		self.validate_credentials()
		self.validate_default_binding()

	def validate_identity(self):
		validate_key(self.account_key, _("Account Key"))
		if not self.is_new():
			previous = self.get_doc_before_save()
			if previous and (
				previous.account_key != self.account_key or previous.mcp_server != self.mcp_server
			):
				frappe.throw(
					_("Account Key and MCP Server cannot be changed after the account is created."),
					frappe.PermissionError,
				)
		# ``format:`` autoname sets self.name before validate(), so the name
		# exclusion applies only to persisted (renamed) documents; a new
		# document colliding with an existing (server, key) pair is detected
		# here instead of failing later as a raw duplicate-name integrity error.
		filters = {"mcp_server": self.mcp_server, "account_key": self.account_key}
		if not self.is_new():
			filters["name"] = ("!=", self.name)
		duplicate = frappe.db.get_value("AI MCP Server Account", filters, "name")
		if duplicate:
			frappe.throw(
				_("Account Key {0} already exists for MCP server {1}.").format(
					frappe.bold(self.account_key), frappe.bold(self.mcp_server)
				)
			)

	def validate_credentials(self):
		self.auth_type = self.auth_type or AUTH_TYPE_NONE
		if self.auth_type not in {AUTH_TYPE_NONE, AUTH_TYPE_BEARER}:
			frappe.throw(_("Authentication type is invalid."), frappe.ValidationError)
		if self.auth_type != AUTH_TYPE_BEARER:
			return
		if self.disabled:
			return
		token = self.get_password("bearer_token", raise_exception=False)
		if not token:
			frappe.throw(
				_("Account {0} uses bearer token authentication but has no token.").format(
					frappe.bold(self.account_name)
				)
			)

	def validate_default_binding(self):
		defaults = frappe.get_all(
			"AI MCP Server Account",
			filters={"mcp_server": self.mcp_server, "is_default": 1, "name": ("!=", self.name)},
			pluck="name",
		)
		if self.is_default:
			if defaults:
				frappe.throw(
					_(
						"MCP server {0} already has a default account ({1}). Only one default account is allowed."
					).format(frappe.bold(self.mcp_server), frappe.bold(defaults[0])),
					frappe.ValidationError,
				)
			return
		accounts = frappe.get_all(
			"AI MCP Server Account",
			filters={"mcp_server": self.mcp_server, "name": ("!=", self.name)},
			pluck="name",
		)
		if accounts and not defaults:
			frappe.throw(
				_(
					"MCP server {0} must keep exactly one default account. Mark one account as default first."
				).format(frappe.bold(self.mcp_server)),
				frappe.ValidationError,
			)
