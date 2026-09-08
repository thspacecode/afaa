# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Any, Literal

import frappe
from frappe import _
from frappe.utils import cint, now_datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BUNDLE_ROOT_PARENT = "Home/afaa/ai-skill"
EMPTY_BUNDLE_MANIFEST = {"files": []}
EMPTY_BUNDLE_DIGEST = hashlib.sha256(b'{"files":[]}').hexdigest()


@dataclass(frozen=True)
class SkillBundleLimits:
	max_files: int
	max_depth: int
	max_path_length: int
	max_file_bytes: int
	max_bundle_bytes: int
	max_agent_bundle_bytes: int


@dataclass(frozen=True)
class EditableBundleFile:
	file_id: str
	relative_path: str
	content: bytes


class SkillBundleManifestFile(BaseModel):
	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, strict=True)

	path: str = Field(min_length=1)
	byte_length: int = Field(alias="byteLength", ge=0)
	sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
	encoding: Literal["base64"] = "base64"

	@field_validator("path")
	@classmethod
	def validate_path(cls, value: str) -> str:
		return canonical_relative_path(value)


class SkillBundleReference(BaseModel):
	"""Small immutable reference suitable for a thread snapshot."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, strict=True)

	version_id: str = Field(alias="versionId", pattern=r"^[0-9a-f]{64}$")
	digest: str = Field(pattern=r"^[0-9a-f]{64}$")
	file_count: int = Field(alias="fileCount", ge=0)
	total_bytes: int = Field(alias="totalBytes", ge=0)


class SkillBundleRuntimeFile(SkillBundleManifestFile):
	content: str

	@model_validator(mode="after")
	def validate_content(self) -> SkillBundleRuntimeFile:
		try:
			decoded = base64.b64decode(self.content, validate=True)
		except binascii.Error, ValueError:
			raise ValueError("bundle file content is not valid base64") from None
		if len(decoded) != self.byte_length:
			raise ValueError("bundle file byte length does not match its content")
		if hashlib.sha256(decoded).hexdigest() != self.sha256:
			raise ValueError("bundle file digest does not match its content")
		return self


class ResolvedSkillBundle(BaseModel):
	"""Trusted machine-only bundle payload containing exact pinned bytes."""

	model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, strict=True)

	version_id: str = Field(alias="versionId", pattern=r"^[0-9a-f]{64}$")
	skill_key: str = Field(alias="skillKey", pattern=r"^[a-z0-9][a-z0-9_-]{0,139}$")
	digest: str = Field(pattern=r"^[0-9a-f]{64}$")
	manifest: tuple[SkillBundleManifestFile, ...]
	files: tuple[SkillBundleRuntimeFile, ...]
	file_count: int = Field(alias="fileCount", ge=0)
	total_bytes: int = Field(alias="totalBytes", ge=0)

	@model_validator(mode="after")
	def validate_bundle(self) -> ResolvedSkillBundle:
		if self.file_count != len(self.manifest) or len(self.files) != len(self.manifest):
			raise ValueError("bundle file count does not match its manifest")
		manifest_values = [item.model_dump(mode="json", by_alias=True) for item in self.manifest]
		file_manifest_values = [
			item.model_dump(mode="json", by_alias=True, exclude={"content"}) for item in self.files
		]
		if file_manifest_values != manifest_values:
			raise ValueError("bundle files do not match its manifest")
		if sum(item.byte_length for item in self.manifest) != self.total_bytes:
			raise ValueError("bundle total byte count does not match its manifest")
		if bundle_digest(manifest_values) != self.digest:
			raise ValueError("bundle digest does not match its manifest")
		return self

	@property
	def reference(self) -> SkillBundleReference:
		return SkillBundleReference(
			versionId=self.version_id,
			digest=self.digest,
			fileCount=self.file_count,
			totalBytes=self.total_bytes,
		)


def get_skill_bundle_limits() -> SkillBundleLimits:
	"""Return positive, deployment-configurable limits for synchronous bundle delivery."""
	defaults = {
		"max_files": 100,
		"max_depth": 8,
		"max_path_length": 512,
		"max_file_bytes": 1024 * 1024,
		"max_bundle_bytes": 5 * 1024 * 1024,
		"max_agent_bundle_bytes": 10 * 1024 * 1024,
	}
	values = {
		name: cint(frappe.conf.get(f"afaa_skill_bundle_{name}")) or default
		for name, default in defaults.items()
	}
	for name, value in values.items():
		if value <= 0:
			frappe.throw(
				_("AFAA skill bundle limit {0} must be greater than zero.").format(
					frappe.bold(f"afaa_skill_bundle_{name}")
				),
				frappe.ValidationError,
			)
	return SkillBundleLimits(**values)


def canonical_relative_path(path: str, *, allow_empty: bool = False) -> str:
	"""Validate one POSIX bundle path without normalizing unsafe input."""
	if not isinstance(path, str):
		raise ValueError("bundle path must be text")
	if not path:
		if allow_empty:
			return ""
		raise ValueError("bundle path cannot be empty")
	if path.startswith("/") or PurePosixPath(path).is_absolute():
		raise ValueError("bundle path cannot be absolute")
	if "\\" in path:
		raise ValueError("bundle paths must use '/' separators")
	segments = path.split("/")
	if any(segment in {"", ".", ".."} for segment in segments):
		raise ValueError("bundle path contains an empty, '.' or '..' segment")
	if any(
		any(ord(character) < 32 or ord(character) == 127 for character in segment) for segment in segments
	):
		raise ValueError("bundle path contains a control character")
	if any(len(segment) > 140 for segment in segments):
		raise ValueError("bundle path segment exceeds the Frappe File name limit (140)")
	return path


def validate_relative_path_limits(path: str, limits: SkillBundleLimits | None = None) -> str:
	limits = limits or get_skill_bundle_limits()
	path = canonical_relative_path(path)
	if len(path) > limits.max_path_length:
		raise frappe.ValidationError(
			_("Bundle path exceeds afaa_skill_bundle_max_path_length ({0}).").format(limits.max_path_length)
		)
	if len(path.split("/")) > limits.max_depth:
		raise frappe.ValidationError(
			_("Bundle path exceeds afaa_skill_bundle_max_depth ({0}).").format(limits.max_depth)
		)
	return path


def skill_bundle_root(skill_key: str) -> str:
	from afaa.utils.data import validate_key

	validate_key(skill_key, _("Skill Key"))
	return f"{BUNDLE_ROOT_PARENT}/{skill_key}"


def validate_managed_folder_path(skill_name: str, relative_path: str) -> None:
	if len(f"{skill_bundle_root(skill_name)}/{relative_path}") > 140:
		raise frappe.ValidationError(
			_("Bundle folder path exceeds the Frappe managed-folder path limit (140).")
		)


def require_skill_permission(skill_name: str, permission_type: Literal["read", "write"]):
	"""Resolve a skill without revealing its existence to an unauthorized caller."""
	try:
		skill = frappe.get_doc("AI Skill", skill_name)
	except frappe.DoesNotExistError:
		raise frappe.PermissionError(_("You are not permitted to access this AI Skill bundle.")) from None
	if skill.name != skill.skill_key or not skill.has_permission(permission_type):
		raise frappe.PermissionError(_("You are not permitted to access this AI Skill bundle."))
	return skill


def lock_skill(skill_name: str) -> None:
	frappe.db.sql("select name from `tabAI Skill` where name = %s for update", skill_name)


def ensure_skill_bundle_root(skill_name: str) -> str:
	"""Create or validate the exact managed File root for an AI Skill."""
	skill = frappe.get_doc("AI Skill", skill_name)
	root = skill_bundle_root(skill.skill_key)
	for folder_name, parent, owner in (
		("afaa", "Home", None),
		("ai-skill", "Home/afaa", None),
		(skill.skill_key, BUNDLE_ROOT_PARENT, skill.name),
	):
		name = f"{parent}/{folder_name}"
		existing = frappe.db.get_value(
			"File",
			name,
			[
				"name",
				"is_folder",
				"is_private",
				"folder",
				"attached_to_doctype",
				"attached_to_name",
			],
			as_dict=True,
		)
		if existing:
			valid_owner = (
				(existing.attached_to_doctype, existing.attached_to_name) == ("AI Skill", owner)
				if owner
				else not existing.attached_to_doctype and not existing.attached_to_name
			)
			if (
				not existing.is_folder
				or not existing.is_private
				or existing.folder != parent
				or not valid_owner
			):
				frappe.throw(
					_("The managed AI Skill bundle root is already assigned incorrectly."),
					frappe.ValidationError,
				)
			continue
		folder = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": folder_name,
				"folder": parent,
				"is_folder": 1,
				"is_private": 1,
				"attached_to_doctype": "AI Skill" if owner else None,
				"attached_to_name": owner,
			}
		)
		folder.flags.afaa_bundle_internal = True
		try:
			folder.insert(ignore_permissions=True)
		except frappe.DuplicateEntryError:
			# A concurrent creator won. Re-enter to verify that it created the expected root.
			return ensure_skill_bundle_root(skill_name)
	return root


def validate_bundle_file_record(doc, _method=None) -> None:
	"""Protect editable roots and immutable version files from generic File APIs."""
	if getattr(doc.flags, "afaa_bundle_internal", False) or frappe.flags.afaa_bundle_internal:
		return
	if doc.attached_to_doctype == "AI Skill Bundle Version":
		raise frappe.PermissionError(_("Immutable AI Skill bundle files cannot be changed."))

	candidate = doc.name if doc.name and doc.is_folder else None
	if not candidate and doc.folder and doc.file_name:
		candidate = f"{doc.folder}/{doc.file_name}"
	prefix = f"{BUNDLE_ROOT_PARENT}/"
	if not candidate or not candidate.startswith(prefix):
		return

	remainder = candidate.removeprefix(prefix)
	skill_key, separator, relative_path = remainder.partition("/")
	if not skill_key:
		raise frappe.PermissionError(_("The managed AI Skill bundle path is invalid."))
	if (doc.attached_to_doctype, doc.attached_to_name) != ("AI Skill", skill_key):
		raise frappe.PermissionError(_("AI Skill bundle resources must remain attached to their skill."))
	if not doc.is_private:
		frappe.throw(_("AI Skill bundle resources must be private."), frappe.ValidationError)
	if not doc.is_folder and doc.is_remote_file:
		frappe.throw(_("Remote files are not supported in AI Skill bundles."), frappe.ValidationError)
	if separator:
		try:
			validate_relative_path_limits(relative_path)
		except ValueError as error:
			raise frappe.ValidationError(str(error)) from None
	lock_skill(skill_key)
	if not frappe.get_doc("AI Skill", skill_key).has_permission("write"):
		raise frappe.PermissionError(_("You are not permitted to change this AI Skill bundle."))


def protect_bundle_file_delete(doc, _method=None) -> None:
	validate_bundle_file_record(doc)


def has_bundle_file_permission(doc, ptype=None, user=None, debug=False) -> bool:
	"""Force bundle mutations through owner-aware APIs instead of generic File endpoints."""
	if frappe.flags.afaa_bundle_internal:
		return True
	if ptype not in {"create", "write", "delete", "share", "submit"}:
		return True
	if doc.attached_to_doctype == "AI Skill Bundle Version":
		return False
	candidate = doc.name if doc.name and doc.is_folder else None
	if not candidate and doc.folder and doc.file_name:
		candidate = f"{doc.folder}/{doc.file_name}"
	return not (candidate and candidate.startswith(f"{BUNDLE_ROOT_PARENT}/"))


@contextmanager
def internal_bundle_file_changes():
	previous = frappe.flags.afaa_bundle_internal
	frappe.flags.afaa_bundle_internal = True
	try:
		yield
	finally:
		frappe.flags.afaa_bundle_internal = previous


def _detach_file_record(file_doc) -> None:
	for fieldname in ("attached_to_doctype", "attached_to_name", "attached_to_field"):
		if file_doc.get(fieldname):
			file_doc.db_set(fieldname, None, update_modified=False)
			file_doc.set(fieldname, None)


def _create_private_file(
	logical_name: str,
	content: bytes,
	*,
	folder: str | None = None,
	attached_to_doctype: str | None = None,
	attached_to_name: str | None = None,
	attached_to_field: str | None = None,
):
	# Frappe classifies an empty ``content`` value as remote. A unique staging body
	# creates a rollback-aware local File; it is truncated before publication.
	# Bundle integrity always uses the manifest SHA-256, not File.content_hash.
	storage_content = content or os.urandom(32)
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{frappe.generate_hash(length=10)}-{logical_name}",
			"folder": folder,
			"content": storage_content,
			"is_private": 1,
			"attached_to_doctype": attached_to_doctype,
			"attached_to_name": attached_to_name,
			"attached_to_field": attached_to_field,
		}
	)
	file_doc.flags.afaa_bundle_internal = True
	file_doc.insert(ignore_permissions=True)
	if not content:
		flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
		descriptor = os.open(file_doc.get_full_path(), flags)
		try:
			os.fsync(descriptor)
		finally:
			os.close(descriptor)
		file_doc.db_set("file_size", 0, update_modified=False)
	if file_doc.file_name != logical_name:
		file_doc.db_set("file_name", logical_name, update_modified=False)
	return file_doc


def _read_regular_private_file(file_doc, limits: SkillBundleLimits) -> bytes:
	if file_doc.is_folder or not file_doc.is_private or file_doc.is_remote_file:
		raise frappe.ValidationError(_("AI Skill bundles support only private local regular files."))
	try:
		path = file_doc.get_full_path()
		private_root = os.path.realpath(frappe.get_site_path("private", "files"))
		real_path = os.path.realpath(path)
		if os.path.commonpath((private_root, real_path)) != private_root:
			raise OSError
		flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
		descriptor = os.open(path, flags)
	except OSError, ValueError:
		raise frappe.ValidationError(_("An AI Skill bundle file is missing or unreadable.")) from None
	try:
		metadata = os.fstat(descriptor)
		if not stat.S_ISREG(metadata.st_mode):
			raise frappe.ValidationError(_("AI Skill bundles support only regular files."))
		if metadata.st_size > limits.max_file_bytes:
			raise frappe.ValidationError(
				_("Bundle file exceeds afaa_skill_bundle_max_file_bytes ({0}).").format(limits.max_file_bytes)
			)
		with os.fdopen(descriptor, "rb", closefd=False) as stream:
			content = stream.read(limits.max_file_bytes + 1)
	finally:
		os.close(descriptor)
	if len(content) > limits.max_file_bytes:
		raise frappe.ValidationError(
			_("Bundle file exceeds afaa_skill_bundle_max_file_bytes ({0}).").format(limits.max_file_bytes)
		)
	return content


def inspect_editable_bundle(skill_name: str) -> tuple[list[str], list[EditableBundleFile]]:
	"""Read a complete, validated editable tree. Callers lock the skill for a consistent view."""
	limits = get_skill_bundle_limits()
	root = ensure_skill_bundle_root(skill_name)
	folders: list[str] = []
	files: list[EditableBundleFile] = []
	seen_records = {root}
	seen_paths: set[str] = set()
	queue = [(root, "")]

	while queue:
		parent, parent_path = queue.pop(0)
		children = frappe.get_all(
			"File",
			filters={"folder": parent},
			fields=[
				"name",
				"file_name",
				"file_url",
				"is_folder",
				"is_private",
				"attached_to_doctype",
				"attached_to_name",
			],
			order_by="name asc",
		)
		for child in children:
			if child.name in seen_records:
				raise frappe.ValidationError(_("AI Skill bundle folder cycle detected."))
			seen_records.add(child.name)
			path = f"{parent_path}/{child.file_name}".lstrip("/")
			try:
				path = validate_relative_path_limits(path, limits)
			except ValueError as error:
				raise frappe.ValidationError(str(error)) from None
			if path in seen_paths:
				raise frappe.ValidationError(_("AI Skill bundle contains a duplicate canonical path."))
			seen_paths.add(path)
			if (child.attached_to_doctype, child.attached_to_name) != ("AI Skill", skill_name):
				raise frappe.ValidationError(_("AI Skill bundle contains a resource outside its owner."))
			if child.is_folder:
				expected_name = f"{parent}/{child.file_name}"
				if child.name != expected_name:
					raise frappe.ValidationError(_("AI Skill bundle contains an invalid folder record."))
				folders.append(path)
				queue.append((child.name, path))
				continue
			if len(files) >= limits.max_files:
				raise frappe.ValidationError(
					_("Bundle exceeds afaa_skill_bundle_max_files ({0}).").format(limits.max_files)
				)
			file_doc = frappe.get_doc("File", child.name)
			content = _read_regular_private_file(file_doc, limits)
			files.append(EditableBundleFile(child.name, path, content))

	associated_records = set(
		frappe.get_all(
			"File",
			filters={"attached_to_doctype": "AI Skill", "attached_to_name": skill_name},
			pluck="name",
		)
	)
	if associated_records != seen_records:
		raise frappe.ValidationError(_("AI Skill bundle contains a resource outside its managed root."))

	total_bytes = sum(len(item.content) for item in files)
	if total_bytes > limits.max_bundle_bytes:
		raise frappe.ValidationError(
			_("Bundle exceeds afaa_skill_bundle_max_bundle_bytes ({0}).").format(limits.max_bundle_bytes)
		)
	return sorted(folders), sorted(files, key=lambda item: item.relative_path)


def manifest_for_files(files: list[EditableBundleFile]) -> list[dict[str, Any]]:
	return [
		{
			"path": item.relative_path,
			"byteLength": len(item.content),
			"sha256": hashlib.sha256(item.content).hexdigest(),
			"encoding": "base64",
		}
		for item in sorted(files, key=lambda item: item.relative_path)
	]


def canonical_manifest_json(manifest: list[dict[str, Any]]) -> str:
	return json.dumps({"files": manifest}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def bundle_digest(manifest: list[dict[str, Any]]) -> str:
	return hashlib.sha256(canonical_manifest_json(manifest).encode()).hexdigest()


def bundle_version_id(skill_key: str, digest: str) -> str:
	return hashlib.sha256(f"afaa-skill-bundle-version\0{skill_key}\0{digest}".encode()).hexdigest()


def create_skill_bundle_version(skill_name: str) -> SkillBundleReference:
	"""Publish the current editable tree as an immutable, content-addressed version."""
	skill = frappe.get_doc("AI Skill", skill_name)
	lock_skill(skill.name)
	_folders, source_files = inspect_editable_bundle(skill.name)
	manifest = manifest_for_files(source_files)
	digest = bundle_digest(manifest)
	version_id = bundle_version_id(skill.name, digest)
	if frappe.db.exists("AI Skill Bundle Version", version_id):
		return resolve_skill_bundle_version(version_id, expected_skill_key=skill.name).reference

	stored_files = []
	for source, manifest_item in zip(source_files, manifest, strict=True):
		logical_name = source.relative_path.rsplit("/", 1)[-1]
		file_doc = _create_private_file(
			logical_name,
			source.content,
			attached_to_doctype="AI Skill Bundle Version",
			attached_to_name=version_id,
			attached_to_field="files",
		)
		stored_files.append({**manifest_item, "file": file_doc.name})

	version = frappe.get_doc(
		{
			"doctype": "AI Skill Bundle Version",
			"version_id": version_id,
			"skill": skill.name,
			"bundle_digest": digest,
			"file_count": len(manifest),
			"total_bytes": sum(item["byteLength"] for item in manifest),
			"manifest": canonical_manifest_json(manifest),
			"files": [
				{
					"relative_path": item["path"],
					"byte_length": item["byteLength"],
					"sha256": item["sha256"],
					"encoding": item["encoding"],
					"file": item["file"],
				}
				for item in stored_files
			],
		}
	)
	version.flags.afaa_bundle_internal = True
	version.insert(ignore_permissions=True)
	return SkillBundleReference(
		versionId=version_id,
		digest=digest,
		fileCount=len(manifest),
		totalBytes=sum(item["byteLength"] for item in manifest),
	)


def resolve_skill_bundle_version(
	version_id: str,
	*,
	expected_skill_key: str | None = None,
) -> ResolvedSkillBundle:
	"""Resolve and independently verify one retained version for a trusted server caller."""
	if (
		not isinstance(version_id, str)
		or len(version_id) != 64
		or any(c not in "0123456789abcdef" for c in version_id)
	):
		raise frappe.ValidationError(_("AI Skill bundle version ID is invalid."))
	try:
		version = frappe.get_doc("AI Skill Bundle Version", version_id)
	except frappe.DoesNotExistError:
		raise frappe.ValidationError(_("The retained AI Skill bundle version is unavailable.")) from None
	if expected_skill_key and version.skill != expected_skill_key:
		raise frappe.ValidationError(_("The retained AI Skill bundle belongs to another skill."))

	try:
		manifest_object = json.loads(version.manifest)
		if not isinstance(manifest_object, dict) or set(manifest_object) != {"files"}:
			raise ValueError
		manifest = tuple(SkillBundleManifestFile.model_validate(item) for item in manifest_object["files"])
	except TypeError, ValueError:
		raise frappe.ValidationError(_("The retained AI Skill bundle manifest is invalid.")) from None
	if list(manifest) != sorted(manifest, key=lambda item: item.path):
		raise frappe.ValidationError(_("The retained AI Skill bundle manifest is not canonical."))
	paths = [item.path for item in manifest]
	if len(paths) != len(set(paths)):
		raise frappe.ValidationError(_("The retained AI Skill bundle has duplicate canonical paths."))

	limits = get_skill_bundle_limits()
	if len(manifest) > limits.max_files:
		raise frappe.ValidationError(_("Retained bundle exceeds afaa_skill_bundle_max_files."))
	for item in manifest:
		validate_relative_path_limits(item.path, limits)
		if item.byte_length > limits.max_file_bytes:
			raise frappe.ValidationError(_("Retained bundle exceeds afaa_skill_bundle_max_file_bytes."))
	manifest_total_bytes = sum(item.byte_length for item in manifest)
	if manifest_total_bytes > limits.max_bundle_bytes:
		raise frappe.ValidationError(_("Retained bundle exceeds afaa_skill_bundle_max_bundle_bytes."))
	if version.file_count != len(manifest) or version.total_bytes != manifest_total_bytes:
		raise frappe.ValidationError(_("The retained AI Skill bundle size metadata is invalid."))
	manifest_values = [item.model_dump(mode="json", by_alias=True) for item in manifest]
	digest = bundle_digest(manifest_values)
	if version.bundle_digest != digest or version_id != bundle_version_id(version.skill, digest):
		raise frappe.ValidationError(_("The retained AI Skill bundle digest is invalid."))

	rows = sorted(version.files, key=lambda item: item.relative_path)
	if len(rows) != len(manifest):
		raise frappe.ValidationError(_("The retained AI Skill bundle file map is invalid."))
	runtime_files = []
	for item, row in zip(manifest, rows, strict=True):
		if (
			row.relative_path != item.path
			or row.byte_length != item.byte_length
			or row.sha256 != item.sha256
			or row.encoding != item.encoding
		):
			raise frappe.ValidationError(_("The retained AI Skill bundle file map is invalid."))
		try:
			file_doc = frappe.get_doc("File", row.file)
		except frappe.DoesNotExistError:
			raise frappe.ValidationError(_("A retained AI Skill bundle file is unavailable.")) from None
		if (file_doc.attached_to_doctype, file_doc.attached_to_name, file_doc.attached_to_field) != (
			"AI Skill Bundle Version",
			version_id,
			"files",
		) or file_doc.file_name != item.path.rsplit("/", 1)[-1]:
			raise frappe.ValidationError(_("A retained AI Skill bundle file has an invalid binding."))
		content = _read_regular_private_file(file_doc, limits)
		if len(content) != item.byte_length or hashlib.sha256(content).hexdigest() != item.sha256:
			raise frappe.ValidationError(_("A retained AI Skill bundle file failed integrity checks."))
		runtime_files.append(
			SkillBundleRuntimeFile(
				**item.model_dump(mode="python", by_alias=True),
				content=base64.b64encode(content).decode("ascii"),
			)
		)

	return ResolvedSkillBundle(
		versionId=version_id,
		skillKey=version.skill,
		digest=digest,
		manifest=manifest,
		files=tuple(runtime_files),
		fileCount=version.file_count,
		totalBytes=version.total_bytes,
	)


def _validate_bundle_reference_values(
	reference_doctype: str, reference_name: str, reference_key: str
) -> None:
	for label, value in (
		(_("Reference DocType"), reference_doctype),
		(_("Reference Name"), reference_name),
		(_("Reference Key"), reference_key),
	):
		if not isinstance(value, str) or not value or len(value) > 140 or any(ord(c) < 32 for c in value):
			raise frappe.ValidationError(_("{0} is invalid.").format(label))


def _bundle_reference_id(
	version_id: str, *, reference_doctype: str, reference_name: str, reference_key: str
) -> str:
	_validate_bundle_reference_values(reference_doctype, reference_name, reference_key)
	return hashlib.sha256(
		f"{version_id}\0{reference_doctype}\0{reference_name}\0{reference_key}".encode()
	).hexdigest()


def retain_skill_bundle_version(
	version_id: str,
	*,
	reference_doctype: str,
	reference_name: str,
	reference_key: str,
) -> str:
	"""Idempotently retain a version for one trusted external record, such as a Porch thread skill."""
	locked = frappe.db.sql(
		"select name from `tabAI Skill Bundle Version` where name = %s for update", version_id
	)
	if not locked:
		raise frappe.ValidationError(_("The retained AI Skill bundle version is unavailable."))
	resolve_skill_bundle_version(version_id)
	reference_id = _bundle_reference_id(
		version_id,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		reference_key=reference_key,
	)
	if not frappe.db.exists("AI Skill Bundle Reference", reference_id):
		reference = frappe.get_doc(
			{
				"doctype": "AI Skill Bundle Reference",
				"reference_id": reference_id,
				"bundle_version": version_id,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"reference_key": reference_key,
			}
		)
		reference.flags.afaa_bundle_internal = True
		reference.insert(ignore_permissions=True, ignore_if_duplicate=True)
	return reference_id


def release_skill_bundle_version(
	version_id: str,
	*,
	reference_doctype: str,
	reference_name: str,
	reference_key: str,
) -> None:
	reference_id = _bundle_reference_id(
		version_id,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		reference_key=reference_key,
	)
	if frappe.db.exists("AI Skill Bundle Reference", reference_id):
		reference = frappe.get_doc("AI Skill Bundle Reference", reference_id)
		reference.flags.afaa_bundle_internal = True
		reference.delete(ignore_permissions=True)


def _valid_sha256(value: Any) -> bool:
	return (
		isinstance(value, str)
		and len(value) == 64
		and all(character in "0123456789abcdef" for character in value)
	)


def _validate_external_bundle_identity(*, skill_key: str, bundle_digest: str, bundle_reference: str) -> None:
	# This validates the same stable key format used to derive managed bundle roots.
	skill_bundle_root(skill_key)
	if not _valid_sha256(bundle_digest):
		raise frappe.ValidationError(_("AI Skill bundle digest is invalid."))
	if not _valid_sha256(bundle_reference):
		raise frappe.ValidationError(_("AI Skill bundle reference is invalid."))


def _external_runtime_bundle_payload(
	resolved: ResolvedSkillBundle, *, reference: str, include_files: bool
) -> dict[str, Any]:
	"""Translate the immutable storage model into Porch's strict bundle contract."""
	manifest = [
		{
			"path": item.path,
			"byteLength": item.byte_length,
			"sha256": item.sha256,
			"encoding": item.encoding,
			"contentType": None,
		}
		for item in resolved.manifest
	]
	payload = {
		"digest": bundle_digest(manifest),
		"reference": reference,
		"fileCount": resolved.file_count,
		"byteCount": resolved.total_bytes,
	}
	if include_files:
		payload.update(
			{
				"manifest": manifest,
				"files": [
					{"path": item.path, "encoding": item.encoding, "content": item.content}
					for item in resolved.files
				],
			}
		)
	return payload


