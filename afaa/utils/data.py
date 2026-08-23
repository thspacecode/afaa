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
