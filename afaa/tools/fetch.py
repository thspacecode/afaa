# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from typing import Any

import frappe
from frappe import _
from pydantic import BaseModel, ConfigDict, Field, RootModel

from afaa.ai.tools import tool

Filters = dict[str, Any] | list[list[Any]]


class ToolInput(BaseModel):
	model_config = ConfigDict(extra="forbid")


class GetDocTypeSchemaInput(ToolInput):
	doctype: str = Field(description="The Frappe DocType to inspect.")


class FieldSchema(BaseModel):
	fieldname: str
	label: str
	fieldtype: str
	options: str | None
	required: bool
	read_only: bool
	description: str | None


class ChildTableSchema(BaseModel):
	doctype: str
	fields: list[FieldSchema]


class GetDocTypeSchemaOutput(BaseModel):
	doctype: str
	is_single: bool
	is_submittable: bool
	title_field: str | None
	search_fields: list[str]
	standard_fields: list[str]
	fields: list[FieldSchema]
	child_tables: dict[str, ChildTableSchema]
	permissions: dict[str, bool]


class GetDocumentListInput(ToolInput):
	doctype: str
	fields: list[str] | None = None
	filters: Filters | None = None
	or_filters: Filters | None = None
	order_by: str | None = None
	limit_start: int = 0
	limit_page_length: int = 20


class GetDocumentInput(ToolInput):
	doctype: str
	name: str


class CountDocumentsInput(ToolInput):
	doctype: str
	filters: Filters | None = None
	or_filters: Filters | None = None


class DocumentOutput(RootModel[dict[str, Any]]):
	pass


class DocumentListOutput(RootModel[list[dict[str, Any]]]):
	pass


class DocumentCountOutput(RootModel[int]):
	pass


MAX_LIST_PAGE_LENGTH = 100


@tool(
	name="Get DocType Schema",
	description=(
		"Get the readable field schema and available operations for a Frappe DocType. "
		"Use this before reading or changing an unfamiliar DocType."
	),
	key="frappe_get_doctype_schema",
)
def frappe_get_doctype_schema(request: GetDocTypeSchemaInput) -> GetDocTypeSchemaOutput:
	"""Get the permission-aware schema for a DocType."""
	meta = frappe.get_meta(request.doctype)
	if meta.istable:
		frappe.throw(_("Child DocType schemas must be read through their parent DocType."))

	frappe.has_permission(request.doctype, "read", throw=True)
	readable_fields = set(meta.get_permitted_fieldnames(permission_type="read"))
	writable_fields = set(meta.get_permitted_fieldnames(permission_type="write"))

	fields = [
		_format_field(df, writable=df.fieldname in writable_fields)
		for df in meta.fields
		if df.fieldname in readable_fields and not df.hidden and df.fieldtype != "Password"
	]
	child_tables = {}
	for df in meta.get_table_fields(include_computed=True):
		if df.fieldname not in readable_fields or df.hidden:
			continue
		child_meta = frappe.get_meta(df.options)
		child_readable_fields = set(
			child_meta.get_permitted_fieldnames(parenttype=request.doctype, permission_type="read")
		)
		child_writable_fields = set(
			child_meta.get_permitted_fieldnames(parenttype=request.doctype, permission_type="write")
		)
		child_tables[df.fieldname] = {
			"doctype": df.options,
			"fields": [
				_format_field(child_df, writable=child_df.fieldname in child_writable_fields)
				for child_df in child_meta.fields
				if child_df.fieldname in child_readable_fields
				and not child_df.hidden
				and child_df.fieldtype != "Password"
			],
		}

	standard_fields = ["name", "owner", "creation", "modified", "modified_by"]
	if meta.is_submittable:
		standard_fields.append("docstatus")

	return GetDocTypeSchemaOutput(
		doctype=meta.name,
		is_single=bool(meta.issingle),
		is_submittable=bool(meta.is_submittable),
		title_field=meta.title_field or None,
		search_fields=[field.strip() for field in (meta.search_fields or "").split(",") if field.strip()],
		standard_fields=standard_fields,
		fields=fields,
		child_tables=child_tables,
		permissions={
			permission_type: bool(frappe.has_permission(request.doctype, permission_type))
			for permission_type in ("read", "create", "write", "delete", "submit", "cancel")
		},
	)


@tool(
	name="Get Document List",
	description=(
		"Get a permission-filtered list of Frappe documents. Filters may be a field-value object "
		"or Frappe filter rows such as [['status', '=', 'Open']]. Returns at most 100 rows per call."
	),
	key="frappe_get_list",
)
def frappe_get_list(request: GetDocumentListInput) -> DocumentListOutput:
	"""Get documents using Frappe's permission-aware list API."""
	return DocumentListOutput(
		frappe.get_list(
			request.doctype,
			fields=request.fields or ["name", "modified"],
			filters=request.filters,
			or_filters=request.or_filters,
			order_by=request.order_by,
			start=max(0, request.limit_start),
			limit=min(max(1, request.limit_page_length), MAX_LIST_PAGE_LENGTH),
		)
	)


@tool(
	name="Get Document",
	description="Get one Frappe document by DocType and name, including readable child rows.",
	key="frappe_get_doc",
)
def frappe_get_doc(request: GetDocumentInput) -> DocumentOutput:
	"""Get a document after applying document and field-level read permissions."""
	return DocumentOutput(_readable_document(frappe.get_doc(request.doctype, request.name)))


@tool(
	name="Count Documents",
	description="Count Frappe documents matching permission-aware filters.",
	key="frappe_get_count",
)
def frappe_get_count(request: CountDocumentsInput) -> DocumentCountOutput:
	"""Count documents using the same permission-aware query path as get_list."""
	result = frappe.get_list(
		request.doctype,
		fields=[{"COUNT": "*", "as": "count"}],
		filters=request.filters,
		or_filters=request.or_filters,
		limit=1,
	)
	return DocumentCountOutput(int(result[0].get("count", 0)) if result else 0)


def _format_field(df, *, writable: bool) -> dict[str, Any]:
	return {
		"fieldname": df.fieldname,
		"label": df.label,
		"fieldtype": df.fieldtype,
		"options": df.options or None,
		"required": bool(df.reqd),
		"read_only": bool(df.read_only or not writable),
		"description": df.description or None,
	}


def _readable_document(doc) -> dict[str, Any]:
	doc.check_permission("read")
	doc.apply_fieldlevel_read_permissions()
	return doc.as_dict()