def create_external_runtime_bundle_version(
	skill_key: str, reference_doctype: str, reference_name: str
) -> dict[str, Any]:
	"""Create and retain one immutable bundle for a trusted external runtime record."""
	skill_bundle_root(skill_key)
	_validate_bundle_reference_values(reference_doctype, reference_name, skill_key)
	version = create_skill_bundle_version(skill_key)
	resolved = resolve_skill_bundle_version(version.version_id, expected_skill_key=skill_key)
	payload = _external_runtime_bundle_payload(resolved, reference=version.version_id, include_files=False)
	retain_skill_bundle_version(
		version.version_id,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		reference_key=skill_key,
	)
	return payload


def _get_external_bundle_reference(
	version_id: str, *, reference_doctype: str, reference_name: str, reference_key: str
):
	reference_id = _bundle_reference_id(
		version_id,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		reference_key=reference_key,
	)
	reference = frappe.db.get_value(
		"AI Skill Bundle Reference",
		reference_id,
		["name", "bundle_version", "reference_doctype", "reference_name", "reference_key"],
		as_dict=True,
	)
	if not reference:
		return None
	if (
		reference.bundle_version,
		reference.reference_doctype,
		reference.reference_name,
		reference.reference_key,
	) != (version_id, reference_doctype, reference_name, reference_key):
		raise frappe.ValidationError(_("The retained AI Skill bundle reference is invalid."))
	return reference


