# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe.utils.password import remove_encrypted_password

from afaa.ai.oauth.openai_codex import (
	CONNECTED_APP_NAME,
	DEFAULT_CLIENT_ID,
	DEFAULT_ISSUER,
	DEFAULT_SCOPES,
)


def execute():
	"""Create or reconcile AFAA's managed public Codex OAuth client configuration."""
	client_id = frappe.conf.get("afaa_openai_codex_client_id") or DEFAULT_CLIENT_ID
	if frappe.db.exists("Connected App", CONNECTED_APP_NAME):
		connected_app = frappe.get_doc("Connected App", CONNECTED_APP_NAME)
	else:
		connected_app = frappe.new_doc("Connected App")

	connected_app.update(
		{
			"provider_name": CONNECTED_APP_NAME,
			"client_id": client_id,
			"client_secret": None,
			"authorization_uri": f"{DEFAULT_ISSUER}/oauth/authorize",
			"token_uri": f"{DEFAULT_ISSUER}/oauth/token",
			"revocation_uri": f"{DEFAULT_ISSUER}/oauth/revoke",
			"openid_configuration": None,
			"userinfo_uri": None,
			"introspection_uri": None,
			"query_parameters": [],
			"scopes": [{"scope": scope} for scope in DEFAULT_SCOPES],
		}
	)
	if connected_app.is_new():
		connected_app.insert(ignore_permissions=True, set_name=CONNECTED_APP_NAME)
	else:
		connected_app.save(ignore_permissions=True)

	# This is a public OAuth client. Ensure an accidentally entered secret is not retained.
	remove_encrypted_password("Connected App", CONNECTED_APP_NAME, "client_secret")
