# AFAA

A thin, extensible AI configuration and execution layer for Frappe.

Phase 1 provides provider, model, prompt, tool, skill, and agent configuration. Email, Raven, AI Tasks, OCR, chat, and RAG are intentionally outside this phase.

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/thspacecode/afaa.git --branch version-16
bench --site $SITE install-app afaa
```

AFAA installs `pydantic-ai-slim` with the Google and OpenAI provider SDKs by default.

## Extending providers

OpenAI and Google adapters are built in. Another app can register a custom provider:

```python
# hooks.py
required_apps = ["afaa"]
afaa_ai_providers = ["my_app.ai.providers.get_provider_definitions"]
```

The hook returns one or more definitions:

```python
def get_provider_definitions():
    return {
        "key": "custom",
        "label": "Custom Provider",
        "required_distributions": ["custom-provider-sdk"],
        "factory": "my_app.ai.providers.build_model",
    }
```

Custom definitions are merged with the built-in providers by `key`; an extension does not need to return the OpenAI or Google definitions. A new key adds a provider, while using an existing key such as `openai` replaces that built-in definition. If multiple apps register the same key, the last loaded definition wins, so extension apps should use unique keys unless they intentionally override a provider.

The factory receives the `AI Model` and `AI Provider` documents and returns a Pydantic AI model instance.

## Extending tools

Tools are code-registered so users cannot execute arbitrary dotted Python paths. Define decorated tools in your app's `ai.tools` module; AFAA discovers that module automatically:

```python
# my_app/ai/tools.py
import afaa


@afaa.tool(
    name="Look Up Order",
    description="Find an order by its identifier.",
)
def lookup_order(order_id: str):
    ...
```

The key defaults to the function name, the dotted method path is derived automatically, and the input schema is generated from the function's type annotations. Pass `key=` or `input_schema=` to override those defaults. A function docstring can be used instead of `description=`.

### Module-level tool requirement

A decorated tool must be defined directly in a Python module—not inside another function or on a class. AFAA stores the tool as a stable dotted path such as `my_app.ai.tools.lookup_order`, then uses that path to import and execute the function later. Nested functions and class methods do not have the supported module-level import path and may depend on captured or instance state, so the decorator rejects them during import.

```python
# Supported: defined directly in the module
@afaa.tool(name="Look Up Order", description="Find an order.")
def lookup_order(order_id: str):
    ...


# Not supported: nested function
def build_tools():
    @afaa.tool(name="Look Up Order", description="Find an order.")
    def lookup_order(order_id: str):
        ...


# Not supported: instance, class, or static method
class OrderTools:
    @afaa.tool(name="Look Up Order", description="Find an order.")
    def lookup_order(self, order_id: str):
        ...
```

Tools may be organized in other modules as long as each function remains module-level and is re-exported from the app's `ai.tools` discovery module:

```python
# my_app/ai/tools.py
from my_app.orders.tools import lookup_order
```

Registered tools synchronize after migration and start disabled. An AI Manager must explicitly enable them and allow them on an AI Agent.

### Built-in Frappe tools

AFAA registers these permission-aware tools:

- Read-only: `frappe_get_doctype_schema`, `frappe_get_list`, `frappe_get_doc`, and `frappe_get_count`
- Mutation: `frappe_create_doc`, `frappe_update_doc`, `frappe_delete_doc`, `frappe_submit_doc`, and `frappe_cancel_doc`

The tools execute as the current Frappe user and use the Document and permission-aware list APIs. They do not use `ignore_permissions`, and list responses are limited to 100 rows per call. Mutation tools are independently disabled by default so administrators can grant only the operations an agent needs.

## Development

```bash
cd apps/afaa
pre-commit run --all-files
bench --site $SITE run-tests --app afaa
```

## License

GNU Affero General Public License v3.0 (AGPL-3.0). See [license.txt](license.txt).