def resolve_external_runtime_bundle_version(
	skill_key: str,
	bundle_digest: str,
	bundle_reference: str,
	reference_doctype: str,
	reference_name: str,
) -> dict[str, Any]:
	"""Resolve exact retained bytes only for the external record that pinned them."""
	_validate_external_bundle_identity(
		skill_key=skill_key,
		bundle_digest=bundle_digest,
		bundle_reference=bundle_reference,
	)
	locked = frappe.db.sql(
		"select name from `tabAI Skill Bundle Version` where name = %s for update",
		bundle_reference,
	)
	if not locked:
		raise frappe.ValidationError(_("The retained AI Skill bundle version is unavailable."))
	if not _get_external_bundle_reference(
		bundle_reference,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		reference_key=skill_key,
	):
		raise frappe.ValidationError(_("The retained AI Skill bundle reference is unavailable."))
	resolved = resolve_skill_bundle_version(bundle_reference, expected_skill_key=skill_key)
	payload = _external_runtime_bundle_payload(resolved, reference=bundle_reference, include_files=True)
	if payload["digest"] != bundle_digest:
		raise frappe.ValidationError(_("The retained AI Skill bundle digest is invalid."))
	return payload


def release_external_runtime_bundle_version(
	skill_key: str,
	bundle_digest: str,
	bundle_reference: str,
	reference_doctype: str,
	reference_name: str,
) -> None:
	"""Idempotently release only the exact external record's retention reference."""
	_validate_external_bundle_identity(
		skill_key=skill_key,
		bundle_digest=bundle_digest,
		bundle_reference=bundle_reference,
	)
	reference = _get_external_bundle_reference(
		bundle_reference,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		reference_key=skill_key,
	)
	if not reference:
		return
	if frappe.db.exists("AI Skill Bundle Version", bundle_reference):
		resolved = resolve_skill_bundle_version(bundle_reference, expected_skill_key=skill_key)
		payload = _external_runtime_bundle_payload(resolved, reference=bundle_reference, include_files=False)
		if payload["digest"] != bundle_digest:
			raise frappe.ValidationError(_("The retained AI Skill bundle digest is invalid."))
	release_skill_bundle_version(
		bundle_reference,
		reference_doctype=reference_doctype,
		reference_name=reference_name,
		reference_key=skill_key,
	)


