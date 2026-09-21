# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from afaa.ai.provider.base_provider import BaseProvider, provider_with_base_url

ZAI_BASE_URL = "https://api.z.ai/api/paas/v4"


class ZaiProvider(BaseProvider):
	key = "zai"
	label = "Z.AI"
	required_distributions = ("openai",)
	supports_base_url_override = True

	def build_model(self, model_doc: Document):
		from pydantic_ai.models.zai import ZaiModel
		from pydantic_ai.providers.zai import ZaiProvider as PydanticZaiProvider

		provider = provider_with_base_url(
			PydanticZaiProvider, self.get_base_url(), api_key=self.get_api_key()
		)
		return ZaiModel(model_doc.model_id, provider=provider)

	def list_models(self) -> list[str]:
		from openai import OpenAI

		with OpenAI(api_key=self.get_api_key(), base_url=self.get_base_url() or ZAI_BASE_URL) as client:
			return [model.id for model in client.models.list().data if self.is_zai_chat_model(model.id)]

	def is_zai_chat_model(self, model_id: str) -> bool:
		# Z.AI exposes embedding and reranker models through the same catalog;
		# AFAA agents can only use chat-completion models.
		non_chat_markers = (
			"embedding",
			"reranker",
			"audio",
			"asr",
			"tts",
			"realtime",
			"video",
			"cogview",
		)
		return not any(marker in model_id.lower() for marker in non_chat_markers)
