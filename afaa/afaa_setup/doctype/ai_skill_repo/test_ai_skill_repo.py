# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import base64
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from afaa.ai.skill_bundles import (
	create_skill_bundle_version,
	resolve_skill_bundle_version,
)
from afaa.ai.skill_repos import (
	SkillRepoHTTPError,
	enabled_skill_tag_names,
	fetch_skills,
	merge_skill_tags,
	normalize_skills_folder,
	parse_repo_url,
	split_skill_markdown,
	sync_skills,
)
from afaa.tests.utils import AFAATestSuite

COMMIT_ONE = "c" * 40
COMMIT_TWO = "d" * 40
TREE_ONE = "a" * 40
TREE_TWO = "b" * 40


def blob_payload(content: bytes) -> dict:
	return {
		"content": base64.b64encode(content).decode("ascii"),
		"encoding": "base64",
		"size": len(content),
	}


def make_skill_md(name=None, description=None, body="Follow the guide carefully."):
	frontmatter = ""
	if name is not None or description is not None:
		lines = ["---"]
		if name is not None:
			lines.append(f"name: {name}")
		if description is not None:
			lines.append(f"description: {description}")
		lines.append("---")
		frontmatter = "\n".join(lines) + "\n"
	return (frontmatter + body).encode("utf-8")


class FakeGitHub:
	"""Deterministic stand-in for the bounded GitHub REST client."""

	def __init__(self):
		self.routes: dict[str, object] = {}
		self.requests: list[tuple[str, str | None]] = []

	def serve(self, url: str, payload: object) -> None:
		self.routes[url] = payload

	@property
	def urls(self) -> list[str]:
		return [url for url, _token in self.requests]

	def __call__(self, url, *, token=None, max_bytes=None):
		self.requests.append((url, token))
		if url not in self.routes:
			raise SkillRepoHTTPError(404)
		payload = self.routes[url]
		if isinstance(payload, Exception):
			raise payload
		return payload


class FakeRepository:
	"""A canned GitHub repository served through FakeGitHub."""

	def __init__(self, fake: FakeGitHub, owner="octo", repository="skills"):
		self.fake = fake
		self.base = f"https://api.github.com/repos/{owner}/{repository}"

	def serve_default_branch(self, branch="main", private=False):
		self.fake.serve(self.base, {"default_branch": branch, "private": private})

	def serve_ref(self, ref: str, commit: str, tree_sha: str = TREE_ONE):
		self.fake.serve(
			f"{self.base}/commits/{ref}",
			{"sha": commit, "commit": {"tree": {"sha": tree_sha}}},
		)

	def serve_commit_tree(self, commit: str, tree_sha: str):
		self.fake.serve(f"{self.base}/git/commits/{commit}", {"sha": commit, "tree": {"sha": tree_sha}})

	def serve_tree(self, tree_sha: str, files: dict[str, bytes], folders: list[str]):
		for _path, content in files.items():
			digest = hashlib.sha256(content).hexdigest()
			self.fake.serve(f"{self.base}/git/blobs/{digest}", blob_payload(content))
		entries = [
			{"path": folder, "type": "tree", "sha": hashlib.sha256(folder.encode()).hexdigest()}
			for folder in folders
		]
		entries.extend(
			{
				"path": path,
				"type": "blob",
				"sha": hashlib.sha256(content).hexdigest(),
				"size": len(content),
			}
			for path, content in files.items()
		)
		self.fake.serve(
			f"{self.base}/git/trees/{tree_sha}?recursive=1", {"truncated": False, "tree": entries}
		)


def default_repository_files() -> dict[str, bytes]:
	return {
		"skills/code-style/SKILL.md": make_skill_md(name="Code Style", description="Write consistent code."),
		"skills/code-style/guide.md": b"# Guide\nversion one\n",
		"skills/review-checklist/SKILL.md": make_skill_md(name="Review Checklist"),
		"skills/README.md": b"# Loose README is not a skill\n",
		"docs/other/SKILL.md": make_skill_md(name="Outside"),
	}


