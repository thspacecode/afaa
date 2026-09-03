# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import base64
import binascii
from typing import Literal

import frappe
from frappe import _
from pydantic import BaseModel, ConfigDict, Field

from afaa.ai.tools import tool


class ToolInput(BaseModel):
	model_config = ConfigDict(extra="forbid")


class UploadFileInput(ToolInput):
	file_name: str = Field(min_length=1, description="File name including its extension.")
	content: str = Field(
		min_length=1,
		description="File content as UTF-8 text or base64, according to content_encoding.",
	)
	content_encoding: Literal["text", "base64"] = Field(
		default="text",
		description="Use text for UTF-8 content or base64 for binary content.",
	)
	is_private: bool = Field(default=True, description="Store the file as private by default.")


class AttachFileInput(ToolInput):
	file_id: str = Field(description="Name/ID of an existing Frappe File document.")
	doctype: str = Field(description="DocType of the target document.")
	name: str = Field(description="Name of the target document.")
	fieldname: str | None = Field(
		default=None,
		description="Optional Attach or Attach Image field to populate with the file URL.",
	)


class FileOutput(BaseModel):
	name: str
	file_name: str
	file_url: str
	file_size: int
	is_private: bool
	attached_to_doctype: str | None
	attached_to_name: str | None
	attached_to_field: str | None


@tool(
	name="Upload File",
	description=(
		"Upload a new private or public Frappe file from UTF-8 text or base64 content. "
		"The file is initially unattached; use Attach File to link it to a document."
	),
	key="frappe_upload_file",
)
def frappe_upload_file(request: UploadFileInput) -> FileOutput:
	"""Upload an unattached file as the current Frappe user."""
	content = request.content.encode("utf-8")
	if request.content_encoding == "base64":
		content = _decode_base64_content(request.content)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": request.file_name,
			"content": content,
			"is_private": request.is_private,
		}
	).insert()
	return _format_file(file_doc)


@tool(
	name="Attach File",
	description=(
		"Attach an existing Frappe File to a document by creating an attachment copy that reuses "
		"the stored file. Optionally populate an Attach or Attach Image field on the target document."
	),
	key="frappe_attach_file",
)
def frappe_attach_file(request: AttachFileInput) -> FileOutput:
	"""Attach an accessible file to a writable document as the current Frappe user."""
	source_file = frappe.get_doc("File", request.file_id)
	source_file.check_permission("read")

	target = frappe.get_doc(request.doctype, request.name)
	target.check_permission("write")
	_validate_attach_field(request.doctype, request.fieldname)

	save_point = f"afaa_attach_{frappe.generate_hash(length=10)}"
	frappe.db.savepoint(save_point)
	try:
		attachment = source_file.create_attachment_copy(
			request.doctype,
			request.name,
			attached_to_field=request.fieldname,
		)
		if request.fieldname:
			target.set(request.fieldname, attachment.file_url)
			target.save()
	except Exception:
		frappe.db.rollback(save_point=save_point)
		raise
	else:
		frappe.db.release_savepoint(save_point)

	return _format_file(attachment)


def _decode_base64_content(content: str) -> bytes:
	encoded_content = content
	if content.startswith("data:"):
		header, separator, encoded_content = content.partition(",")
		if not separator or ";base64" not in header.lower():
			frappe.throw(_("File content must be a base64 data URI."))

	try:
		decoded_content = base64.b64decode("".join(encoded_content.split()), validate=True)
	except binascii.Error, ValueError:
		frappe.throw(_("File content must be valid base64 data."))

	if not decoded_content:
		frappe.throw(_("Decoded file content cannot be empty."))
	return decoded_content


def _validate_attach_field(doctype: str, fieldname: str | None):
	if not fieldname:
		return

	meta = frappe.get_meta(doctype)
	field = meta.get_field(fieldname)
	if not field or field.fieldtype not in {"Attach", "Attach Image"}:
		frappe.throw(
			_("Field {0} must be an Attach or Attach Image field on {1}.").format(
				frappe.bold(fieldname), frappe.bold(doctype)
			)
		)

	writable_fields = set(meta.get_permitted_fieldnames(permission_type="write"))
	if field.read_only or fieldname not in writable_fields:
		frappe.throw(
			_("You do not have permission to write attachment field {0}.").format(frappe.bold(fieldname)),
			frappe.PermissionError,
		)


def _format_file(file_doc) -> FileOutput:
	return FileOutput(
		name=file_doc.name,
		file_name=file_doc.file_name,
		file_url=file_doc.file_url,
		file_size=file_doc.file_size or 0,
		is_private=bool(file_doc.is_private),
		attached_to_doctype=file_doc.attached_to_doctype or None,
		attached_to_name=file_doc.attached_to_name or None,
		attached_to_field=file_doc.attached_to_field or None,
	)
