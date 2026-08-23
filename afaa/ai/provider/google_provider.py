# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from afaa.ai.provider.base_provider import BaseProvider


class GoogleProvider(BaseProvider):
	key = "google"
	label = "Google"
	required_distributions = ("google-genai",)

	def build_model(self, model_doc: Document):
		from pydantic_ai.models.google import GoogleModel
		from pydantic_ai.providers.google import GoogleProvider as PydanticGoogleProvider

		provider = PydanticGoogleProvider(api_key=self.get_api_key())
		return GoogleModel(model_doc.model_id, provider=provider)

	def list_models(self) -> list[str]:
		from google import genai

		client = genai.Client(api_key=self.get_api_key())
		try:
			return [
				model.name.removeprefix("models/")
				for model in client.models.list()
				if "generateContent" in (model.supported_actions or [])
			]
		finally:
			client.close()
