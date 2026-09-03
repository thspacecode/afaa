# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from typing import TYPE_CHECKING, Any

import frappe
import requests
from frappe import _
from frappe.model.document import Document

from afaa import __version__
from afaa.ai.oauth.openai_codex import CONNECTED_APP_NAME
from afaa.ai.provider.base_provider import BaseProvider

if TYPE_CHECKING:
	from afaa.ai.oauth.openai_codex_service import CodexTokenResult

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_CLIENT_VERSION = __version__


class OpenAICodexProvider(BaseProvider):
	key = "openai_codex"
	label = "OpenAI Codex Subscription"
	required_distributions = ("openai",)
	supported_auth_methods = ("OAuth Subscription",)
	supports_oauth_connection = True
	model_catalog_is_account_specific = True

	def validate_account(self) -> None:
		super().validate_account()
		if self.provider_account_doc.connected_app != CONNECTED_APP_NAME:
			frappe.throw(
				_("OpenAI Codex accounts must use Connected App {0}.").format(frappe.bold(CONNECTED_APP_NAME))
			)

	def is_authenticated(self) -> bool:
		if self.provider_account_doc.oauth_status != "Connected":
			return False
		if not self.provider_account_doc.connected_user:
			return False
		if not frappe.db.exists("Connected App", CONNECTED_APP_NAME):
			return False
		token_cache = frappe.get_doc("Connected App", CONNECTED_APP_NAME).get_token_cache(
			self.provider_account_doc.connected_user
		)
		return bool(token_cache and token_cache.get_password("access_token", raise_exception=False))

	def get_authentication_status(self) -> str:
		status = self.provider_account_doc.oauth_status or "Not Connected"
		if status == "Connected" and not self.is_authenticated():
			return "Expired"
		return status

	def resolve_access_token_result(
		self, minimum_validity: int = 300, *, force_refresh: bool = False
	) -> "CodexTokenResult":
		"""Return the typed token interface used by trusted credential brokers."""
		from afaa.ai.oauth.openai_codex import resolve_codex_access_token

		return resolve_codex_access_token(
			self.provider_account_doc,
			minimum_validity,
			force_refresh=force_refresh,
		)

	def resolve_access_token(self) -> str:
		return self.resolve_access_token_result().access_token.get_secret_value()

	def disconnect(self) -> None:
		from afaa.ai.oauth.openai_codex import disconnect_oauth

		disconnect_oauth(self.provider_account_doc)

	def prepare_model_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
		from afaa.ai.provider.openai_codex_transport import force_codex_model_settings

		return force_codex_model_settings(settings)

	def build_model(self, model_doc: Document):
		from openai import AsyncOpenAI
		from pydantic_ai.providers.openai import OpenAIProvider as PydanticOpenAIProvider

		from afaa.ai.provider.openai_codex_transport import OpenAICodexResponsesModel

		account_id = self.get_account_id()

		# Resolve once so model construction fails early. The async SDK callback then
		# resolves again before every HTTP request, including tool and retry turns.
		self.resolve_access_token()

		async def resolve_access_token() -> str:
			# This stays on the request thread/event loop, preserving Frappe's local DB
			# context rather than moving synchronous Token Cache access to an executor.
			return self.resolve_access_token()

		client = AsyncOpenAI(
			api_key=resolve_access_token,
			base_url=CODEX_BASE_URL,
			default_headers={
				"chatgpt-account-id": account_id,
				"originator": "afaa",
				"OpenAI-Beta": "responses=experimental",
			},
		)
		provider = PydanticOpenAIProvider(openai_client=client)
		return OpenAICodexResponsesModel(
			model_doc.model_id,
			provider=provider,
			settings={"openai_store": False},
			on_authentication_expired=self.mark_authentication_expired,
		)

	def list_models(self) -> list[str]:
		account_id = self.get_account_id()
		access_token = self.resolve_access_token()
		try:
			response = requests.get(
				f"{CODEX_BASE_URL}/models",
				params={"client_version": CODEX_CLIENT_VERSION},
				headers={
					"Authorization": f"Bearer {access_token}",
					"chatgpt-account-id": account_id,
					"originator": "afaa",
				},
				timeout=30,
			)
		except requests.RequestException:
			frappe.throw(_("Unable to retrieve the OpenAI Codex model catalog."))

		if not response.ok:
			from afaa.ai.provider.openai_codex_transport import raise_codex_provider_error

			raise_codex_provider_error(response.status_code, self.mark_authentication_expired)

		try:
			payload = response.json()
		except ValueError:
			frappe.throw(_("OpenAI Codex returned an invalid model catalog."))
		if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
			frappe.throw(_("OpenAI Codex returned an invalid model catalog."))

		return [
			model["slug"].strip()
			for model in payload["models"]
			if isinstance(model, dict)
			and model.get("supported_in_api") is True
			and model.get("visibility") == "list"
			and isinstance(model.get("slug"), str)
			and model["slug"].strip()
		]

	def get_account_id(self) -> str:
		account_id = (self.provider_account_doc.external_account_id or "").strip()
		if not account_id:
			frappe.throw(_("Unable to determine ChatGPT account."))
		return account_id

	def mark_authentication_expired(self) -> None:
		from afaa.ai.oauth.openai_codex import report_codex_authentication_failure

		report_codex_authentication_failure(self.provider_account_doc)
