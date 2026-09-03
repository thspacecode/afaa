import json

import frappe
from frappe import _


def get_data():
	return {
		"fieldname": "provider",
		"method": "afaa.afaa_setup.doctype.ai_provider.ai_provider_dashboard.get_open_count",
		"transactions": [
			{"label": _("Related"), "items": ["AI Provider Account", "AI Model", "AI Agent"]},
		],
	}


@frappe.whitelist()
@frappe.read_only()
def get_open_count(doctype: str, name: str, items=None):
	if doctype != "AI Provider":
		frappe.throw(_("Invalid DocType"))

	frappe.get_doc(doctype, name).check_permission("read")
	if isinstance(items, str):
		items = json.loads(items)
	items = set(items or ["AI Provider Account", "AI Model", "AI Agent"])

	model_names = []
	if items.intersection({"AI Model", "AI Agent"}) and frappe.has_permission("AI Model", "read"):
		model_names = frappe.get_list(
			"AI Model",
			filters={"provider": name},
			pluck="name",
			order_by="name",
			limit_page_length=None,
		)

	external_links = []
	if "AI Provider Account" in items and frappe.has_permission("AI Provider Account", "read"):
		account_count = frappe.get_list(
			"AI Provider Account",
			filters={"provider": name},
			pluck="name",
			limit_page_length=None,
		)
		external_links.append(
			{"doctype": "AI Provider Account", "count": len(account_count), "open_count": 0}
		)
	if "AI Model" in items and frappe.has_permission("AI Model", "read"):
		external_links.append({"doctype": "AI Model", "count": len(model_names), "open_count": 0})

	internal_links = []
	if "AI Agent" in items and frappe.has_permission("AI Agent", "read"):
		agent_names = []
		if model_names:
			agent_names = frappe.get_list(
				"AI Agent",
				filters={"model": ["in", model_names]},
				pluck="name",
				order_by="name",
				limit_page_length=None,
			)
		internal_links.append(
			{
				"doctype": "AI Agent",
				"count": len(agent_names),
				"open_count": 0,
				"names": agent_names,
			}
		)

	return {
		"count": {
			"external_links_found": external_links,
			"internal_links_found": internal_links,
		}
	}
