# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe

from afaa.ai.agent_levels import SUB_AGENT_RUNTIME_TOOL_KEYS, runtime_tool_definition


def execute():
	"""Create or reconcile the reserved runtime tool records.

	These keys are implemented by Porch Agent's workspace runtime and exist in
	AFAA only so administrators can add them to a Sub Agent's allowed tools.
	"""
	for tool_key in sorted(SUB_AGENT_RUNTIME_TOOL_KEYS):
		definition = runtime_tool_definition(tool_key)
		if definition is None:  # pragma: no cover - keys and definitions are 1:1
			continue
		if frappe.db.exists("AI Tool", tool_key):
			doc = frappe.get_doc("AI Tool", tool_key)
		else:
			doc = frappe.new_doc("AI Tool")
			doc.tool_key = tool_key
			doc.disabled = 0
		doc.update(definition)
		doc.flags.from_registry_sync = True
		doc.save(ignore_permissions=True)
