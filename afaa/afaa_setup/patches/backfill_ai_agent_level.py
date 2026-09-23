# Copyright (c) 2026, SpaceCode and contributors
# For license information, please see license.txt

import frappe

from afaa.ai.agent_levels import AGENT_LEVEL_WORKER


def execute():
	"""Backfill the Agent Level select for agents created before the hierarchy.

	Every existing agent becomes a Worker Agent (Level 1): supervisors stay
	selectable for legacy control-plane configurations until an administrator
	explicitly relabels them, and no existing thread binding changes meaning.
	"""
	frappe.db.sql(
		"update `tabAI Agent` set agent_level = %s where agent_level is null or agent_level = ''",
		(AGENT_LEVEL_WORKER,),
	)
