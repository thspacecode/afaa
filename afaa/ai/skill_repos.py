# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

"""AI Skill Repositories: discover and import Git-hosted skills from GitHub.

The module keeps AFAA independent of any credential-broker app. Private
repository access is resolved through the documented extension hook::

    afaa_skill_repo_github_credentials = ["app.module.handler"]

Each registered callable receives ``(owner, repository)`` and returns one of:

    None                                   # unknown: fall back to anonymous access
    {"status": "public", ...}              # reachable anonymously
    {"status": "private", "token": ...}    # short-lived ``contents:read`` token
    {"status": "private", "reason": ...}   # known private, no usable access

Tokens are used for the current request only and are never persisted, logged,
or echoed in error messages.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import frappe
import requests
from frappe import _
from frappe.utils import cint, now_datetime

from afaa.ai.skill_bundles import (
	create_skill_bundle_version,
	delete_bundle_member,
	get_skill_bundle_limits,
	inspect_editable_bundle,
	upload_bundle_file,
)
from afaa.utils.data import validate_key

GITHUB_WEB_HOST = "github.com"
GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
SKILL_FILE_NAME = "SKILL.md"
MAX_TREE_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = (5, 20)
GITHUB_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?$")
GITHUB_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
SLUG_STRIP_PATTERN = re.compile(r"[^a-z0-9_-]+")
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
FRONTMATTER_FIELD_LIMIT = 500
SKILL_NAME_LIMIT = 140


class SkillRepoHTTPError(Exception):
	"""Provider-neutral HTTP failure mapped from a GitHub status code."""

	def __init__(self, status_code: int):
		self.status_code = status_code
		super().__init__(self.user_message)

	@property
	def user_message(self) -> str:
		if self.status_code in {401, 403}:
			return _(
				"GitHub refused access to this repository. If it is private, connect a GitHub"
				" integration with contents read access to this repository and try again."
			)
		if self.status_code == 404:
			return _("The GitHub repository, branch, folder or pinned commit was not found.")
		if self.status_code == 429:
			return _("GitHub rate limit was reached. Wait a moment and try again.")
		if self.status_code >= 500:
			return _("GitHub is currently unavailable. Try again later.")
		return _("GitHub rejected the request with status {0}.").format(self.status_code)


@dataclass(frozen=True)
class ParsedRepoUrl:
	owner: str
	repository: str
	ref: str | None


@dataclass(frozen=True)
class RepoCredential:
	token: str | None
	default_branch: str | None


@dataclass(frozen=True)
class TreeEntry:
	path: str
	type: str
	sha: str
	size: int


@dataclass(frozen=True)
class DiscoveredSkill:
	folder_name: str
	folder_path: str
	skill_md_sha: str
	skill_md_size: int


def parse_repo_url(value: str | None) -> ParsedRepoUrl:
	"""Parse and validate one plain GitHub HTTPS repository URL."""
	parsed = urlsplit((value or "").strip())
	if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
		raise frappe.ValidationError(
			_("Repository URL must be a plain https URL without credentials, query or fragment.")
		)
	if (parsed.hostname or "").lower() != GITHUB_WEB_HOST:
		raise frappe.ValidationError(_("Only {0} repository URLs are supported.").format(GITHUB_WEB_HOST))
	segments = [segment for segment in parsed.path.split("/") if segment]
	if len(segments) < 2:
		raise frappe.ValidationError(_("Repository URL must point to an owner and a repository."))
	owner, repository = segments[0], segments[1]
	if not GITHUB_OWNER_PATTERN.fullmatch(owner):
		raise frappe.ValidationError(_("Repository URL has an invalid GitHub owner."))
	repository = repository.removesuffix(".git")
	if not GITHUB_REPOSITORY_PATTERN.fullmatch(repository):
		raise frappe.ValidationError(_("Repository URL has an invalid GitHub repository name."))
	ref = None
	if len(segments) > 2:
		if segments[2] != "tree" or len(segments) < 4:
			raise frappe.ValidationError(
				_('Repository URL may only add a "/tree/<branch>" part after the repository.')
			)
		ref = "/".join(segments[3:])
		if not ref or any(segment in {"", ".", ".."} for segment in ref.split("/")):
			raise frappe.ValidationError(_("Repository URL has an invalid branch reference."))
	return ParsedRepoUrl(owner=owner, repository=repository, ref=ref)


def normalize_skills_folder(value: str | None) -> str:
	"""Normalize and validate the repo-relative skills folder path."""
	folder = (value or "").strip().strip("/")
	if not folder:
		raise frappe.ValidationError(_("Skills Folder is required."))
	if folder.startswith("/") or "\\" in folder or folder.startswith("~"):
		raise frappe.ValidationError(_("Skills Folder must be a relative repository path."))
	segments = folder.split("/")
	if any(segment in {"", ".", ".."} for segment in segments):
		raise frappe.ValidationError(_("Skills Folder cannot contain empty, '.' or '..' segments."))
	if CONTROL_CHARACTER_PATTERN.search(folder) or len(folder) > 200:
		raise frappe.ValidationError(_("Skills Folder is invalid or too long."))
	return folder


def slugify_skill_segment(value: str) -> str:
	"""Derive the lowercase slug segment used inside prefixed skill keys."""
	return SLUG_STRIP_PATTERN.sub("-", (value or "").strip().lower()).strip("-")


def derive_skill_key(repo_slug: str, folder_name: str) -> str:
	return f"{repo_slug}-{slugify_skill_segment(folder_name)}"


def split_skill_markdown(content: bytes) -> tuple[str | None, str | None, str] | None:
	"""Split SKILL.md bytes into (name, description, instructions).

	Returns ``None`` when the file is not valid UTF-8 text. A missing or
	non-dict frontmatter degrades to name/description ``None``.
	"""
	try:
		text = content.decode("utf-8")
	except UnicodeDecodeError:
		return None

	name: str | None = None
	description: str | None = None
	body = text
	if text.startswith("---"):
		lines = text.splitlines(keepends=True)
		closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
		if closing is not None:
			frontmatter = _parse_simple_frontmatter("".join(lines[1:closing]))
			body = "".join(lines[closing + 1 :])
			name = frontmatter.get("name")
			description = frontmatter.get("description")
	return name, description, body.lstrip("\r\n")


def _parse_simple_frontmatter(text: str) -> dict[str, str]:
	"""Parse the flat ``key: value`` frontmatter subset used by SKILL.md files."""
	fields: dict[str, str] = {}
	for line in text.splitlines():
		line = line.strip()
		if not line or line.startswith("#") or ":" not in line:
			continue
		key, _, value = line.partition(":")
		key = key.strip().lower()
		value = value.strip()
		if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
			value = value[1:-1]
		if key and key not in fields and len(value) <= FRONTMATTER_FIELD_LIMIT:
			fields[key] = value
	return fields


def github_request(
	url: str, *, token: str | None = None, max_bytes: int = MAX_TREE_RESPONSE_BYTES
) -> dict[str, Any]:
	"""Perform one bounded GET against the GitHub REST API.

	Responses are size-capped and errors are mapped to provider-neutral
	messages that never include tokens or raw payloads.
	"""
	headers = {
		"Accept": "application/vnd.github+json",
		"X-GitHub-Api-Version": GITHUB_API_VERSION,
		"User-Agent": "AFAA",
	}
	if token:
		headers["Authorization"] = f"Bearer {token}"
	try:
		response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, stream=True)
	except requests.RequestException as error:
		raise SkillRepoHTTPError(0) from error
	try:
		if cint(response.headers.get("Content-Length")) > max_bytes:
			raise frappe.ValidationError(_("GitHub returned an oversized response."))
		body = bytearray()
		for chunk in response.iter_content(chunk_size=16_384):
			body.extend(chunk)
			if len(body) > max_bytes:
				raise frappe.ValidationError(_("GitHub returned an oversized response."))
		if response.status_code != 200:
			raise SkillRepoHTTPError(response.status_code)
		try:
			payload = json.loads(body)
		except (json.JSONDecodeError, UnicodeDecodeError) as error:
			raise frappe.ValidationError(_("GitHub returned an invalid response.")) from error
		if not isinstance(payload, dict):
			raise frappe.ValidationError(_("GitHub returned an invalid response."))
		return payload
	finally:
		response.close()


def resolve_repo_credentials(owner: str, repository: str) -> RepoCredential:
	"""Resolve access for one repository through registered credential hooks."""
	for hook_path in frappe.get_hooks("afaa_skill_repo_github_credentials"):
		try:
			result = frappe.get_attr(hook_path)(owner, repository)
		except Exception:
			frappe.log_error(
				title=_("AFAA skill repository credential hook failed"),
				message=frappe.get_traceback(),
			)
			continue
		if result is None:
			continue
		if not isinstance(result, dict):
			continue
		status = result.get("status")
		token = result.get("token")
		default_branch = result.get("default_branch")
		if status not in {"public", "private"}:
			continue
		if token is not None and not isinstance(token, str):
			continue
		default_branch = default_branch.strip() if isinstance(default_branch, str) else ""
		if status == "public":
			return RepoCredential(token=None, default_branch=default_branch or None)
		if token:
			return RepoCredential(token=token, default_branch=default_branch or None)
		reason = result.get("reason")
		raise frappe.ValidationError(
			reason.strip()
			if isinstance(reason, str) and reason.strip()
			else _(
				"This GitHub repository is private and no integration can currently read it."
				" Connect a GitHub integration with contents read access and try again."
			)
		)
	return RepoCredential(token=None, default_branch=None)


def _repo_api_url(parsed: ParsedRepoUrl, suffix: str = "") -> str:
	return f"{GITHUB_API_URL}/repos/{parsed.owner}/{parsed.repository}{suffix}"


def _require_sha(value: Any) -> str:
	if not isinstance(value, str) or not GIT_SHA_PATTERN.fullmatch(value):
		raise frappe.ValidationError(_("GitHub returned invalid commit metadata."))
	return value


def resolve_commit(parsed: ParsedRepoUrl, credential: RepoCredential, ref: str) -> tuple[str, str]:
	"""Resolve a branch/tag to its exact commit and tree SHAs."""
	payload = github_request(f"{_repo_api_url(parsed)}/commits/{quote(ref, safe='')}", token=credential.token)
	commit = payload.get("commit")
	tree_sha = commit.get("tree", {}).get("sha") if isinstance(commit, dict) else None
	return _require_sha(payload.get("sha")), _require_sha(tree_sha)


def commit_tree_sha(parsed: ParsedRepoUrl, credential: RepoCredential, commit_sha: str) -> str:
	"""Resolve the tree SHA of one exact commit (used for pinned syncs)."""
	payload = github_request(
		f"{_repo_api_url(parsed)}/git/commits/{quote(commit_sha, safe='')}", token=credential.token
	)
	tree = payload.get("tree")
	tree_sha = tree.get("sha") if isinstance(tree, dict) else None
	return _require_sha(tree_sha)


def fetch_tree_entries(
	parsed: ParsedRepoUrl, credential: RepoCredential, tree_sha: str
) -> tuple[TreeEntry, ...]:
	"""List the complete repository tree addressed by one tree SHA."""
	payload = github_request(
		f"{_repo_api_url(parsed)}/git/trees/{quote(tree_sha, safe='')}?recursive=1",
		token=credential.token,
	)
	if payload.get("truncated"):
		raise frappe.ValidationError(_("The GitHub repository tree is too large to inspect."))
	raw_entries = payload.get("tree")
	if not isinstance(raw_entries, list):
		raise frappe.ValidationError(_("GitHub returned an invalid repository tree."))
	entries: list[TreeEntry] = []
	seen_paths: set[str] = set()
	for item in raw_entries:
		if not isinstance(item, dict):
			raise frappe.ValidationError(_("GitHub returned an invalid repository tree."))
		path = item.get("path")
		entry_type = item.get("type")
		sha = item.get("sha")
		if not isinstance(path, str) or not path or path in seen_paths:
			raise frappe.ValidationError(_("GitHub returned an invalid repository tree."))
		seen_paths.add(path)
		if entry_type not in {"blob", "tree", "commit"} or not isinstance(sha, str):
			raise frappe.ValidationError(_("GitHub returned an invalid repository tree."))
		size = cint(item.get("size")) if entry_type == "blob" else 0
		if size < 0:
			raise frappe.ValidationError(_("GitHub returned an invalid repository tree."))
		entries.append(TreeEntry(path=path, type=entry_type, sha=sha, size=size))
	return tuple(sorted(entries, key=lambda entry: entry.path))


def fetch_blob(
	parsed: ParsedRepoUrl,
	credential: RepoCredential,
	blob_sha: str,
	expected_size: int,
	limits=None,
) -> bytes:
	"""Download one immutable blob, enforcing the configured per-file limit."""
	limits = limits or get_skill_bundle_limits()
	if expected_size > limits.max_file_bytes:
		raise frappe.ValidationError(
			_("Bundle file exceeds afaa_skill_bundle_max_file_bytes ({0}).").format(limits.max_file_bytes)
		)
	response_cap = max(8_192, expected_size * 4 // 3 + 8_192)
	payload = github_request(
		f"{_repo_api_url(parsed)}/git/blobs/{quote(blob_sha, safe='')}",
		token=credential.token,
		max_bytes=response_cap,
	)
	if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
		raise frappe.ValidationError(_("GitHub returned an invalid file blob."))
	try:
		content = base64.b64decode(payload["content"])
	except (binascii.Error, ValueError) as error:
		raise frappe.ValidationError(_("GitHub returned an invalid file blob.")) from error
	if cint(payload.get("size")) != len(content) or len(content) > limits.max_file_bytes:
		raise frappe.ValidationError(
			_("Bundle file exceeds afaa_skill_bundle_max_file_bytes ({0}).").format(limits.max_file_bytes)
		)
	return content


def discover_skills(entries: tuple[TreeEntry, ...], skills_folder: str) -> tuple[DiscoveredSkill, ...]:
	"""Find direct child directories of the skills folder that contain SKILL.md."""
	prefix = f"{skills_folder}/"
	directories = {
		entry.path[len(prefix) :]: entry
		for entry in entries
		if entry.type == "tree" and entry.path.startswith(prefix) and "/" not in entry.path[len(prefix) :]
	}
	skill_md_by_folder: dict[str, TreeEntry] = {}
	for entry in entries:
		if entry.type != "blob" or not entry.path.startswith(prefix):
			continue
		remainder = entry.path[len(prefix) :]
		folder, separator, file_name = remainder.partition("/")
		if separator and folder and "/" not in folder and file_name == SKILL_FILE_NAME:
			skill_md_by_folder[folder] = entry
	discovered = []
	for folder_name in sorted(directories):
		skill_md = skill_md_by_folder.get(folder_name)
		if not skill_md or skill_md.path != f"{prefix}{folder_name}/{SKILL_FILE_NAME}":
			continue
		discovered.append(
			DiscoveredSkill(
				folder_name=folder_name,
				folder_path=f"{prefix}{folder_name}",
				skill_md_sha=skill_md.sha,
				skill_md_size=skill_md.size,
			)
		)
	return tuple(discovered)


def _display_name(folder_name: str, frontmatter_name: str | None) -> str:
	name = (frontmatter_name or "").strip()
	if name and len(name) <= SKILL_NAME_LIMIT and not CONTROL_CHARACTER_PATTERN.search(name):
		return name
	return folder_name


def _get_repo_doc(name: str, permission_type: str):
	try:
		doc = frappe.get_doc("AI Skill Repo", name)
	except frappe.DoesNotExistError:
		raise frappe.PermissionError(_("You are not permitted to access this AI Skill Repo.")) from None
	if not doc.has_permission(permission_type):
		raise frappe.PermissionError(_("You are not permitted to change this AI Skill Repo."))
	return doc


def _sanitized_error_message(error: BaseException) -> str:
	if isinstance(error, SkillRepoHTTPError):
		return str(error.user_message)
	if isinstance(error, (frappe.ValidationError, frappe.PermissionError, frappe.DoesNotExistError)):
		message = str(error)
		return message if message else _("The skill repository operation failed.")
	return _("The skill repository operation failed unexpectedly.")


def _record_repo_error(doc, fieldname: str, error: BaseException) -> None:
	message = _sanitized_error_message(error)[:500]
	if not isinstance(error, (SkillRepoHTTPError, frappe.ValidationError, frappe.PermissionError)):
		frappe.log_error(
			title=_("AI Skill Repo operation failed"),
			message=frappe.get_traceback(),
		)
	frappe.db.set_value(doc.doctype, doc.name, fieldname, message, update_modified=False)
	if not frappe.in_test:
		frappe.db.commit()


def fetch_repo_skills(doc) -> dict[str, Any]:
	"""Mirror the repository's skills folder into the child table and pin the commit."""
	parsed = parse_repo_url(doc.repo_url)
	credential = resolve_repo_credentials(parsed.owner, parsed.repository)
	limits = get_skill_bundle_limits()

	ref = (doc.branch or "").strip() or credential.default_branch
	if not ref:
		metadata = github_request(_repo_api_url(parsed), token=credential.token)
		ref = str(metadata.get("default_branch") or "").strip()
		if not ref:
			raise frappe.ValidationError(_("GitHub did not report a default branch."))

	commit_sha, tree_sha = resolve_commit(parsed, credential, ref)
	entries = fetch_tree_entries(parsed, credential, tree_sha)
	discovered = discover_skills(entries, doc.skills_folder)

	previous_rows = {row.skill_folder: row for row in doc.skills}
	doc.set("skills", [])
	invalid_keys: list[str] = []
	for skill in discovered:
		name_value: str | None = None
		try:
			markdown = fetch_blob(parsed, credential, skill.skill_md_sha, skill.skill_md_size, limits)
			parsed_markdown = split_skill_markdown(markdown)
			if parsed_markdown is not None:
				name_value = parsed_markdown[0]
		except (SkillRepoHTTPError, frappe.ValidationError):
			name_value = None
		previous = previous_rows.get(skill.folder_name)
		skill_key = derive_skill_key(doc.name, skill.folder_name)
		if not re.fullmatch(r"[a-z][a-z0-9_-]{2,49}", skill_key):
			invalid_keys.append(skill.folder_name)
		doc.append(
			"skills",
			{
				"skill_folder": skill.folder_name,
				"skill_name": _display_name(skill.folder_name, name_value),
				"sync": cint(previous.sync) if previous else 1,
				"skill_key": skill_key,
				"ai_skill": previous.ai_skill if previous else None,
				"synced_commit": previous.synced_commit if previous else None,
			},
		)

	doc.last_fetched_commit = commit_sha
	doc.last_fetched_at = now_datetime()
	doc.last_fetch_error = None
	doc.save()

	return {
		"commit": commit_sha,
		"branch": ref,
		"discovered": len(discovered),
		"removed": len(previous_rows) - len(previous_rows.keys() & {s.folder_name for s in discovered}),
		"invalidKeys": invalid_keys,
	}


