# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class AISkillRepoSkill(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		ai_skill: DF.Link | None
		skill_folder: DF.Data
		skill_key: DF.Data | None
		skill_name: DF.Data
		sync: DF.Check
		synced_commit: DF.Data | None
	# end: auto-generated types
