import json
from typing import TYPE_CHECKING, Any

from .base import DocTypeFactory

if TYPE_CHECKING:
	from afaa.afaa_setup.doctype.ai_provider_account.ai_provider_account import AIProviderAccount


class AIProviderAccountFactory(DocTypeFactory["AIProviderAccount"]):
	doctype = "AI Provider Account"
	password_fields = ("api_key",)

	@classmethod
	def defaults(cls) -> dict[str, Any]:
		config = cls.get_test_master_data()
		provider = config["provider"]
		account_settings = config.get("account_settings")
		return {
			"account_name": config.get("account_name") or f"AFAA Test - {provider}",
			"provider": provider,
			"disabled": 0,
			"account_settings": (
				json.dumps(account_settings, indent=2, sort_keys=True)
				if account_settings is not None
				else None
			),
			"api_key": config.get("api_key"),
		}
