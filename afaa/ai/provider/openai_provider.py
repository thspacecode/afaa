# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from afaa.ai.provider.base_provider import BaseProvider


class OpenAIProvider(BaseProvider):
	key = "openai"
	label = "OpenAI"
	required_distributions = ("openai",)

	def build_model(self, model_doc: Document):
		from pydantic_ai.models.openai import OpenAIChatModel
		from pydantic_ai.providers.openai import OpenAIProvider as PydanticOpenAIProvider

		provider = PydanticOpenAIProvider(api_key=self.get_api_key())
		return OpenAIChatModel(model_doc.model_id, provider=provider)

	def list_models(self) -> list[str]:
		from openai import OpenAI

		with OpenAI(api_key=self.get_api_key()) as client:
			return [model.id for model in client.models.list().data if self.is_openai_chat_model(model.id)]

	def is_openai_chat_model(self, model_id: str) -> bool:
		non_chat_markers = (
			"audio",
			"babbage",
			"dall-e",
			"davinci",
			"embedding",
			"image",
			"moderation",
			"realtime",
			"search",
			"transcribe",
			"tts",
			"whisper",
		)
		return not any(marker in model_id.lower() for marker in non_chat_markers)