@frappe.whitelist(methods=["POST"])
def fetch_skills(repo: str) -> dict[str, Any]:
	"""Fetch Skill button: refresh the skills child table from the repository."""
	doc = _get_repo_doc(repo, "write")
	try:
		return fetch_repo_skills(doc)
	except Exception as error:
		_record_repo_error(doc, "last_fetch_error", error)
		raise frappe.ValidationError(_sanitized_error_message(error)) from error


def _sync_one(
	doc,
	row,
	parsed: ParsedRepoUrl,
	credential: RepoCredential,
	entries: tuple[TreeEntry, ...],
	pinned_commit: str,
	limits,
) -> tuple[str, str | None]:
	from afaa.ai.skill_bundles import validate_relative_path_limits

	folder_name = (row.skill_folder or "").strip()
	if not folder_name:
		return "skipped", _("The skill folder is missing on this row.")
	skill_dir = f"{doc.skills_folder}/{folder_name}"
	skill_key = (row.skill_key or "").strip() or derive_skill_key(doc.name, folder_name)
	try:
		validate_key(skill_key, _("Skill Key"))
	except frappe.ValidationError:
		return "skipped", _("Skill Key {0} is invalid for folder {1}.").format(
			frappe.bold(skill_key), frappe.bold(folder_name)
		)

	files: list[tuple[str, TreeEntry]] = []
	total_bytes = 0
	for entry in entries:
		if entry.type != "blob" or not entry.path.startswith(f"{skill_dir}/"):
			continue
		relative_path = entry.path[len(skill_dir) + 1 :]
		try:
			validate_relative_path_limits(relative_path, limits)
		except (ValueError, frappe.ValidationError) as error:
			return "skipped", str(error)
		if entry.size > limits.max_file_bytes:
			return "skipped", _("Bundle file exceeds afaa_skill_bundle_max_file_bytes ({0}).").format(
				limits.max_file_bytes
			)
		files.append((relative_path, entry))
		total_bytes += entry.size
	if not files:
		return "skipped", _("The skill folder is empty or missing at the pinned commit.")
	if len(files) > limits.max_files:
		return "skipped", _("Bundle exceeds afaa_skill_bundle_max_files ({0}).").format(limits.max_files)
	if total_bytes > limits.max_bundle_bytes:
		return "skipped", _("Bundle exceeds afaa_skill_bundle_max_bundle_bytes ({0}).").format(
			limits.max_bundle_bytes
		)

	skill_md_entry = next((entry for path, entry in files if path == SKILL_FILE_NAME), None)
	if not skill_md_entry:
		return "skipped", _("SKILL.md is missing at the pinned commit.")

	linked = row.ai_skill if row.ai_skill and frappe.db.exists("AI Skill", row.ai_skill) else None
	if linked and linked != skill_key:
		return "skipped", _("The linked AI Skill does not match the derived skill key.")
	if not linked and frappe.db.exists("AI Skill", skill_key):
		return "skipped", _(
			"An AI Skill with key {0} already exists and was not created by this repository."
		).format(frappe.bold(skill_key))

	markdown_bytes = fetch_blob(parsed, credential, skill_md_entry.sha, skill_md_entry.size, limits)
	parsed_markdown = split_skill_markdown(markdown_bytes)
	if parsed_markdown is None:
		return "skipped", _("SKILL.md is not valid UTF-8 text.")
	frontmatter_name, description, instructions = parsed_markdown
	if not instructions.strip():
		return "skipped", _("SKILL.md does not contain any instructions.")

	desired: dict[str, bytes] = {}
	for relative_path, entry in files:
		if relative_path == SKILL_FILE_NAME:
			desired[relative_path] = markdown_bytes
		else:
			desired[relative_path] = fetch_blob(parsed, credential, entry.sha, entry.size, limits)

	name_value = _display_name(folder_name, frontmatter_name)
	description = (description or "").strip() or None
	created = linked is None
	if created:
		skill_doc = frappe.get_doc(
			{
				"doctype": "AI Skill",
				"skill_key": skill_key,
				"skill_name": name_value,
				"description": description,
				"instructions": instructions,
			}
		)
		skill_doc.insert()
	else:
		skill_doc = frappe.get_doc("AI Skill", skill_key)
		skill_doc.skill_name = name_value
		skill_doc.description = description
		skill_doc.instructions = instructions

	if created:
		current_folders, current_files = [], []
	else:
		current_folders, current_files = inspect_editable_bundle(skill_key)
	current = {item.relative_path: item.content for item in current_files}
	unchanged_bundle = not created and current == desired
	stored = (
		None
		if created
		else frappe.db.get_value(
			"AI Skill", skill_key, ["skill_name", "description", "instructions"], as_dict=True
		)
	)
	unchanged_fields = bool(stored) and (
		stored.skill_name == name_value
		and (stored.description or None) == description
		and (stored.instructions or "") == instructions
	)

	if unchanged_bundle and unchanged_fields:
		row.ai_skill = skill_key
		row.synced_commit = pinned_commit
		return "unchanged", None

	if not created and not unchanged_fields:
		skill_doc.save()
	for relative_path, content in desired.items():
		if current.get(relative_path) != content:
			upload_bundle_file(
				skill_key,
				relative_path,
				base64.b64encode(content).decode("ascii"),
			)
	for relative_path in current:
		if relative_path not in desired:
			delete_bundle_member(skill_key, relative_path)
	for folder_path in sorted(current_folders, key=lambda path: path.count("/"), reverse=True):
		if any(path.startswith(f"{folder_path}/") for path in desired):
			continue
		try:
			delete_bundle_member(skill_key, folder_path)
		except frappe.DoesNotExistError:
			continue
	create_skill_bundle_version(skill_key)

	row.ai_skill = skill_key
	row.synced_commit = pinned_commit
	return ("created" if created else "updated"), None