def _reconciliation_reference(value: Any) -> dict[str, str]:
	if hasattr(value, "as_dict"):
		value = value.as_dict()
	if not isinstance(value, dict) or set(value) != {
		"skillKey",
		"bundleDigest",
		"bundleReference",
		"referenceDoctype",
		"referenceName",
	}:
		raise frappe.ValidationError(_("AI Skill bundle reconciliation input is invalid."))
	reference = {
		"skill_key": value["skillKey"],
		"bundle_digest": value["bundleDigest"],
		"bundle_reference": value["bundleReference"],
		"reference_doctype": value["referenceDoctype"],
		"reference_name": value["referenceName"],
	}
	_validate_external_bundle_identity(
		skill_key=reference["skill_key"],
		bundle_digest=reference["bundle_digest"],
		bundle_reference=reference["bundle_reference"],
	)
	_validate_bundle_reference_values(
		reference["reference_doctype"], reference["reference_name"], reference["skill_key"]
	)
	reference["reference_id"] = _bundle_reference_id(
		reference["bundle_reference"],
		reference_doctype=reference["reference_doctype"],
		reference_name=reference["reference_name"],
		reference_key=reference["skill_key"],
	)
	return reference


def reconcile_external_runtime_bundle_references(
	references: list[dict[str, Any]], dry_run: bool = True
) -> dict[str, int]:
	"""Report and repair retention metadata from Porch's authoritative thread snapshots."""
	if not isinstance(dry_run, bool) or not isinstance(references, (list, tuple)):
		raise frappe.ValidationError(_("AI Skill bundle reconciliation input is invalid."))

	expected = {}
	for raw_reference in references:
		reference = _reconciliation_reference(raw_reference)
		if reference["reference_id"] in expected:
			raise frappe.ValidationError(_("AI Skill bundle reconciliation contains duplicates."))
		expected[reference["reference_id"]] = reference

	actual_rows = frappe.get_all(
		"AI Skill Bundle Reference",
		fields=["name", "bundle_version", "reference_doctype", "reference_name", "reference_key"],
	)
	actual = {row.name: row for row in actual_rows}
	missing_references: set[str] = set()
	missing_versions: set[str] = set()
	inconsistent_references: set[str] = set()
	existing_expected_versions: set[str] = set()
	valid_expected_versions: set[str] = set()
	created_reference_count = 0

	for reference_id, reference in expected.items():
		version_id = reference["bundle_reference"]
		version_exists = bool(frappe.db.exists("AI Skill Bundle Version", version_id))
		if not version_exists:
			missing_versions.add(reference_id)
		else:
			existing_expected_versions.add(version_id)
			try:
				resolved = resolve_skill_bundle_version(version_id, expected_skill_key=reference["skill_key"])
				payload = _external_runtime_bundle_payload(
					resolved, reference=version_id, include_files=False
				)
				if payload["digest"] != reference["bundle_digest"]:
					raise ValueError
			except Exception:
				inconsistent_references.add(reference_id)
			else:
				valid_expected_versions.add(version_id)

		row = actual.get(reference_id)
		if not row:
			missing_references.add(reference_id)
			if not dry_run and version_id in valid_expected_versions:
				retain_skill_bundle_version(
					version_id,
					reference_doctype=reference["reference_doctype"],
					reference_name=reference["reference_name"],
					reference_key=reference["skill_key"],
				)
				created_reference_count += 1
		elif (
			row.bundle_version,
			row.reference_doctype,
			row.reference_name,
			row.reference_key,
		) != (
			version_id,
			reference["reference_doctype"],
			reference["reference_name"],
			reference["skill_key"],
		):
			inconsistent_references.add(reference_id)

	for reference_id, row in actual.items():
		if not frappe.db.exists("AI Skill Bundle Version", row.bundle_version):
			missing_versions.add(reference_id)
		if reference_id not in expected:
			inconsistent_references.add(reference_id)

	eligible_versions = set(
		garbage_collect_skill_bundle_versions(
			older_than_days=30,
			dry_run=dry_run,
			_protected_version_ids=existing_expected_versions,
		)
	)
	deleted_version_count = 0 if dry_run else len(eligible_versions)

	return {
		"missingReferenceCount": len(missing_references),
		"missingVersionCount": len(missing_versions),
		"unreferencedVersionCount": len(eligible_versions),
		"inconsistentReferenceCount": len(inconsistent_references),
		"createdReferenceCount": created_reference_count,
		"deletedVersionCount": deleted_version_count,
	}


