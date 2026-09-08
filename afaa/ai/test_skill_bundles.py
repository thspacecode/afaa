# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import base64
import hashlib

import frappe

from afaa.ai.skill_bundles import (
	EMPTY_BUNDLE_DIGEST,
	canonical_relative_path,
	create_skill_bundle_version,
	delete_bundle_member,
	garbage_collect_skill_bundle_versions,
	get_bundle_tree,
	read_bundle_file,
	release_skill_bundle_version,
	resolve_skill_bundle_version,
	retain_skill_bundle_version,
	upload_bundle_file,
)
from afaa.tests.utils import AFAATestSuite


class TestSkillBundles(AFAATestSuite):
	def make_skill(self):
		suffix = frappe.generate_hash(length=8).lower()
		return frappe.get_doc(
			{
				"doctype": "AI Skill",
				"skill_key": f"bundle-test-{suffix}",
				"skill_name": f"Bundle Test {suffix}",
				"instructions": "Use references relative to the skill root.",
			}
		).insert(ignore_permissions=True)

	def test_skill_creates_exact_private_managed_root_and_key_is_immutable(self):
		skill = self.make_skill()
		root = frappe.get_doc("File", f"Home/afaa/ai-skill/{skill.name}")
		self.assertTrue(root.is_folder)
		self.assertTrue(root.is_private)
		self.assertEqual((root.attached_to_doctype, root.attached_to_name), ("AI Skill", skill.name))

		skill.skill_key = f"changed-{skill.skill_key}"
		with self.assertRaisesRegex(frappe.PermissionError, "cannot be changed"):
			skill.save(ignore_permissions=True)

	def test_nested_text_and_binary_files_round_trip_and_versions_do_not_drift(self):
		skill = self.make_skill()
		binary = b"\x00\xfftemplate\n"
		upload_bundle_file(
			skill.name,
			"references/domain/model.md",
			base64.b64encode(b"domain v1").decode(),
		)
		upload_bundle_file(
			skill.name,
			"assets/template.bin",
			base64.b64encode(binary).decode(),
		)
		upload_bundle_file(skill.name, "assets/empty.txt", "")

		tree = get_bundle_tree(skill.name)
		self.assertEqual(
			[item["path"] for item in tree["items"] if item["type"] == "file"],
			["assets/empty.txt", "assets/template.bin", "references/domain/model.md"],
		)
		read_binary = read_bundle_file(skill.name, "assets/template.bin")
		self.assertFalse(read_binary["isText"])
		self.assertEqual(base64.b64decode(read_binary["content"]), binary)

		first = create_skill_bundle_version(skill.name)
		self.assertEqual(first, create_skill_bundle_version(skill.name))
		upload_bundle_file(
			skill.name,
			"references/domain/model.md",
			base64.b64encode(b"domain v2").decode(),
		)
		second = create_skill_bundle_version(skill.name)
		self.assertNotEqual(first.digest, second.digest)

		resolved = resolve_skill_bundle_version(first.version_id, expected_skill_key=skill.name)
		contents = {item.path: base64.b64decode(item.content) for item in resolved.files}
		self.assertEqual(contents["references/domain/model.md"], b"domain v1")
		self.assertEqual(contents["assets/template.bin"], binary)
		self.assertEqual(contents["assets/empty.txt"], b"")

	def test_empty_bundle_has_deterministic_identity_and_retention_is_idempotent(self):
		skill = self.make_skill()
		version = create_skill_bundle_version(skill.name)
		self.assertEqual(version.digest, EMPTY_BUNDLE_DIGEST)
		self.assertEqual(version.file_count, 0)
		self.assertEqual(version.total_bytes, 0)

		values = {
			"reference_doctype": "P Thread",
			"reference_name": "thread-test",
			"reference_key": skill.name,
		}
		first = retain_skill_bundle_version(version.version_id, **values)
		self.assertEqual(first, retain_skill_bundle_version(version.version_id, **values))
		self.assertEqual(
			frappe.db.count("AI Skill Bundle Reference", {"bundle_version": version.version_id}),
			1,
		)
		release_skill_bundle_version(version.version_id, **values)
		self.assertFalse(frappe.db.exists("AI Skill Bundle Reference", first))

	def test_deleting_editable_content_does_not_delete_immutable_version(self):
		skill = self.make_skill()
		upload_bundle_file(skill.name, "retained.txt", base64.b64encode(b"retained").decode())
		version = create_skill_bundle_version(skill.name)

		delete_bundle_member(skill.name, "retained.txt")

		self.assertEqual(
			[item for item in get_bundle_tree(skill.name)["items"] if item["type"] == "file"], []
		)
		resolved = resolve_skill_bundle_version(version.version_id, expected_skill_key=skill.name)
		self.assertEqual(base64.b64decode(resolved.files[0].content), b"retained")

	def test_management_authorizes_against_skill_instead_of_file_owner(self):
		skill = self.make_skill()
		with self.set_create_user(["AI Manager"]) as first_manager:
			upload_bundle_file(skill.name, "shared.txt", base64.b64encode(b"first").decode())
			file_id = frappe.db.get_value(
				"File",
				{"folder": f"Home/afaa/ai-skill/{skill.name}", "file_name": "shared.txt"},
				"name",
			)
			self.assertEqual(frappe.db.get_value("File", file_id, "owner"), first_manager)

		with self.set_create_user(["AI Manager"]) as second_manager:
			self.assertEqual(read_bundle_file(skill.name, "shared.txt")["text"], "first")
			upload_bundle_file(skill.name, "shared.txt", base64.b64encode(b"second").decode())
			self.assertEqual(read_bundle_file(skill.name, "shared.txt")["text"], "second")
			current_file = frappe.db.get_value(
				"File",
				{"folder": f"Home/afaa/ai-skill/{skill.name}", "file_name": "shared.txt"},
				"name",
			)
			self.assertEqual(frappe.db.get_value("File", current_file, "owner"), second_manager)

	def test_retention_cleanup_rechecks_references_before_deleting(self):
		skill = self.make_skill()
		upload_bundle_file(skill.name, "cleanup.txt", base64.b64encode(b"cleanup").decode())
		version = create_skill_bundle_version(skill.name)
		file_id = frappe.db.get_value(
			"AI Skill Bundle File",
			{"parent": version.version_id},
			"file",
		)
		values = {
			"reference_doctype": "P Thread",
			"reference_name": "retained-thread",
			"reference_key": skill.name,
		}
		retain_skill_bundle_version(version.version_id, **values)
		frappe.db.set_value("AI Skill Bundle Version", version.version_id, "creation", "2000-01-01")
		self.assertNotIn(
			version.version_id,
			garbage_collect_skill_bundle_versions(older_than_days=3650, dry_run=False),
		)

		release_skill_bundle_version(version.version_id, **values)
		self.assertIn(
			version.version_id,
			garbage_collect_skill_bundle_versions(older_than_days=3650, dry_run=False),
		)
		self.assertFalse(frappe.db.exists("AI Skill Bundle Version", version.version_id))
		self.assertFalse(frappe.db.exists("File", file_id))

	def test_manifest_digest_is_independent_of_source_database_order(self):
		skill = self.make_skill()
		for path, content in (("z.txt", b"z"), ("a.txt", b"a")):
			upload_bundle_file(skill.name, path, base64.b64encode(content).decode())
		version = create_skill_bundle_version(skill.name)
		resolved = resolve_skill_bundle_version(version.version_id)
		self.assertEqual([item.path for item in resolved.manifest], ["a.txt", "z.txt"])
		self.assertEqual(
			version.digest,
			hashlib.sha256(
				b'{"files":[{"byteLength":1,"encoding":"base64","path":"a.txt","sha256":"ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"},{"byteLength":1,"encoding":"base64","path":"z.txt","sha256":"594e519ae499312b29433b7dd8a97ff068defcba9755b6d5d00e84c524d67b06"}]}'
			).hexdigest(),
		)

	def test_unsafe_paths_are_rejected(self):
		for path in ("/absolute", "../escape", "a//b", "a/./b", "a\\b", "null\x00byte"):
			with self.subTest(path=path), self.assertRaises(ValueError):
				canonical_relative_path(path)
