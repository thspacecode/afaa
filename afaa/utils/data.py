# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _


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