def default_repository_folders() -> list[str]:
	return ["skills", "skills/code-style", "skills/review-checklist", "skills/not-a-skill", "docs"]


def private_hook(owner, repository_name):
	return {"status": "private", "token": "ghp_secret_installation_token", "default_branch": "main"}


def denied_hook(owner, repository_name):
	return {"status": "private", "token": None, "reason": "Install the GitHub App first."}


def selective_skill_repo_hooks(hook_path: str):
	"""Patch ``frappe.get_hooks`` only for the skill-repo credential hook."""
	original_get_hooks = frappe.get_hooks

	def wrapper(hook_name=None, *args, **kwargs):
		if hook_name == "afaa_skill_repo_github_credentials":
			return [hook_path]
		return original_get_hooks(hook_name, *args, **kwargs)

	return wrapper


class TestAISkillRepo(AFAATestSuite):
	def make_repo(self, **values):
		suffix = frappe.generate_hash(length=8).lower()
		values.setdefault("repo_slug", f"repo-{suffix}")
		values.setdefault("repo_url", "https://github.com/octo/skills")
		values.setdefault("branch", "main")
		values.setdefault("skills_folder", "skills")
		return frappe.get_doc({"doctype": "AI Skill Repo", **values}).insert(ignore_permissions=True)

	def make_tag(self, tag_key: str, **overrides):
		if not frappe.db.exists("AI Skill Tag", tag_key):
			frappe.get_doc(
				{
					"doctype": "AI Skill Tag",
					"tag_key": tag_key,
					"tag_name": tag_key.replace("-", " ").title(),
					**overrides,
				}
			).insert(ignore_permissions=True)
		return tag_key

	def skill_tags(self, skill_key: str) -> list[str]:
		return sorted(
			frappe.get_all(
				"AI Skill Tag Link",
				filters={"parent": skill_key, "parenttype": "AI Skill"},
				pluck="tag",
			)
		)

	def test_merge_skill_tags_is_additive_and_deduplicating(self):
		self.assertEqual(
			[row["tag"] for row in merge_skill_tags(None, ["repo", "repo2"])],
			["repo", "repo2"],
		)
		existing = [SimpleNamespace(tag="manual"), SimpleNamespace(tag=None), SimpleNamespace(tag="repo")]
		self.assertEqual(
			[row["tag"] for row in merge_skill_tags(existing, ["repo", "manual2"])],
			["manual", "repo", "manual2"],
			"existing tags keep their order and repo tags are appended once",
		)
		self.assertEqual(merge_skill_tags(existing, []), [{"tag": "manual"}, {"tag": "repo"}])
		self.assertEqual(merge_skill_tags([], []), [])

	def test_enabled_skill_tag_names_skips_disabled_and_duplicates(self):
		enabled = self.make_tag("enabled-sync-tag")
		disabled = self.make_tag("disabled-sync-tag", disabled=1)
		rows = [
			SimpleNamespace(tag=enabled),
			SimpleNamespace(tag=disabled),
			SimpleNamespace(tag=enabled),
			SimpleNamespace(tag=None),
		]
		self.assertEqual(enabled_skill_tag_names(rows), ["enabled-sync-tag"])
		self.assertEqual(enabled_skill_tag_names(None), [])

	def test_sync_merges_repo_tags_and_never_removes_manual_tags(self):
		_fake, repository = self.fake_github()
		self.serve_default_fetch(repository)
		first_tag = self.make_tag("synced-repo-tag")
		manual_tag = self.make_tag("manual-skill-tag")
		repo = self.make_repo(repo_slug="tagged-repo", tags=[{"tag": first_tag}])
		fetch_skills(repo.name)

		created = sync_skills(repo.name)

		self.assertEqual(created["counts"], {"created": 2, "updated": 0, "unchanged": 0, "skipped": 0})
		self.assertEqual(self.skill_tags("tagged-repo-code-style"), ["synced-repo-tag"])

		# A manually added tag survives re-sync of identical upstream content.
		skill = frappe.get_doc("AI Skill", "tagged-repo-code-style")
		skill.append("tags", {"tag": manual_tag})
		skill.save(ignore_permissions=True)
		unchanged = sync_skills(repo.name)
		self.assertEqual(unchanged["counts"]["unchanged"], 2)
		self.assertEqual(self.skill_tags("tagged-repo-code-style"), ["manual-skill-tag", "synced-repo-tag"])

		# A new repo tag is merged in without touching the manual one.
		second_tag = self.make_tag("second-synced-tag")
		repo = frappe.get_doc("AI Skill Repo", repo.name)
		repo.append("tags", {"tag": second_tag})
		repo.save(ignore_permissions=True)
		updated = sync_skills(repo.name)
		outcomes = {item["skill"]: item["status"] for item in updated["results"]}
		self.assertEqual(outcomes["code-style"], "updated", "a tag-only diff still reports updated")
		self.assertEqual(
			self.skill_tags("tagged-repo-code-style"),
			["manual-skill-tag", "second-synced-tag", "synced-repo-tag"],
		)

		# Removing a repo tag never strips it from skills that already carry it.
		repo = frappe.get_doc("AI Skill Repo", repo.name)
		repo.tags = [row for row in repo.tags if row.tag == second_tag]
		repo.save(ignore_permissions=True)
		sync_skills(repo.name)
		self.assertEqual(
			self.skill_tags("tagged-repo-code-style"),
			["manual-skill-tag", "second-synced-tag", "synced-repo-tag"],
		)

	def test_sync_ignores_disabled_repo_tags(self):
		_fake, repository = self.fake_github()
		self.serve_default_fetch(repository)
		disabled_tag = self.make_tag("disabled-repo-tag", disabled=1)
		repo = self.make_repo(repo_slug="disabled-tag-repo", tags=[{"tag": disabled_tag}])
		fetch_skills(repo.name)

		result = sync_skills(repo.name)

		self.assertEqual(result["counts"]["created"], 2)
		self.assertEqual(self.skill_tags("disabled-tag-repo-code-style"), [])

	def test_repo_rejects_duplicate_tags(self):
		tag = self.make_tag("duplicate-repo-tag")
		with self.assertRaisesRegex(frappe.ValidationError, "listed more than once"):
			self.make_repo(repo_slug="dupe-tag-repo", tags=[{"tag": tag}, {"tag": tag}])

	def fake_github(self):
		fake = FakeGitHub()
		patcher = patch("afaa.ai.skill_repos.github_request", fake)
		patcher.start()
		self.addCleanup(patcher.stop)
		return fake, FakeRepository(fake)

	def serve_default_fetch(self, repository: FakeRepository, commit=COMMIT_ONE, tree=TREE_ONE):
		repository.serve_ref("main", commit, tree)
		repository.serve_tree(tree, default_repository_files(), default_repository_folders())
		repository.serve_commit_tree(commit, tree)

	def test_repo_url_and_folder_validation(self):
		parsed = parse_repo_url("https://github.com/octo/skills")
		self.assertEqual((parsed.owner, parsed.repository, parsed.ref), ("octo", "skills", None))
		parsed = parse_repo_url("https://github.com/octo/skills.git/tree/feature/x")
		self.assertEqual((parsed.owner, parsed.repository, parsed.ref), ("octo", "skills", "feature/x"))
		for invalid in (
			"https://user:token@github.com/octo/skills",
			"http://github.com/octo/skills",
			"git@github.com:octo/skills.git",
			"https://gitlab.com/octo/skills",
			"https://github.com/octo",
			"https://github.com/octo/skills/blob/main/SKILL.md",
			"https://github.com/octo/skills?token=1",
		):
			with self.assertRaises(frappe.ValidationError):
				parse_repo_url(invalid)
		self.assertEqual(normalize_skills_folder(" /skills/ "), "skills")
		self.assertEqual(normalize_skills_folder("skills/engineering"), "skills/engineering")
		for invalid in ("../skills", "skills/../guide", "skills//guide", "skills\\guide", ".."):
			with self.assertRaises(frappe.ValidationError):
				normalize_skills_folder(invalid)

	def test_tree_ref_url_prefills_branch_and_slug_is_immutable(self):
		repo = frappe.get_doc(
			{
				"doctype": "AI Skill Repo",
				"repo_slug": "prefill-repo",
				"repo_url": "https://github.com/mattpocock/skills/tree/main",
				"skills_folder": "skills/engineering",
			}
		).insert(ignore_permissions=True)
		self.assertEqual(repo.branch, "main")
		repo.repo_slug = "renamed-repo"
		with self.assertRaises(frappe.PermissionError):
			repo.save(ignore_permissions=True)

	def test_fetch_lists_skills_and_ignores_loose_files(self):
		fake, repository = self.fake_github()
		self.serve_default_fetch(repository)
		repo = self.make_repo(repo_slug="fetch-list")

		result = fetch_skills(repo.name)

		self.assertEqual(result["commit"], COMMIT_ONE)
		rows = {row.skill_folder: row for row in frappe.get_doc("AI Skill Repo", repo.name).skills}
		self.assertEqual(set(rows), {"code-style", "review-checklist"})
		self.assertEqual(rows["code-style"].skill_name, "Code Style")
		self.assertEqual(rows["code-style"].skill_key, "fetch-list-code-style")
		self.assertEqual(rows["code-style"].sync, 1)
		self.assertIsNone(rows["code-style"].ai_skill)
		self.assertEqual(frappe.db.get_value("AI Skill Repo", repo.name, "last_fetched_commit"), COMMIT_ONE)
		self.assertIn(f"{repository.base}/commits/main", fake.urls)

	def test_fetch_resolves_default_branch_when_branch_empty(self):
		fake, repository = self.fake_github()
		repository.serve_default_branch(branch="trunk")
		repository.serve_ref("trunk", COMMIT_ONE)
		repository.serve_tree(TREE_ONE, default_repository_files(), default_repository_folders())
		repo = self.make_repo(branch=None)

		result = fetch_skills(repo.name)

		self.assertEqual(result["branch"], "trunk")
		self.assertIn(repository.base, fake.urls)

	def test_fetch_preserves_choices_and_removes_deleted_skills(self):
		_fake, repository = self.fake_github()
		self.serve_default_fetch(repository)
		repo = self.make_repo(repo_slug="mirror-repo")
		fetch_skills(repo.name)
		sync_skills(repo.name)

		repo = frappe.get_doc("AI Skill Repo", repo.name)
		for row in repo.skills:
			if row.skill_folder == "code-style":
				row.sync = 0
		repo.save(ignore_permissions=True)

		# Upstream deletes review-checklist while code-style survives.
		files = {
			"skills/code-style/SKILL.md": make_skill_md(name="Code Style v2"),
			"skills/README.md": b"README\n",
		}
		repository.serve_ref("main", COMMIT_TWO, TREE_TWO)
		repository.serve_tree(TREE_TWO, files, ["skills", "skills/code-style"])
		repository.serve_commit_tree(COMMIT_TWO, TREE_TWO)

		fetch_skills(repo.name)

		repo = frappe.get_doc("AI Skill Repo", repo.name)
		rows = {row.skill_folder: row for row in repo.skills}
		self.assertEqual(set(rows), {"code-style"})
		self.assertEqual(rows["code-style"].sync, 0)
		self.assertEqual(rows["code-style"].ai_skill, "mirror-repo-code-style")
		# Deletion policy: the AI Skill of the removed folder stays untouched.
		self.assertTrue(frappe.db.exists("AI Skill", "mirror-repo-review-checklist"))
		self.assertEqual(frappe.db.get_value("AI Skill Repo", repo.name, "last_fetched_commit"), COMMIT_TWO)

	def test_sync_creates_skills_and_bundle_versions_from_pinned_commit(self):
		fake, repository = self.fake_github()
		self.serve_default_fetch(repository)
		repo = self.make_repo(repo_slug="sync-repo")

		fetch_skills(repo.name)
		# The branch advances after the fetch; sync must still use COMMIT_ONE.
		repository.serve_ref("main", COMMIT_TWO, TREE_TWO)
		repository.serve_tree(
			TREE_TWO,
			{
				"skills/code-style/SKILL.md": make_skill_md(name="Changed"),
				"skills/review-checklist/SKILL.md": make_skill_md(name="Changed"),
			},
			["skills", "skills/code-style", "skills/review-checklist"],
		)
		repository.serve_commit_tree(COMMIT_TWO, TREE_TWO)

		result = sync_skills(repo.name)

		self.assertEqual(result["commit"], COMMIT_ONE)
		self.assertEqual(result["counts"], {"created": 2, "updated": 0, "unchanged": 0, "skipped": 0})
		skill = frappe.get_doc("AI Skill", "sync-repo-code-style")
		self.assertEqual(skill.skill_name, "Code Style")
		self.assertEqual(skill.description, "Write consistent code.")
		self.assertEqual(skill.instructions, "Follow the guide carefully.")
		self.assertNotIn(COMMIT_TWO, str(fake.urls))

		version = create_skill_bundle_version("sync-repo-code-style")
		resolved = resolve_skill_bundle_version(version.version_id, expected_skill_key="sync-repo-code-style")
		contents = {item.path: base64.b64decode(item.content) for item in resolved.files}
		self.assertEqual(
			contents["SKILL.md"],
			make_skill_md(name="Code Style", description="Write consistent code."),
		)
		self.assertEqual(contents["guide.md"], b"# Guide\nversion one\n")

		row = next(
			row
			for row in frappe.get_doc("AI Skill Repo", repo.name).skills
			if row.skill_key == "sync-repo-code-style"
		)
		self.assertEqual(row.synced_commit, COMMIT_ONE)

	def test_sync_is_reproducible_and_reports_updates(self):
		_fake, repository = self.fake_github()
		self.serve_default_fetch(repository)
		repo = self.make_repo(repo_slug="repro-repo")
		fetch_skills(repo.name)
		sync_skills(repo.name)
		version_count = frappe.db.count("AI Skill Bundle Version", {"skill": "repro-repo-code-style"})

		again = sync_skills(repo.name)

		self.assertEqual(again["counts"], {"created": 0, "updated": 0, "unchanged": 2, "skipped": 0})
		self.assertEqual(
			frappe.db.count("AI Skill Bundle Version", {"skill": "repro-repo-code-style"}),
			version_count,
		)

		# Upstream moves forward: fetch pins the new commit, sync updates content.
		files = {
			"skills/code-style/SKILL.md": make_skill_md(name="Code Style", body="Updated body."),
			"skills/code-style/guide.md": b"# Guide\nversion two\n",
			"skills/review-checklist/SKILL.md": make_skill_md(name="Review Checklist", body="New checks."),
		}
		repository.serve_ref("main", COMMIT_TWO, TREE_TWO)
		repository.serve_tree(TREE_TWO, files, default_repository_folders())
		repository.serve_commit_tree(COMMIT_TWO, TREE_TWO)
		fetch_skills(repo.name)
		updated = sync_skills(repo.name)

		self.assertEqual(updated["counts"]["updated"], 2)
		self.assertEqual(updated["counts"]["unchanged"], 0)
		self.assertGreater(
			frappe.db.count("AI Skill Bundle Version", {"skill": "repro-repo-code-style"}),
			version_count,
		)
		skill = frappe.get_doc("AI Skill", "repro-repo-code-style")
		self.assertEqual(skill.instructions, "Updated body.")
		tree = create_skill_bundle_version("repro-repo-code-style")
		resolved = resolve_skill_bundle_version(tree.version_id)
		contents = {item.path: base64.b64decode(item.content) for item in resolved.files}
		self.assertEqual(contents["guide.md"], b"# Guide\nversion two\n")

	def test_sync_skips_disabled_rows_without_touching_their_skills(self):
		_fake, repository = self.fake_github()
		self.serve_default_fetch(repository)
		repo = self.make_repo(repo_slug="disabled-repo")
		fetch_skills(repo.name)
		sync_skills(repo.name)

		repo = frappe.get_doc("AI Skill Repo", repo.name)
		for row in repo.skills:
			if row.skill_folder == "code-style":
				row.sync = 0
		repo.save(ignore_permissions=True)

		files = {
			"skills/code-style/SKILL.md": make_skill_md(name="Changed"),
			"skills/review-checklist/SKILL.md": make_skill_md(name="Review Checklist v2"),
		}
		repository.serve_ref("main", COMMIT_TWO, TREE_TWO)
		repository.serve_tree(TREE_TWO, files, default_repository_folders())
		repository.serve_commit_tree(COMMIT_TWO, TREE_TWO)
		fetch_skills(repo.name)
		result = sync_skills(repo.name)

		outcomes = {item["skill"]: item for item in result["results"]}
		self.assertEqual(outcomes["code-style"]["status"], "skipped")
		self.assertEqual(outcomes["review-checklist"]["status"], "updated")
		self.assertEqual(
			frappe.db.get_value("AI Skill", "disabled-repo-code-style", "skill_name"), "Code Style"
		)

	def test_sync_never_overwrites_unlinked_colliding_skills(self):
		_fake, repository = self.fake_github()
		self.serve_default_fetch(repository)
		repo = self.make_repo(repo_slug="collide-repo")
		frappe.get_doc(
			{
				"doctype": "AI Skill",
				"skill_key": "collide-repo-code-style",
				"skill_name": "Hand Made",
				"instructions": "Hand written.",
			}
		).insert(ignore_permissions=True)
		fetch_skills(repo.name)

		result = sync_skills(repo.name)

		outcomes = {item["skill"]: item for item in result["results"]}
		self.assertEqual(outcomes["code-style"]["status"], "skipped")
		self.assertIn("already exists", outcomes["code-style"]["reason"])
		self.assertEqual(
			frappe.db.get_value("AI Skill", "collide-repo-code-style", "instructions"), "Hand written."
		)
		self.assertEqual(outcomes["review-checklist"]["status"], "created")

	def test_sync_skips_oversized_and_invalid_key_skills_but_syncs_others(self):
		_fake, repository = self.fake_github()
		long_folder = "x" * 60
		files = {
			"skills/big-file/SKILL.md": make_skill_md(name="Big"),
			"skills/big-file/huge.bin": b"0" * 64,
			f"skills/{long_folder}/SKILL.md": make_skill_md(name="Long"),
			"skills/ok-skill/SKILL.md": make_skill_md(name="OK"),
		}
		folders = ["skills", "skills/big-file", f"skills/{long_folder}", "skills/ok-skill"]
		repository.serve_ref("main", COMMIT_ONE)
		repository.serve_tree(TREE_ONE, files, folders)
		repository.serve_commit_tree(COMMIT_ONE, TREE_ONE)
		repo = self.make_repo(repo_slug="limits-repo")
		fetch_skills(repo.name)

		from afaa.ai.skill_bundles import SkillBundleLimits

		small_limits = SkillBundleLimits(
			max_files=100,
			max_depth=8,
			max_path_length=512,
			max_file_bytes=50,
			max_bundle_bytes=5 * 1024 * 1024,
			max_agent_bundle_bytes=10 * 1024 * 1024,
		)
		with patch("afaa.ai.skill_repos.get_skill_bundle_limits", return_value=small_limits):
			result = sync_skills(repo.name)

		outcomes = {item["skill"]: item for item in result["results"]}
		self.assertEqual(outcomes["ok-skill"]["status"], "created")
		self.assertIn("max_file_bytes", outcomes["big-file"]["reason"])
		self.assertIn("invalid", outcomes[long_folder]["reason"])
		self.assertFalse(frappe.db.exists("AI Skill", f"limits-repo-{long_folder}"))

	def test_fetch_failures_are_sanitized_and_recorded(self):
		_fake, repository = self.fake_github()
		repository.serve_ref("main", COMMIT_ONE)
		# No tree served: the tree request fails with 404.
		repo = self.make_repo(repo_slug="error-repo")

		with self.assertRaises(frappe.ValidationError) as context:
			fetch_skills(repo.name)

		message = str(context.exception)
		self.assertNotIn("token", message.lower())
		self.assertEqual(
			frappe.db.get_value("AI Skill Repo", repo.name, "last_fetch_error"),
			"The GitHub repository, branch, folder or pinned commit was not found.",
		)
		self.assertIsNone(frappe.db.get_value("AI Skill Repo", repo.name, "last_fetched_commit"))

	def test_sync_reports_missing_pinned_commit_with_guidance(self):
		_fake, repository = self.fake_github()
		self.serve_default_fetch(repository)
		repo = self.make_repo(repo_slug="pin-gone-repo")
		fetch_skills(repo.name)
		repository.fake.routes.pop(f"{repository.base}/git/commits/{COMMIT_ONE}")

		with self.assertRaises(frappe.ValidationError) as context:
			sync_skills(repo.name)

		self.assertIn("Fetch Skill again", str(context.exception))

	def test_credential_hook_supplies_private_token(self):
		fake, repository = self.fake_github()
		self.serve_default_fetch(repository)
		repo = self.make_repo(repo_slug="private-repo")

		with patch(
			"afaa.ai.skill_repos.frappe.get_hooks",
			selective_skill_repo_hooks(f"{__name__}.private_hook"),
		):
			fetch_skills(repo.name)

		tokens = [token for _url, token in fake.requests]
		self.assertTrue(tokens)
		self.assertTrue(all(token == "ghp_secret_installation_token" for token in tokens))
		# The branch never had to be resolved through repository metadata.
		self.assertNotIn(repository.base, fake.urls)

	def test_credential_hook_without_token_raises_actionable_error(self):
		fake, _repository = self.fake_github()
		repo = self.make_repo(repo_slug="denied-repo")

		with patch(
			"afaa.ai.skill_repos.frappe.get_hooks",
			selective_skill_repo_hooks(f"{__name__}.denied_hook"),
		):
			with self.assertRaises(frappe.ValidationError) as context:
				fetch_skills(repo.name)

		self.assertIn("Install the GitHub App first.", str(context.exception))
		self.assertEqual(fake.requests, [])
		self.assertIn(
			"Install the GitHub App first.",
			frappe.db.get_value("AI Skill Repo", repo.name, "last_fetch_error"),
		)

	def test_skill_markdown_frontmatter_parsing(self):
		name, description, body = split_skill_markdown(
			b'---\nname: "Quoted Name"\ndescription: A description\nother: ignored\n---\n\nBody text.\n'
		)
		self.assertEqual(name, "Quoted Name")
		self.assertEqual(description, "A description")
		self.assertEqual(body, "Body text.\n")

		name, description, body = split_skill_markdown(b"No frontmatter here.\n")
		self.assertIsNone(name)
		self.assertIsNone(description)
		self.assertEqual(body, "No frontmatter here.\n")

		name, description, body = split_skill_markdown(b"---\r\nname: CRLF\r\n---\r\nBody.\r\n")
		self.assertEqual(name, "CRLF")
		self.assertEqual(body, "Body.\r\n")

		self.assertIsNone(split_skill_markdown(b"\x00\xff\xfe binary"))
		# An unclosed frontmatter block degrades to plain body text.
		self.assertEqual(split_skill_markdown(b"---\nnever closed\n"), (None, None, "---\nnever closed\n"))

	def test_nested_skills_folder_discovery(self):
		_fake, repository = self.fake_github()
		files = {
			"skills/engineering/code-style/SKILL.md": make_skill_md(name="Nested"),
			"skills/engineering/README.md": b"loose file\n",
		}
		repository.serve_ref("main", COMMIT_ONE)
		repository.serve_tree(
			TREE_ONE, files, ["skills", "skills/engineering", "skills/engineering/code-style"]
		)
		repository.serve_commit_tree(COMMIT_ONE, TREE_ONE)
		repo = self.make_repo(repo_slug="nested-repo", skills_folder="skills/engineering")

		fetch_skills(repo.name)
		result = sync_skills(repo.name)

		rows = frappe.get_doc("AI Skill Repo", repo.name).skills
		self.assertEqual([row.skill_folder for row in rows], ["code-style"])
		self.assertEqual(result["counts"]["created"], 1)
		self.assertTrue(frappe.db.exists("AI Skill", "nested-repo-code-style"))
