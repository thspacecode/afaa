# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import json
from collections.abc import Callable
from typing import Any

import frappe
from frappe import _
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, Tool

from afaa.ai.outputs import build_task_output_model, build_task_selection_model
from afaa.ai.prompts import render_task_instructions
from afaa.ai.provider import build_model
from afaa.ai.runtime import ResolvedTask, resolve_ai_agent


class AIExecutionResult(BaseModel):
	model_config = ConfigDict(frozen=True)

	task: str | None
	output_type: str
	output_text: str | None
	output_json: dict[str, Any] | None
	configuration_snapshot: dict[str, Any]
	message_history: list[dict[str, Any]]
	request_count: int
	tool_call_count: int
	input_tokens: int
	output_tokens: int
	total_tokens: int


def run_ai_agent(
	agent_name: str,
	input_text: str,
	context=None,
	*,
	task_name: str | None = None,
	on_task_determined: Callable[[str], None] | None = None,
) -> AIExecutionResult:
	"""Route and execute one configured AI Agent request as the current Frappe user."""
	resolved = resolve_ai_agent(agent_name, context)
	if not resolved.tasks:
		frappe.throw(_("AI Agent {0} has no tasks configured.").format(frappe.bold(agent_name)))

	model_doc = frappe.get_doc("AI Model", resolved.model.name)
	provider_account_doc = frappe.get_doc("AI Provider Account", resolved.model.provider_account)
	model = build_model(model_doc, provider_account_doc)
	model_settings = dict(resolved.model.settings)
	model_settings.setdefault("timeout", resolved.timeout)
	configuration_snapshot = resolved.model_dump(mode="json")
	results = []

	if task_name:
		selected_task = next(
			(task for task in resolved.tasks if task.document_name == task_name),
			None,
		)
		if not selected_task:
			frappe.throw(_("Task {0} is not available to this AI Agent.").format(frappe.bold(task_name)))
		configuration_snapshot["routing_selection"] = {
			"task_key": selected_task.key,
			"reason": "Task Definition selected on AI Task.",
		}
	else:
		routing_model = build_task_selection_model(tuple(task.key for task in resolved.tasks))
		routing_agent = Agent(
			model=model,
			output_type=[routing_model, str],
			instructions=[resolved.prompt, _get_routing_instructions(resolved.tasks)],
			model_settings=model_settings,
			retries=resolved.retries,
		)
		routing_result = routing_agent.run_sync(input_text)
		results.append(("routing", routing_result))
		configuration_snapshot["routing_output_schema"] = routing_model.model_json_schema()

		if isinstance(routing_result.output, str):
			configuration_snapshot["routing_selection"] = None
			configuration_snapshot["selected_task"] = None
			return _build_result(
				task=None,
				output_type="str",
				output_text=routing_result.output,
				output_json=None,
				configuration_snapshot=configuration_snapshot,
				results=results,
			)

		selected_task = next(
			(task for task in resolved.tasks if task.key == routing_result.output.task_key),
			None,
		)
		if not selected_task:
			frappe.throw(_("The routing response selected an unavailable task."))
		configuration_snapshot["routing_selection"] = routing_result.output.model_dump(mode="json")
		if on_task_determined:
			on_task_determined(selected_task.document_name)

	tools = [
		Tool(
			frappe.get_attr(tool.method),
			name=tool.key,
			description=tool.description,
			sequential=True,
		)
		for tool in resolved.tools
	]
	task_output_model = build_task_output_model(selected_task)
	selected_task_instructions = render_task_instructions(selected_task.instructions, context)
	execution_agent = Agent(
		model=model,
		output_type=[task_output_model, str],
		instructions=[
			resolved.prompt,
			*(skill.instructions for skill in resolved.skills),
			selected_task_instructions,
		],
		model_settings=model_settings,
		retries=resolved.retries,
		tools=tools,
	)
	execution_result = execution_agent.run_sync(input_text)
	results.append(("execution", execution_result))

	selected_task_snapshot = selected_task.model_dump(mode="json")
	selected_task_snapshot["instructions"] = selected_task_instructions
	configuration_snapshot["selected_task"] = selected_task_snapshot
	configuration_snapshot["execution_output_schema"] = task_output_model.model_json_schema()
	if isinstance(execution_result.output, str):
		return _build_result(
			task=selected_task.document_name,
			output_type="str",
			output_text=execution_result.output,
			output_json=None,
			configuration_snapshot=configuration_snapshot,
			results=results,
		)

	return _build_result(
		task=selected_task.document_name,
		output_type=task_output_model.__name__,
		output_text=None,
		output_json=execution_result.output.model_dump(mode="json"),
		configuration_snapshot=configuration_snapshot,
		results=results,
	)


def _get_routing_instructions(tasks: tuple[ResolvedTask, ...]) -> str:
	task_descriptions = "\n".join(f"- {task.key}: {task.description}" for task in tasks)
	return (
		"Determine which configured task best matches the user's request. "
		"Return the structured task selection when one task matches. "
		"If the request is ambiguous or no task matches, return a concise plain-text clarification question. "
		"Do not attempt the task during this routing step.\n\n"
		f"Available tasks:\n{task_descriptions}"
	)


def _build_result(
	*,
	task: str | None,
	output_type: str,
	output_text: str | None,
	output_json: dict[str, Any] | None,
	configuration_snapshot: dict[str, Any],
	results: list[tuple[str, Any]],
) -> AIExecutionResult:
	usage = [result.usage for _, result in results]
	return AIExecutionResult(
		task=task,
		output_type=output_type,
		output_text=output_text,
		output_json=output_json,
		configuration_snapshot=configuration_snapshot,
		message_history=[
			{
				"stage": stage,
				"messages": json.loads(result.all_messages_json()),
			}
			for stage, result in results
		],
		request_count=sum(item.requests for item in usage),
		tool_call_count=sum(item.tool_calls for item in usage),
		input_tokens=sum(item.input_tokens for item in usage),
		output_tokens=sum(item.output_tokens for item in usage),
		total_tokens=sum(item.total_tokens for item in usage),
	)
