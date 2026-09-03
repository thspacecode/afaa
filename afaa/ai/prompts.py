# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from typing import Any

import frappe
from frappe import _
from frappe.utils.jinja import get_jenv, safe_render_flags
from jinja2 import StrictUndefined, TemplateError

SAFE_FILTERS = {
	"abs",
	"capitalize",
	"default",
	"escape",
	"first",
	"float",
	"int",
	"join",
	"json",
	"last",
	"len",
	"list",
	"lower",
	"replace",
	"round",
	"sort",
	"str",
	"title",
	"trim",
	"upper",
}


def parse_json_object(value, label: str) -> dict[str, Any]:
	if not value:
		return {}

	try:
		parsed = frappe.parse_json(value)
		if not isinstance(parsed, dict):
			frappe.throw(_("{0} must be a JSON object.").format(label))
		return frappe.parse_json(frappe.as_json(parsed))
	except frappe.ValidationError:
		raise
	except Exception:
		frappe.throw(_("{0} must contain a JSON-serializable object.").format(label))


def validate_jinja_template(content: str) -> None:
	_parse_template(content)


def render_system_prompt(content: str, context=None) -> str:
	context = parse_json_object(context, _("System Prompt Context"))
	validate_jinja_template(content)
	return _render(content, context)


def render_task_instructions(content: str, context=None) -> str:
	context = parse_json_object(context, _("Task Context"))
	validate_jinja_template(content)
	return _render(content, context)


def _get_environment():
	base = get_jenv(restrict_globals=True)
	environment = base.overlay(undefined=StrictUndefined)
	environment.globals = {}
	environment.filters = {name: base.filters[name] for name in SAFE_FILTERS if name in base.filters}
	return environment


def _parse_template(content: str):
	if ".__" in (content or ""):
		frappe.throw(_("Illegal template attribute access."))
	try:
		return _get_environment().parse(content or "")
	except TemplateError as error:
		frappe.throw(_("Invalid Jinja template: {0}").format(str(error)))


def _render(content: str, context: dict[str, Any]) -> str:
	try:
		template = _get_environment().from_string(content)
		with safe_render_flags():
			return template.render(context)
	except TemplateError as error:
		frappe.throw(_("Unable to render Jinja template: {0}").format(str(error)))
