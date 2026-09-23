# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

"""Agent hierarchy levels shared by the AI Agent doctype and external runtimes.

The hierarchy has three levels:

- Level 0 — Supervisor Agent: durable task orchestration (control plane only).
- Level 1 — Worker Agent: primary coding/review agents; the only level that may
  be selected as a thread's main agent and the only level that may delegate.
- Level 2 — Sub Agent: scoped in-process delegates of exactly one Level 1 agent.
"""

from __future__ import annotations

AGENT_LEVEL_SUPERVISOR = "0"
AGENT_LEVEL_WORKER = "1"
AGENT_LEVEL_SUB_AGENT = "2"

AGENT_LEVELS = (AGENT_LEVEL_SUPERVISOR, AGENT_LEVEL_WORKER, AGENT_LEVEL_SUB_AGENT)

AGENT_LEVEL_LABELS = {
	AGENT_LEVEL_SUPERVISOR: "Supervisor Agent",
	AGENT_LEVEL_WORKER: "Worker Agent",
	AGENT_LEVEL_SUB_AGENT: "Sub Agent",
}

# Reserved tool keys implemented by Porch Agent's workspace runtime rather than
# by a registered AFAA tool. A Level 2 agent's Allowed Tools may reference these
# keys so an administrator can grant a sub-agent read/search/execute access to
# the parent run's workspace. Porch Agent maps each key to its own coding tool.
SUB_AGENT_RUNTIME_TOOL_KEYS = frozenset(
	{
		"read_file",
		"write_file",
		"edit_file",
		"grep",
		"glob",
		"execute",
	}
)

# Display metadata for the reserved runtime tools, mirrored from Porch Agent's
# coding toolset. The authoritative implementation always lives in Porch Agent;
# these records only make the keys selectable and describable inside AFAA.
RUNTIME_TOOL_SOURCE_APP = "porch_agent"


def _runtime_tool_definitions() -> dict[str, dict]:
	def object_schema(properties: dict, required: list[str]) -> dict:
		return {
			"type": "object",
			"properties": properties,
			"required": required,
			"additionalProperties": False,
		}

	path = {"type": "string", "description": "Workspace-relative or absolute container file path."}
	service = {
		"type": ["string", "null"],
		"description": "Authorized workspace service name. Omit to use the default.",
	}
	text = {"type": "string"}
	return {
		"read_file": {
			"tool_name": "read_file",
			"description": "Read a text file from the project workspace.",
			"method": "porch_agent.runtime.read_file",
			"input_schema": object_schema(
				{
					"path": path,
					"offset": {"type": "integer", "description": "First one-based line to return."},
					"limit": {"type": "integer", "description": "Maximum number of lines to return."},
					"service": service,
				},
				["path"],
			),
			"output_schema": {"type": "string"},
		},
		"write_file": {
			"tool_name": "write_file",
			"description": "Create or replace a text file in the project workspace.",
			"method": "porch_agent.runtime.write_file",
			"input_schema": object_schema(
				{
					"path": path,
					"content": {"type": "string", "description": "Complete replacement file content."},
					"service": service,
				},
				["path", "content"],
			),
			"output_schema": {
				"type": "object",
				"properties": {"path": {"type": "string"}, "bytesWritten": {"type": "integer"}},
			},
		},
		"edit_file": {
			"tool_name": "edit_file",
			"description": "Replace one exact, unique text block in a workspace file.",
			"method": "porch_agent.runtime.edit_file",
			"input_schema": object_schema(
				{
					"path": path,
					"old_text": {"type": "string", "description": "Exact text that must occur once."},
					"new_text": {"type": "string", "description": "Replacement text."},
					"service": service,
				},
				["path", "old_text", "new_text"],
			),
			"output_schema": {
				"type": "object",
				"properties": {"path": {"type": "string"}, "replacements": {"type": "integer"}},
			},
		},
		"grep": {
			"tool_name": "grep",
			"description": "Recursively search project files using a regular expression.",
			"method": "porch_agent.runtime.grep",
			"input_schema": object_schema(
				{
					"pattern": {"type": "string", "description": "grep-compatible regular expression."},
					"path": {
						"type": "string",
						"description": "Workspace-relative or absolute container path to search.",
					},
					"include": {"type": ["string", "null"], "description": "Optional filename glob."},
					"max_results": {"type": "integer", "description": "Maximum matching lines."},
					"service": service,
				},
				["pattern"],
			),
			"output_schema": {"type": "string"},
		},
		"glob": {
			"tool_name": "glob",
			"description": "List project files matching a glob pattern.",
			"method": "porch_agent.runtime.glob",
			"input_schema": object_schema(
				{
					"pattern": {"type": "string", "description": "File pattern such as '**/*.py'."},
					"max_results": {"type": "integer", "description": "Maximum paths to return."},
					"service": service,
				},
				["pattern"],
			),
			"output_schema": {"type": "string"},
		},
		"execute": {
			"tool_name": "execute",
			"description": "Execute a shell command inside the project's agent service.",
			"method": "porch_agent.runtime.execute",
			"input_schema": object_schema(
				{
					"command": {"type": "string", "description": "Shell command to run."},
					"timeout_seconds": {"type": ["number", "null"]},
					"service": service,
					"output_limit": {"type": "integer", "description": "Characters per output stream."},
				},
				["command"],
			),
			"output_schema": {
				"type": "object",
				"properties": {
					"exitCode": {"type": "integer"},
					"stdout": {"type": "string"},
					"stderr": {"type": "string"},
					"stdoutTruncated": {"type": "boolean"},
					"stderrTruncated": {"type": "boolean"},
				},
			},
		},
	}


def runtime_tool_definition(tool_key: str) -> dict | None:
	"""Return the reserved runtime tool record values, or ``None``."""
	if tool_key not in SUB_AGENT_RUNTIME_TOOL_KEYS:
		return None
	return _runtime_tool_definitions()[tool_key]



def agent_level_number(level: str | None) -> int:
	"""Return the numeric agent level, defaulting to Worker for legacy rows."""
	value = (level or AGENT_LEVEL_WORKER).strip()
	if value not in AGENT_LEVELS:
		return int(AGENT_LEVEL_WORKER)
	return int(value)


def agent_level_label(level: str | None) -> str:
	return AGENT_LEVEL_LABELS.get((level or "").strip(), AGENT_LEVEL_LABELS[AGENT_LEVEL_WORKER])
