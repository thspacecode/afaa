from typing import TYPE_CHECKING, Any

from .base import DocTypeFactory

if TYPE_CHECKING:
	from afaa.afaa_setup.doctype.ai_agent.ai_agent import AIAgent


class AIAgentFactory(DocTypeFactory["AIAgent"]):
	doctype = "AI Agent"

	@classmethod
	def defaults(cls) -> dict[str, Any]:
		config = cls.get_test_master_data()
		provider = config["provider"]
		configured_model = config["model"]
		model = (
			configured_model
			if configured_model.startswith(f"{provider}:")
			else f"{provider}:{configured_model}"
		)
		read_tools = [
			"frappe_get_doctype_schema",
			"frappe_get_list",
			"frappe_get_doc",
			"frappe_get_count",
		]
		return {
			"agent_name": "Frappe Data Assistant",
			"agent_key": "frappe-data-assistant",
			"disabled": 0,
			"model": model,
			"provider_account": config.get("account_name") or f"AFAA Test - {provider}",
			"description": "A read-only assistant for answering questions about Frappe data.",
			"system_prompt": (
				"You are a careful Frappe data assistant. Base factual answers on tool results, "
				"respect the current user's permissions, and say when the available data is insufficient."
			),
			"timeout": 120,
			"retries": 2,
			"use_temperature": 1,
			"temperature": 0.2,
			"max_tokens": 1000,
			"tasks": [{"task": "answer-frappe-data-question"}],
			"skills": [{"skill": "frappe-data-reader"}],
			"allowed_tools": [{"tool": tool} for tool in read_tools],
		}