def sync_repo_skills(doc) -> dict[str, Any]:
	"""Materialize the fetched skills as AI Skills pinned to last_fetched_commit."""
	parsed = parse_repo_url(doc.repo_url)
	credential = resolve_repo_credentials(parsed.owner, parsed.repository)

	if not doc.last_fetched_commit:
		fetch_repo_skills(doc)
		doc.reload()

	pinned_commit = doc.last_fetched_commit
	try:
		tree_sha = commit_tree_sha(parsed, credential, pinned_commit)
	except SkillRepoHTTPError as error:
		if error.status_code == 404:
			raise frappe.ValidationError(
				_(
					"The pinned commit no longer exists on GitHub (the repository history may have"
					" been rewritten). Fetch Skill again to pin a new commit."
				)
			) from error
		raise
	entries = fetch_tree_entries(parsed, credential, tree_sha)
	limits = get_skill_bundle_limits()

	results: list[dict[str, Any]] = []
	seen_keys: set[str] = set()
	for row in sorted(doc.skills, key=lambda item: item.idx or 0):
		outcome = {"skill": row.skill_folder, "skillName": row.skill_name, "skillKey": row.skill_key}
		results.append(outcome)
		if not cint(row.sync):
			outcome.update(status="skipped", reason=_("Sync is disabled for this skill."))
			continue
		if row.skill_key in seen_keys:
			outcome.update(status="skipped", reason=_("Another skill already uses this key."))
			continue
		seen_keys.add(row.skill_key)

		previous_ai_skill, previous_commit = row.ai_skill, row.synced_commit
		savepoint = f"afaa_skill_repo_{frappe.generate_hash(length=10)}"
		frappe.db.savepoint(savepoint)
		try:
			status, reason = _sync_one(doc, row, parsed, credential, entries, pinned_commit, limits)
		except Exception as error:
			frappe.db.rollback(savepoint=savepoint)
			row.ai_skill, row.synced_commit = previous_ai_skill, previous_commit
			outcome.update(status="skipped", reason=_sanitized_error_message(error))
			if not isinstance(error, (SkillRepoHTTPError, frappe.ValidationError, frappe.PermissionError)):
				frappe.log_error(
					title=_("AI Skill Repo sync failed"),
					message=frappe.get_traceback(),
				)
		else:
			frappe.db.release_savepoint(savepoint)
			outcome.update(status=status, reason=reason)

	doc.last_synced_at = now_datetime()
	doc.last_sync_error = None
	doc.save()

	counts = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}
	for outcome in results:
		counts[outcome["status"]] += 1
	return {"commit": pinned_commit, "results": results, "counts": counts}


@frappe.whitelist(methods=["POST"])
def sync_skills(repo: str) -> dict[str, Any]:
	"""Sync Skill button: create or update AI Skills from the pinned commit."""
	doc = _get_repo_doc(repo, "write")
	try:
		return sync_repo_skills(doc)
	except Exception as error:
		_record_repo_error(doc, "last_sync_error", error)
		raise frappe.ValidationError(_sanitized_error_message(error)) from error