def garbage_collect_skill_bundle_versions(
	*,
	older_than_days: int = 30,
	dry_run: bool = True,
	_protected_version_ids: set[str] | None = None,
) -> list[str]:
	"""Delete unreferenced immutable versions only after a locked reference recheck."""
	frappe.only_for("System Manager")
	if older_than_days < 0:
		raise frappe.ValidationError(_("Bundle retention days cannot be negative."))
	protected_version_ids = _protected_version_ids or set()
	cutoff = now_datetime() - timedelta(days=older_than_days)
	candidates = frappe.get_all(
		"AI Skill Bundle Version",
		filters={"creation": ("<=", cutoff)},
		pluck="name",
		order_by="creation asc",
	)
	eligible = []
	for version_id in candidates:
		frappe.db.sql("select name from `tabAI Skill Bundle Version` where name = %s for update", version_id)
		if version_id in protected_version_ids or frappe.db.exists(
			"AI Skill Bundle Reference", {"bundle_version": version_id}
		):
			continue
		eligible.append(version_id)
		if dry_run:
			continue
		version = frappe.get_doc("AI Skill Bundle Version", version_id)
		file_ids = [row.file for row in version.files]
		version.flags.afaa_bundle_internal = True
		with internal_bundle_file_changes():
			version.delete(ignore_permissions=True)
		for file_id in file_ids:
			if frappe.db.exists("File", file_id):
				file_doc = frappe.get_doc("File", file_id)
				_detach_file_record(file_doc)
				file_doc.flags.afaa_bundle_internal = True
				file_doc.delete(ignore_permissions=True)
	return eligible


