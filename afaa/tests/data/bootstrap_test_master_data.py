from typing import Any

import frappe

from afaa.ai.provider import make_model_name, sync_provider_models
from afaa.ai.tools import sync_registered_tools
from afaa.tests.data.factories import (
	AIAgentFactory,
	AIModelFactory,
	AIProviderAccountFactory,
	AIProviderFactory,
	AISkillFactory,
	AITaskDefinitionFactory,
	DocTypeFactory,
)


class BootStrapTestMasterData:
	"""Create a small, reusable AFAA configuration for development and tests.

	The default records are local and deterministic, so the test suite never needs
	provider credentials or network access. Development sites may opt into provider
	model discovery with site configuration::

	    {
	        "afaa_test_master_data": {
	            "provider": "google",
	            "api_key": "...",
	            "account_settings": {},
	            "model": "gemini-2.5-flash",
	            "sync_models": true,
	        }
	    }
	"""

	def __init__(self) -> None:
		self.provider = ""
		self.provider_account = ""
		self.model = ""
		self.config: dict[str, Any] = {}

	def make(self) -> None:
		self.config = DocTypeFactory.get_test_master_data()
		self.make_provider_account()
		self.sync_tool()
		self.sync_model()
		self.make_skill()
		self.make_task_definition()
		self.make_agent()

		frappe.db.commit()  # nosemgrep

	# ---
	# Maker methods
	# ---

	def make_provider_account(self) -> None:
		provider_type = self.config["provider"]
		self.provider = provider_type
		AIProviderFactory.upsert(provider_type)

		account_name = self.config.get("account_name") or f"AFAA Test - {provider_type}"
		AIProviderAccountFactory.upsert(account_name)
		self.provider_account = account_name

	def sync_tool(self) -> None:
		sync_registered_tools()

	def sync_model(self) -> None:
		configured_model = self.config["model"]
		model_name = (
			configured_model
			if configured_model.startswith(f"{self.provider}:")
			else make_model_name(self.provider, configured_model)
		)

		if self.config.get("sync_models"):
			account = frappe.get_doc("AI Provider Account", self.provider_account)
			sync_provider_models(account)
		else:
			AIModelFactory.upsert(model_name)

		model = frappe.db.get_value("AI Model", model_name, ["disabled", "available"], as_dict=True)
		if not model or model.disabled or not model.available:
			frappe.throw(f"Configured AFAA test model {configured_model} is unavailable.")
		self.model = model_name

	def make_skill(self) -> None:
		AISkillFactory.upsert("frappe-data-reader")

	def make_task_definition(self) -> None:
		AITaskDefinitionFactory.upsert("answer-frappe-data-question")

	def make_agent(self) -> None:
		AIAgentFactory.upsert("frappe-data-assistant")


def bootstrap_test_master_data() -> None:
	BootStrapTestMasterData().make()
