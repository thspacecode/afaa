from typing import TYPE_CHECKING, Any

from .base import DocTypeFactory

if TYPE_CHECKING:
	from afaa.afaa_setup.doctype.ai_skill.ai_skill import AISkill


class AISkillFactory(DocTypeFactory["AISkill"]):
	doctype = "AI Skill"

	@classmethod
	def defaults(cls) -> dict[str, Any]:
		return {
			"skill_name": "Frappe Data Reader",
			"skill_key": "frappe-data-reader",
			"disabled": 0,
			"description": "Inspect Frappe metadata and records without changing them.",
			"instructions": (
				"Use the schema tool before querying an unfamiliar DocType. "
				"Use list and count for collections and get-doc for a known record. "
				"Never invent field names or record values."
			),
			"required_tools": [
				{"tool": "frappe_get_doctype_schema"},
				{"tool": "frappe_get_list"},
				{"tool": "frappe_get_doc"},
				{"tool": "frappe_get_count"},
			],
		}