def initialize_skill_bundle_roots() -> None:
	"""Idempotent migration helper for skills created before bundle support."""
	for skill_name in frappe.get_all("AI Skill", pluck="name", order_by="name asc"):
		ensure_skill_bundle_root(skill_name)


def _ensure_parent_folders(skill_name: str, relative_path: str) -> str:
	root = ensure_skill_bundle_root(skill_name)
	parent_parts = relative_path.split("/")[:-1]
	parent = root
	for index, segment in enumerate(parent_parts):
		parent_path = "/".join(parent_parts[: index + 1])
		validate_relative_path_limits(parent_path)
		validate_managed_folder_path(skill_name, parent_path)
		existing = _find_bundle_member(skill_name, parent_path)
		if existing:
			if not existing.is_folder:
				raise frappe.ValidationError(_("A bundle file and folder cannot share the same path."))
			parent = existing.name
			continue
		folder = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": segment,
				"folder": parent,
				"is_folder": 1,
				"is_private": 1,
				"attached_to_doctype": "AI Skill",
				"attached_to_name": skill_name,
			}
		)
		folder.flags.afaa_bundle_internal = True
		folder.insert(ignore_permissions=True)
		parent = folder.name
	return parent


def _find_bundle_member(skill_name: str, relative_path: str):
	root = skill_bundle_root(skill_name)
	parent_path, _, file_name = relative_path.rpartition("/")
	folder = f"{root}/{parent_path}" if parent_path else root
	matches = frappe.get_all(
		"File",
		filters={"folder": folder, "file_name": file_name},
		fields=["name", "is_folder", "attached_to_doctype", "attached_to_name"],
		limit=2,
	)
	if not matches:
		return None
	if len(matches) != 1:
		raise frappe.ValidationError(_("AI Skill bundle contains a duplicate canonical path."))
	member = matches[0]
	if (member.attached_to_doctype, member.attached_to_name) != ("AI Skill", skill_name):
		raise frappe.ValidationError(_("AI Skill bundle resource has an invalid owner."))
	return frappe.get_doc("File", member.name)


