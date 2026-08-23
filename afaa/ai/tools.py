# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import importlib
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, get_type_hints

import frappe
from frappe import _
from pydantic import BaseModel


@dataclass(frozen=True)
class ToolDefinition:
	key: str
	name: str
	description: str
	method: str
	input_schema: dict[str, Any]
	output_schema: dict[str, Any]
	source_app: str = "afaa"


def tool(
	*,
	name: str | None = None,
	description: str | None = None,
	key: str | None = None,
) -> Callable:
	"""Register a module-level function as an AFAA tool."""

	def decorator(function: Callable) -> Callable:
		if function.__qualname__ != function.__name__:
			raise ValueError("AFAA tools must be module-level functions")

		tool_key = key or function.__name__
		tool_description = description or inspect.getdoc(function)
		if not tool_description:
			raise ValueError(f"AFAA tool {tool_key} requires a description or docstring")

		input_model, output_model = _get_tool_models(function)
		definition = ToolDefinition(
			key=tool_key,
			name=name or tool_key.replace("_", " ").title(),
			description=tool_description,
			method=f"{function.__module__}.{function.__name__}",
			input_schema=input_model.model_json_schema(mode="validation"),
			output_schema=output_model.model_json_schema(mode="serialization"),
			source_app=function.__module__.split(".", 1)[0],
		)
		function.__afaa_tool_definition__ = definition
		return function

	return decorator


def get_tool_definition(tool_key: str) -> ToolDefinition | None:
	return _get_registered_tools().get(tool_key)


@frappe.whitelist()
def sync_registered_tools() -> dict[str, list[str]]:
	if getattr(frappe.local, "request", None):
		frappe.only_for(["AI Manager", "System Manager"])

	definitions = _get_registered_tools()
	report = {"created": [], "updated": [], "unavailable": []}

	for name in frappe.get_all("AI Tool", pluck="name"):
		if name not in definitions:
			frappe.db.set_value("AI Tool", name, {"available": 0, "disabled": 1}, update_modified=False)
			report["unavailable"].append(name)

	for definition in definitions.values():
		values = {
			"tool_name": definition.name,
			"description": definition.description,
			"source_app": definition.source_app,
			"method": definition.method,
			"input_schema": json.dumps(definition.input_schema, indent=2, sort_keys=True),
			"output_schema": json.dumps(definition.output_schema, indent=2, sort_keys=True),
			"available": 1,
		}
		if frappe.db.exists("AI Tool", definition.key):
			doc = frappe.get_doc("AI Tool", definition.key)
			doc.update(values)
			doc.flags.from_registry_sync = True
			doc.save(ignore_permissions=True)
			report["updated"].append(definition.key)
		else:
			doc = frappe.get_doc(
				{
					"doctype": "AI Tool",
					"tool_key": definition.key,
					"disabled": 0,
					**values,
				}
			)
			doc.flags.from_registry_sync = True
			doc.insert(ignore_permissions=True)
			report["created"].append(definition.key)

	return report


def _get_registered_tools() -> dict[str, ToolDefinition]:
	definitions = {}
	module_paths = ["afaa.tools.crud", "afaa.tools.fetch", "afaa.tools.files"]
	module_paths.extend(f"{app}.ai.tools" for app in frappe.get_installed_apps())

	for module_path in module_paths:
		try:
			module = importlib.import_module(module_path)
		except ModuleNotFoundError as error:
			if error.name and (module_path == error.name or module_path.startswith(f"{error.name}.")):
				continue
			frappe.log_error(title=_("Unable to load AFAA tools"), message=frappe.get_traceback())
			continue
		except Exception:
			frappe.log_error(title=_("Unable to load AFAA tools"), message=frappe.get_traceback())
			continue

		for registered in vars(module).values():
			definition = getattr(registered, "__afaa_tool_definition__", None)
			if not isinstance(definition, ToolDefinition):
				continue
			frappe.get_attr(definition.method)
			definitions[definition.key] = definition

	return definitions


def _get_tool_models(function: Callable) -> tuple[type[BaseModel], type[BaseModel]]:
	parameters = list(inspect.signature(function).parameters.values())
	if len(parameters) != 1 or parameters[0].kind not in {
		inspect.Parameter.POSITIONAL_ONLY,
		inspect.Parameter.POSITIONAL_OR_KEYWORD,
	}:
		raise TypeError("AFAA tools must accept exactly one Pydantic input model")

	type_hints = get_type_hints(function)
	input_model = type_hints.get(parameters[0].name)
	output_model = type_hints.get("return")
	if not _is_model_type(input_model):
		raise TypeError("AFAA tool input must be a Pydantic BaseModel subclass")
	if not _is_model_type(output_model):
		raise TypeError("AFAA tool output must be a Pydantic BaseModel subclass")

	return input_model, output_model


def _is_model_type(annotation: Any) -> bool:
	return isinstance(annotation, type) and issubclass(annotation, BaseModel)
