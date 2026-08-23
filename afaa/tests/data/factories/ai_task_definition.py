from typing import TYPE_CHECKING, Any

from .base import DocTypeFactory

if TYPE_CHECKING:
	from afaa.afaa_setup.doctype.ai_task_definition.ai_task_definition import AITaskDefinition


class AITaskDefinitionFactory(DocTypeFactory["AITaskDefinition"]):
	doctype = "AI Task Definition"

	@classmethod
	def defaults(cls) -> dict[str, Any]:
		return {
			"task_name": "Answer Frappe Data Question",
			"task_key": "answer-frappe-data-question",
			"disabled": 0,
			"description": "Answer a factual question using data available in the Frappe site.",
			"instructions": (
				"Answer the user's question from Frappe data. Use the available read tools "
				"when the answer depends on site records. Return a concise answer and state "
				"which DocTypes were consulted in sources."
			),
			"allow_soft_failure": 1,
			"required_tools": [
				{"tool": "frappe_get_doctype_schema"},
				{"tool": "frappe_get_list"},
				{"tool": "frappe_get_doc"},
				{"tool": "frappe_get_count"},
			],
			"expected_output": [
				{
					"field_name": "answer",
					"field_type": "String",
					"required": 1,
					"description": "A concise answer to the user's question.",
				},
				{
					"field_name": "sources",
					"field_type": "String",
					"required": 0,
					"description": "Comma-separated DocTypes consulted to produce the answer.",
				},
			],
		}
