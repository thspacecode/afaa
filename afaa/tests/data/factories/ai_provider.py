from typing import TYPE_CHECKING, Any

from .base import DocTypeFactory

if TYPE_CHECKING:
	from afaa.afaa_setup.doctype.ai_provider.ai_provider import AIProvider


class AIProviderFactory(DocTypeFactory["AIProvider"]):
	doctype = "AI Provider"

	@classmethod
	def defaults(cls) -> dict[str, Any]:
		config = cls.get_test_master_data()
		return {
			"provider_type": config["provider"],
			"disabled": 0,
		}
