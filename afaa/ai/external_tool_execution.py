# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from typing import Any

import frappe
from frappe import _

from afaa.ai.tools import EXTERNAL_READ_TOOL_METHODS, _get_tool_models, get_tool_definition


def execute_external_tool(tool_key: str, arguments: dict[str, Any]) -> Any:
	"""Validate and execute one approved registered tool as the current Frappe user.

	Callers provide only a stable tool key and JSON arguments. Registered dotted
	methods are resolved exclusively inside this trust boundary and are never part
	of the external execution contract.
	"""
	expected_method = EXTERNAL_READ_TOOL_METHODS.get(tool_key) if isinstance(tool_key, str) else None
	if not expected_method:
		frappe.throw(_("Tool key is not supported for external execution."), frappe.ValidationError)
	if not isinstance(arguments, dict):
		frappe.throw(_("Tool arguments must be a JSON object."), frappe.ValidationError)
	if not frappe.session.user or frappe.session.user == "Guest":
		raise frappe.PermissionError(_("An authenticated Frappe user is required to execute AI tools."))

	definition = get_tool_definition(tool_key)
	if not definition or definition.key != tool_key or definition.method != expected_method:
		frappe.throw(
			_("AI Tool {0} is not registered as an approved read-only tool.").format(frappe.bold(tool_key)),
			frappe.ValidationError,
		)

	status = frappe.db.get_value("AI Tool", tool_key, ["disabled", "available"], as_dict=True)
	if not status or status.disabled or not status.available:
		frappe.throw(
			_("AI Tool {0} is disabled or unavailable.").format(frappe.bold(tool_key)),
			frappe.ValidationError,
		)

	function = frappe.get_attr(definition.method)
	if getattr(function, "__afaa_tool_definition__", None) != definition:
		frappe.throw(
			_("AI Tool {0} does not match its registered definition.").format(frappe.bold(tool_key)),
			frappe.ValidationError,
		)

	input_model, output_model = _get_tool_models(function)
	validated_arguments = input_model.model_validate(arguments)
	result = function(validated_arguments)
	return output_model.model_validate(result).model_dump(mode="json")
