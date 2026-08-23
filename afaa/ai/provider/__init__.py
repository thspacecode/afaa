# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

from importlib.metadata import PackageNotFoundError, version
from inspect import isabstract
from typing import TYPE_CHECKING, Any

import frappe
from frappe import _

from afaa.ai.provider.base_provider import BaseProvider
from afaa.ai.provider.google_provider import GoogleProvider
from afaa.ai.provider.openai_provider import OpenAIProvider

if TYPE_CHECKING:
	from afaa.afaa_setup.doctype.ai_model.ai_model import AIModel
	from afaa.afaa_setup.doctype.ai_provider_account.ai_provider_account import AIProviderAccount

BUILTIN_PROVIDERS = (OpenAIProvider, GoogleProvider)


def is_distribution_installed(distribution: str) -> bool:
	try:
		version(distribution)
		return True
	except PackageNotFoundError:
		return False


def is_provider_available(provider_class: type[BaseProvider]) -> bool:
	try:
		_validate_provider_class(provider_class)
		return all(
			is_distribution_installed(distribution) for distribution in provider_class.required_distributions
		)
	except (AttributeError, ImportError, ModuleNotFoundError, TypeError):
		return False


def get_provider_classes(*, available_only: bool = False) -> dict[str, type[BaseProvider]]:
	providers = {provider_class.key: provider_class for provider_class in BUILTIN_PROVIDERS}

	for hook_path in frappe.get_hooks("afaa_ai_providers"):
		try:
			provider_class = _validate_provider_class(frappe.get_attr(hook_path), hook_path)
			providers[provider_class.key] = provider_class
		except Exception:
			frappe.log_error(
				title=_("Unable to load AFAA provider hook"),
				message=frappe.get_traceback(),
			)

	if available_only:
		return {key: value for key, value in providers.items() if is_provider_available(value)}

	return providers


def get_provider_class(provider_type: str, *, require_available: bool = True) -> type[BaseProvider]:
	provider_class = get_provider_classes().get(provider_type)
	if not provider_class:
		frappe.throw(_("AI provider type {0} is not registered.").format(frappe.bold(provider_type)))

	if require_available and not is_provider_available(provider_class):
		missing = [
			distribution
			for distribution in provider_class.required_distributions
			if not is_distribution_installed(distribution)
		]
		frappe.throw(
			_("AI provider {0} is unavailable. Missing Python distributions: {1}").format(
				frappe.bold(provider_class.label),
				", ".join(missing) or _("provider adapter"),
			)
		)

	return provider_class


@frappe.whitelist()
def get_available_provider_types() -> list[dict[str, Any]]:
	return [
		{
			"value": provider_class.key,
			"label": provider_class.label,
			"source_app": _get_source_app(provider_class),
			"required_distributions": list(provider_class.required_distributions),
		}
		for provider_class in get_provider_classes(available_only=True).values()
	]


def get_provider_account(provider_name: str, account_name: str | None = None) -> "AIProviderAccount":
	if account_name:
		account = frappe.get_doc("AI Provider Account", account_name)
		if account.provider != provider_name:
			frappe.throw(
				_("AI Provider Account {0} does not belong to provider {1}.").format(
					frappe.bold(account.name), frappe.bold(provider_name)
				)
			)
		if account.disabled:
			frappe.throw(_("AI Provider Account {0} is disabled.").format(frappe.bold(account.name)))
		return account

	accounts = frappe.get_all(
		"AI Provider Account",
		filters={"provider": provider_name, "disabled": 0},
		pluck="name",
		limit_page_length=2,
	)
	if not accounts:
		frappe.throw(
			_("Configure an enabled AI Provider Account for provider {0}.").format(frappe.bold(provider_name))
		)
	if len(accounts) > 1:
		frappe.throw(_("Select an AI Provider Account for provider {0}.").format(frappe.bold(provider_name)))
	return frappe.get_doc("AI Provider Account", accounts[0])


def build_model(model_doc: "AIModel", provider_account_doc: "AIProviderAccount | None" = None):
	provider_account_doc = provider_account_doc or get_provider_account(model_doc.provider)
	provider_doc = frappe.get_doc("AI Provider", provider_account_doc.provider)
	if provider_doc.disabled:
		frappe.throw(_("AI Provider {0} is disabled.").format(frappe.bold(provider_doc.name)))
	if provider_account_doc.disabled:
		frappe.throw(_("AI Provider Account {0} is disabled.").format(frappe.bold(provider_account_doc.name)))
	if provider_doc.name != model_doc.provider:
		frappe.throw(
			_("AI Provider Account {0} does not match AI Model {1}.").format(
				frappe.bold(provider_account_doc.name), frappe.bold(model_doc.name)
			)
		)
	provider = get_provider_class(provider_doc.provider_type)(provider_account_doc)
	return provider.build_model(model_doc)


def list_provider_models(provider_account_doc: "AIProviderAccount") -> list[str]:
	provider_doc = frappe.get_doc("AI Provider", provider_account_doc.provider)
	provider = get_provider_class(provider_doc.provider_type)(provider_account_doc)
	model_ids = provider.list_models()
	return sorted(
		{str(model_id).strip() for model_id in model_ids if model_id is not None and str(model_id).strip()}
	)


def sync_provider_models(provider_account_doc: "AIProviderAccount") -> dict[str, list[str]]:
	model_ids = list_provider_models(provider_account_doc)
	available_model_ids = set(model_ids)
	existing = {
		row.model_id: row.name
		for row in frappe.get_all(
			"AI Model",
			filters={"provider": provider_account_doc.provider},
			fields=["name", "model_id"],
		)
	}
	report = {"created": [], "updated": [], "unavailable": []}

	for model_id, name in existing.items():
		if model_id not in available_model_ids:
			frappe.db.set_value(
				"AI Model",
				name,
				{"available": 0, "disabled": 1},
				update_modified=False,
			)
			report["unavailable"].append(model_id)

	for model_id in model_ids:
		if name := existing.get(model_id):
			frappe.db.set_value("AI Model", name, "available", 1, update_modified=False)
			report["updated"].append(model_id)
			continue

		frappe.get_doc(
			{
				"doctype": "AI Model",
				"model_name": make_model_name(provider_account_doc.provider, model_id),
				"provider": provider_account_doc.provider,
				"model_id": model_id,
				"disabled": 0,
				"available": 1,
			}
		).insert(ignore_permissions=True)
		report["created"].append(model_id)

	return report


def as_public_dict(provider_class: type[BaseProvider]) -> dict[str, Any]:
	return {
		"key": provider_class.key,
		"label": provider_class.label,
		"required_distributions": list(provider_class.required_distributions),
		"source_app": _get_source_app(provider_class),
		"available": is_provider_available(provider_class),
	}


def _validate_provider_class(provider_class, hook_path: str | None = None) -> type[BaseProvider]:
	path = hook_path or repr(provider_class)
	if (
		not isinstance(provider_class, type)
		or not issubclass(provider_class, BaseProvider)
		or isabstract(provider_class)
	):
		raise TypeError(f"{path} must be a concrete BaseProvider subclass")
	if not getattr(provider_class, "key", None) or not getattr(provider_class, "label", None):
		raise TypeError(f"{path} must define key and label")
	return provider_class


def _get_source_app(provider_class: type[BaseProvider]) -> str:
	return provider_class.source_app or provider_class.__module__.split(".", 1)[0]


def make_model_name(provider_name: str, model_id: str) -> str:
	return f"{provider_name}:{model_id}"
