from frappe import _


def get_data():
	return {
		"fieldname": "tool",
		"transactions": [
			{"label": _("Related"), "items": ["AI Agent", "AI Skill", "AI Task Definition"]},
		],
	}
