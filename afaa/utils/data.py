# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _

SUB_AGENT_TIMEOUT_SECONDS_LIMIT = 86400


def parse_bounded_seconds(value: object, *, label: str, unset: int | None = 0) -> int | None:
	"""Losslessly parse a whole-number seconds field; blank or zero to ``unset``.

	Accepts the lossless representations Frappe desks, REST payloads, and
	database columns produce (actual ints, integer strings, integral floats).
	Anything lossy or out of bounds - fractions, bools, negatives, values above
	:data:`SUB_AGENT_TIMEOUT_SECONDS_LIMIT` - fails closed.
	"""
	if value is None or (isinstance(value, str) and not value.strip()):
		return unset
	if isinstance(value, bool) or not isinstance(value, (int, float, str)):
		frappe.throw(_("{0} must be a whole number of seconds.").format(label), frappe.ValidationError)
	if isinstance(value, float):
		if not value.is_integer():
			frappe.throw(_("{0} must be a whole number of seconds.").format(label), frappe.ValidationError)
		value = int(value)
	elif isinstance(value, str):
		text = value.strip()
		if not re.fullmatch(r"[+-]?[0-9]+", text):
			frappe.throw(_("{0} must be a whole number of seconds.").format(label), frappe.ValidationError)
		value = int(text)
	if value < 0:
		frappe.throw(_("{0} cannot be negative.").format(label), frappe.ValidationError)
	if value == 0:
		return unset
	if value > SUB_AGENT_TIMEOUT_SECONDS_LIMIT:
		frappe.throw(
			_("{0} must be at most {1} seconds.").format(label, SUB_AGENT_TIMEOUT_SECONDS_LIMIT),
			frappe.ValidationError,
		)
	return value


def validate_deadline_order(
	soft_seconds: int, timeout_seconds: int | float | None, *, soft_label: str, timeout_label: str
) -> None:
	"""Require a set hard deadline and keep the soft deadline strictly below it."""
	if not timeout_seconds or timeout_seconds <= 0:
		frappe.throw(
			_("{0} requires {1} to be set.").format(soft_label, timeout_label), frappe.ValidationError
		)
	if soft_seconds >= timeout_seconds:
		frappe.throw(
			_("{0} must be less than {1}.").format(soft_label, timeout_label), frappe.ValidationError
		)


def validate_key(value: str | None, label: str) -> None:
	key_pattern = re.compile(r"^[a-z][a-z0-9_-]{2,49}$")
	if not key_pattern.fullmatch(value or ""):
		frappe.throw(
			_(
				"{0} must be 3-50 characters using lowercase letters, numbers, underscores, or hyphens."
			).format(label)
		)


def validate_unique_rows(rows, link_field: str, label: str) -> None:
	"""Reject duplicate link values inside one child-table field."""
	seen = set()
	for row in rows or []:
		value = row.get(link_field)
		if value in seen:
			frappe.throw(_("{0} {1} is listed more than once.").format(label, frappe.bold(value)))
		seen.add(value)
