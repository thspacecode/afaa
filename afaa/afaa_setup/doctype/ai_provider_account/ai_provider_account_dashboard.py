from frappe import _


def get_data():
	return {
		"fieldname": "provider_account",
		"transactions": [
			{"label": _("Related"), "items": ["AI Agent"]},
		],
	}
