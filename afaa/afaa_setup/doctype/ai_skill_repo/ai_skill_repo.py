# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from afaa.utils.data import validate_key


class AISkillRepo(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from afaa.afaa_setup.doctype.ai_skill_repo_skill.ai_skill_repo_skill import AISkillRepoSkill

		branch: DF.Data | None
		disabled: DF.Check
		last_fetch_error: DF.SmallText | None
		last_fetched_at: DF.Datetime | None
		last_fetched_commit: DF.Data | None
		last_sync_error: DF.SmallText | None
		last_synced_at: DF.Datetime | None
		repo_slug: DF.Data
		repo_url: DF.Data
		skills: DF.Table[AISkillRepoSkill]
		skills_folder: DF.Data
	# end: auto-generated types

	def validate(self):
		from afaa.ai.skill_repos import normalize_skills_folder, parse_repo_url

		validate_key(self.repo_slug, _("Repo Slug"))
		stored_slug = None if self.is_new() else frappe.db.get_value(self.doctype, self.name, "repo_slug")
		if stored_slug and stored_slug != self.repo_slug:
			frappe.throw(_("Repo Slug cannot be changed after creation."), frappe.PermissionError)

		self.repo_url = (self.repo_url or "").strip()
		parsed = parse_repo_url(self.repo_url)
		branch = (self.branch or "").strip()
		if not branch and parsed.ref:
			branch = parsed.ref
		self.branch = branch or None
		self.skills_folder = normalize_skills_folder(self.skills_folder)
