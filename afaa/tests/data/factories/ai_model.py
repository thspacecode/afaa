from typing import TYPE_CHECKING, Any

from .base import DocTypeFactory

if TYPE_CHECKING:
	from afaa.afaa_setup.doctype.ai_model.ai_model import AIModel


class AIModelFactory(DocTypeFactory["AIModel"]):
	doctype = "AI Model"

	@classmethod
	def defaults(cls) -> dict[str, Any]:
		config = cls.get_test_master_data()
		provider = config["provider"]
		return {
			"provider": provider,
			"model_id": config["model"].removeprefix(f"{provider}:"),
			"disabled": 0,
			"available": 1,
			"supports_tools": 1,
		}