@frappe.whitelist(methods=["GET"])
def get_bundle_tree(skill_name: str) -> dict[str, Any]:
	require_skill_permission(skill_name, "read")
	lock_skill(skill_name)
	folders, files = inspect_editable_bundle(skill_name)
	return {
		"root": skill_bundle_root(skill_name),
		"items": [{"path": path, "type": "folder", "size": None, "isText": False} for path in folders]
		+ [
			{
				"path": item.relative_path,
				"type": "file",
				"size": len(item.content),
				"isText": is_text_content(item.content),
			}
			for item in files
		],
		"limits": {
			"maxFiles": get_skill_bundle_limits().max_files,
			"maxDepth": get_skill_bundle_limits().max_depth,
			"maxPathLength": get_skill_bundle_limits().max_path_length,
			"maxFileBytes": get_skill_bundle_limits().max_file_bytes,
			"maxBundleBytes": get_skill_bundle_limits().max_bundle_bytes,
		},
	}


@frappe.whitelist(methods=["GET"])
def read_bundle_file(skill_name: str, relative_path: str) -> dict[str, Any]:
	require_skill_permission(skill_name, "read")
	try:
		relative_path = validate_relative_path_limits(relative_path)
	except ValueError as error:
		raise frappe.ValidationError(str(error)) from None
	lock_skill(skill_name)
	member = _find_bundle_member(skill_name, relative_path)
	if not member or member.is_folder:
		raise frappe.DoesNotExistError(_("Bundle file was not found."))
	content = _read_regular_private_file(member, get_skill_bundle_limits())
	text = content.decode("utf-8") if is_text_content(content) else None
	return {
		"path": relative_path,
		"byteLength": len(content),
		"isText": text is not None,
		"text": text,
		"encoding": "base64",
		"content": base64.b64encode(content).decode("ascii"),
	}


@frappe.whitelist(methods=["POST"])
def create_bundle_folder(skill_name: str, relative_path: str) -> dict[str, Any]:
	require_skill_permission(skill_name, "write")
	try:
		relative_path = validate_relative_path_limits(relative_path)
	except ValueError as error:
		raise frappe.ValidationError(str(error)) from None
	validate_managed_folder_path(skill_name, relative_path)
	lock_skill(skill_name)
	inspect_editable_bundle(skill_name)
	parent = _ensure_parent_folders(skill_name, relative_path)
	existing = _find_bundle_member(skill_name, relative_path)
	if existing and not existing.is_folder:
		raise frappe.ValidationError(_("A bundle file and folder cannot share the same path."))
	if not existing:
		folder = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": relative_path.rsplit("/", 1)[-1],
				"folder": parent,
				"is_folder": 1,
				"is_private": 1,
				"attached_to_doctype": "AI Skill",
				"attached_to_name": skill_name,
			}
		)
		folder.flags.afaa_bundle_internal = True
		folder.insert(ignore_permissions=True)
	inspect_editable_bundle(skill_name)
	return {"path": relative_path, "type": "folder"}


