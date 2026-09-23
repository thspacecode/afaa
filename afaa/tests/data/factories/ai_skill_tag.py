from typing import TYPE_CHECKING, Any

from .base import DocTypeFactory

if TYPE_CHECKING:
	from afaa.afaa_setup.doctype.ai_skill_tag.ai_skill_tag import AISkillTag


class AISkillTagFactory(DocTypeFactory["AISkillTag"]):
	doctype = "AI Skill Tag"

	@classmethod
	def defaults(cls) -> dict[str, Any]:
		return {
			"tag_name": "Frappe Data",
			"tag_key": "frappe-data",
			"disabled": 0,
			"description": "Skills for reading Frappe data.",
		}
