# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

OUTPUT_FIELD_TYPES: dict[str, type[str] | type[int] | type[float] | type[bool]] = {
	"String": str,
	"Integer": int,
	"Number": float,
	"Boolean": bool,
}


class AIExecutionOutput(BaseModel):
	"""Strict base for every dynamically generated execution output."""

	model_config = ConfigDict(extra="forbid", strict=True)


def build_task_selection_model(task_keys: tuple[str, ...]) -> type[AIExecutionOutput]:
	if not task_keys:
		raise ValueError("At least one task is required to build a task selection model.")
	return create_model(
		"AITaskSelection",
		__base__=AIExecutionOutput,
		task_key=(
			Literal[task_keys],
			Field(description="The key of the task that best matches the request."),
		),
		reason=(str, Field(description="A concise reason for selecting this task.")),
	)


def build_task_output_model(task) -> type[AIExecutionOutput]:
	fields = {}
	for output_field in task.expected_output:
		python_type = OUTPUT_FIELD_TYPES[output_field.field_type]
		description = output_field.description or None
		if output_field.required:
			definition = (python_type, Field(description=description))
		else:
			definition = (python_type | None, Field(default=None, description=description))
		fields[output_field.field_name] = definition

	return create_model(
		get_task_output_model_name(task.key),
		__base__=AIExecutionOutput,
		**fields,
	)


def get_task_output_model_name(task_key: str) -> str:
	parts = (part for part in re.split(r"[^a-zA-Z0-9]+", task_key) if part)
	return "".join(part[:1].upper() + part[1:] for part in parts) + "Output"
