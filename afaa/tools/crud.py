# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from typing import Any

import frappe
from frappe import _
from frappe.model import default_fields
from pydantic import BaseModel, ConfigDict, RootModel

from afaa.ai.tools import tool


class ToolInput(BaseModel):
	model_config = ConfigDict(extra="forbid")


class CreateDocumentInput(ToolInput):
	doctype: str
	values: dict[str, Any]


class UpdateDocumentInput(ToolInput):
	doctype: str
	name: str
	values: dict[str, Any]


class DocumentActionInput(ToolInput):
	doctype: str
	name: str


class DocumentOutput(RootModel[dict[str, Any]]):
	pass


class DeleteDocumentOutput(BaseModel):
	doctype: str
	name: str
	deleted: bool


PROTECTED_CREATE_FIELDS = {
	"creation",
	"docstatus",
	"doctype",
	"flags",
	"idx",
	"modified",
	"modified_by",
	"owner",
	"parent",
	"parentfield",
	"parenttype",
}
PROTECTED_UPDATE_FIELDS = PROTECTED_CREATE_FIELDS | {"doctype", "name"}


@tool(
	name="Create Document",
	description=(
		"Create a draft Frappe document with the supplied field values. Validation, permissions, "
		"hooks, naming, and child-table handling are applied by the Document API."
	),
	key="frappe_create_doc",
)
def frappe_create_doc(request: CreateDocumentInput) -> DocumentOutput:
	"""Create a document as the current Frappe user."""
	_validate_mutation_values(request.doctype, request.values, creating=True)
	doc = frappe.get_doc({"doctype": request.doctype, **request.values})
	doc.insert()
	return DocumentOutput(_readable_document(doc))


@tool(
	name="Update Document",
	description=(
		"Update fields on an existing draft Frappe document. Validation, write permissions, hooks, "
		"and child-table handling are applied by the Document API."
	),
	key="frappe_update_doc",
)
def frappe_update_doc(request: UpdateDocumentInput) -> DocumentOutput:
	"""Update and save a document as the current Frappe user."""
	_validate_mutation_values(request.doctype, request.values)
	doc = frappe.get_doc(request.doctype, request.name)
	doc.update(request.values)
	doc.save()
	return DocumentOutput(_readable_document(doc))


@tool(
	name="Delete Document",
	description="Permanently delete a Frappe document after applying delete permissions and hooks.",
	key="frappe_delete_doc",
)
def frappe_delete_doc(request: DocumentActionInput) -> DeleteDocumentOutput:
	"""Delete a document as the current Frappe user."""
	frappe.delete_doc(request.doctype, request.name, ignore_missing=False)
	return DeleteDocumentOutput(doctype=request.doctype, name=request.name, deleted=True)


@tool(
	name="Submit Document",
	description="Submit an existing draft Frappe document after applying submit permissions and hooks.",
	key="frappe_submit_doc",
)
def frappe_submit_doc(request: DocumentActionInput) -> DocumentOutput:
	"""Submit a document as the current Frappe user."""
	doc = frappe.get_doc(request.doctype, request.name)
	doc.submit()
	return DocumentOutput(_readable_document(doc))


@tool(
	name="Cancel Document",
	description="Cancel an existing submitted Frappe document after applying cancel permissions and hooks.",
	key="frappe_cancel_doc",
)
def frappe_cancel_doc(request: DocumentActionInput) -> DocumentOutput:
	"""Cancel a submitted document as the current Frappe user."""
	doc = frappe.get_doc(request.doctype, request.name)
	doc.cancel()
	return DocumentOutput(_readable_document(doc))


def _validate_mutation_values(doctype: str, values: dict[str, Any], *, creating: bool = False):
	if not values:
		frappe.throw(_("Provide at least one field value."))

	meta = frappe.get_meta(doctype)
	protected_fields = PROTECTED_CREATE_FIELDS if creating else PROTECTED_UPDATE_FIELDS
	invalid_fields = set(values) & protected_fields
	invalid_fields.update(fieldname for fieldname in values if fieldname.startswith("_"))
	invalid_fields.update(
		fieldname for fieldname in values if fieldname not in default_fields and not meta.has_field(fieldname)
	)
	if creating:
		invalid_fields.discard("name")

	if invalid_fields:
		frappe.throw(
			_("These fields cannot be changed with this tool: {0}").format(", ".join(sorted(invalid_fields)))
		)

	_validate_no_control_values(values)


def _validate_no_control_values(value: Any):
	if isinstance(value, dict):
		control_fields = {key for key in value if key == "flags" or key.startswith("__")}
		if control_fields:
			frappe.throw(
				_("Document control fields are not allowed: {0}").format(", ".join(sorted(control_fields)))
			)
		for child_value in value.values():
			_validate_no_control_values(child_value)
	elif isinstance(value, list):
		for child_value in value:
			_validate_no_control_values(child_value)


def _readable_document(doc) -> dict[str, Any]:
	doc.check_permission("read")
	doc.apply_fieldlevel_read_permissions()
	return doc.as_dict()