@frappe.whitelist(methods=["POST"])
def upload_bundle_file(
	skill_name: str,
	relative_path: str,
	content: str,
	encoding: Literal["base64", "utf-8"] = "base64",
) -> dict[str, Any]:
	require_skill_permission(skill_name, "write")
	try:
		relative_path = validate_relative_path_limits(relative_path)
	except ValueError as error:
		raise frappe.ValidationError(str(error)) from None
	if encoding not in {"base64", "utf-8"} or not isinstance(content, str):
		raise frappe.ValidationError(_("Bundle file content encoding is invalid."))
	try:
		decoded = base64.b64decode(content, validate=True) if encoding == "base64" else content.encode()
	except AttributeError, binascii.Error, UnicodeEncodeError, ValueError:
		raise frappe.ValidationError(_("Bundle file content encoding is invalid.")) from None
	limits = get_skill_bundle_limits()
	if len(decoded) > limits.max_file_bytes:
		raise frappe.ValidationError(
			_("Bundle file exceeds afaa_skill_bundle_max_file_bytes ({0}).").format(limits.max_file_bytes)
		)

	lock_skill(skill_name)
	_folders, current_files = inspect_editable_bundle(skill_name)
	existing = _find_bundle_member(skill_name, relative_path)
	if existing and existing.is_folder:
		raise frappe.ValidationError(_("A bundle file and folder cannot share the same path."))
	existing_bytes = next(
		(len(item.content) for item in current_files if item.relative_path == relative_path), 0
	)
	candidate_count = len(current_files) + (0 if existing else 1)
	candidate_bytes = sum(len(item.content) for item in current_files) - existing_bytes + len(decoded)
	if candidate_count > limits.max_files:
		raise frappe.ValidationError(
			_("Bundle exceeds afaa_skill_bundle_max_files ({0}).").format(limits.max_files)
		)
	if candidate_bytes > limits.max_bundle_bytes:
		raise frappe.ValidationError(
			_("Bundle exceeds afaa_skill_bundle_max_bundle_bytes ({0}).").format(limits.max_bundle_bytes)
		)
	parent = _ensure_parent_folders(skill_name, relative_path)
	logical_name = relative_path.rsplit("/", 1)[-1]
	file_doc = _create_private_file(logical_name, decoded, folder=parent)
	file_doc.db_set("attached_to_doctype", "AI Skill", update_modified=False)
	file_doc.db_set("attached_to_name", skill_name, update_modified=False)
	if existing:
		_detach_file_record(existing)
		existing.flags.afaa_bundle_internal = True
		existing.delete(ignore_permissions=True)
	inspect_editable_bundle(skill_name)
	return {"path": relative_path, "type": "file", "size": len(decoded)}


def _delete_bundle_tree(member) -> None:
	if member.is_folder:
		for child_name in frappe.get_all("File", filters={"folder": member.name}, pluck="name"):
			_delete_bundle_tree(frappe.get_doc("File", child_name))
	_detach_file_record(member)
	member.flags.afaa_bundle_internal = True
	member.delete(ignore_permissions=True)


def _set_bundle_tree_owner(member, skill_name: str) -> None:
	member.db_set("attached_to_name", skill_name, update_modified=False)
	if member.is_folder:
		for child_name in frappe.get_all("File", filters={"folder": member.name}, pluck="name"):
			_set_bundle_tree_owner(frappe.get_doc("File", child_name), skill_name)


@frappe.whitelist(methods=["POST"])
def delete_bundle_member(skill_name: str, relative_path: str) -> None:
	require_skill_permission(skill_name, "write")
	try:
		relative_path = validate_relative_path_limits(relative_path)
	except ValueError as error:
		raise frappe.ValidationError(str(error)) from None
	lock_skill(skill_name)
	member = _find_bundle_member(skill_name, relative_path)
	if not member:
		raise frappe.DoesNotExistError(_("Bundle resource was not found."))
	_delete_bundle_tree(member)


@frappe.whitelist(methods=["POST"])
def move_bundle_member(
	skill_name: str,
	source_path: str,
	target_path: str,
	*,
	target_skill_name: str | None = None,
) -> dict[str, Any]:
	target_skill_name = target_skill_name or skill_name
	require_skill_permission(skill_name, "write")
	require_skill_permission(target_skill_name, "write")
	try:
		source_path = validate_relative_path_limits(source_path)
		target_path = validate_relative_path_limits(target_path)
	except ValueError as error:
		raise frappe.ValidationError(str(error)) from None
	for name in sorted({skill_name, target_skill_name}):
		lock_skill(name)
	source = _find_bundle_member(skill_name, source_path)
	if not source:
		raise frappe.DoesNotExistError(_("Bundle resource was not found."))
	if _find_bundle_member(target_skill_name, target_path):
		raise frappe.ValidationError(_("A bundle resource already exists at the target path."))
	if source.is_folder and target_skill_name == skill_name and target_path.startswith(f"{source_path}/"):
		raise frappe.ValidationError(_("A bundle folder cannot be moved inside itself."))

	source_folders, source_files = inspect_editable_bundle(skill_name)
	moving_paths = [
		path
		for path in source_folders + [item.relative_path for item in source_files]
		if path == source_path or path.startswith(f"{source_path}/")
	]
	for path in moving_paths:
		mapped_path = f"{target_path}{path.removeprefix(source_path)}"
		validate_relative_path_limits(mapped_path)
		if path in source_folders:
			validate_managed_folder_path(target_skill_name, mapped_path)
	if target_skill_name != skill_name:
		_target_folders, target_files = inspect_editable_bundle(target_skill_name)
		moving_files = [
			item
			for item in source_files
			if item.relative_path == source_path or item.relative_path.startswith(f"{source_path}/")
		]
		limits = get_skill_bundle_limits()
		if len(target_files) + len(moving_files) > limits.max_files:
			raise frappe.ValidationError(
				_("Bundle exceeds afaa_skill_bundle_max_files ({0}).").format(limits.max_files)
			)
		if sum(len(item.content) for item in target_files + moving_files) > limits.max_bundle_bytes:
			raise frappe.ValidationError(
				_("Bundle exceeds afaa_skill_bundle_max_bundle_bytes ({0}).").format(limits.max_bundle_bytes)
			)

	new_parent = _ensure_parent_folders(target_skill_name, target_path)
	new_name = target_path.rsplit("/", 1)[-1]
	if target_skill_name != skill_name:
		_set_bundle_tree_owner(source, target_skill_name)
	source.attached_to_name = target_skill_name
	source.folder = new_parent
	source.file_name = new_name
	source.flags.afaa_bundle_internal = True
	if source.is_folder:
		from frappe.model.rename_doc import rename_doc

		old_name = source.name
		source.save(ignore_permissions=True)
		with internal_bundle_file_changes():
			rename_doc(
				"File",
				old_name,
				f"{new_parent}/{new_name}",
				force=True,
				ignore_permissions=True,
			)
	else:
		source.save(ignore_permissions=True)
	inspect_editable_bundle(skill_name)
	if target_skill_name != skill_name:
		inspect_editable_bundle(target_skill_name)
	return {"path": target_path, "type": "folder" if source.is_folder else "file"}


def is_text_content(content: bytes) -> bool:
	if b"\x00" in content:
		return False
	try:
		content.decode("utf-8")
		return True
	except UnicodeDecodeError:
		return False


# Compatibility-friendly concise names for companion apps.
create_bundle_version = create_skill_bundle_version
resolve_bundle_version = resolve_skill_bundle_version
retain_bundle_version = retain_skill_bundle_version
release_bundle_version = release_skill_bundle_version
