# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from afaa.ai.provider.base_provider import BaseProvider, provider_with_base_url

MOONSHOT_BASE_URL = "https://api.moonshot.ai/v1"


class MoonshotProvider(BaseProvider):
	key = "moonshot"
	label = "Moonshot (Kimi)"
	required_distributions = ("openai",)
	supports_base_url_override = True

	def build_model(self, model_doc: Document):
		from pydantic_ai.models.openai import OpenAIChatModel
		from pydantic_ai.providers.moonshotai import MoonshotAIProvider

		provider = provider_with_base_url(
			MoonshotAIProvider, self.get_base_url(), api_key=self.get_api_key()
		)
		return OpenAIChatModel(model_doc.model_id, provider=provider)

	def list_models(self) -> list[str]:
		from openai import OpenAI

		with OpenAI(api_key=self.get_api_key(), base_url=self.get_base_url() or MOONSHOT_BASE_URL) as client:
			return [model.id for model in client.models.list().data]
