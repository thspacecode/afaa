// Copyright (c) 2026, SpaceCode and contributors
// For license information, please see license.txt

const WORKER_LEVEL = "1";
const SUB_AGENT_LEVEL = "2";

function load_taken_sub_agents(frm) {
	// Sub-agents belong to exactly one parent: exclude every Level 2 agent
	// already referenced by another AI Agent from the picker.
	return frappe.db
		.get_list("AI Agent Sub Agent", {
			filters: { parenttype: "AI Agent" },
			fields: ["sub_agent", "parent"],
			limit_page_length: 0,
		})
		.then((rows) => {
			frm.taken_sub_agents = rows
				.filter((row) => row.parent !== frm.doc.name)
				.map((row) => row.sub_agent)
				.filter(Boolean);
		})
		.catch(() => {
			frm.taken_sub_agents = [];
		});
}

frappe.ui.form.on("AI Agent", {
	setup(frm) {
		frm.set_query("model", () => ({ filters: { available: 1 } }));
		frm.set_query("provider_account", () => ({
			filters: { provider: frm.doc.provider || "" },
		}));
		// Runtime workspace tools (provisioned from Porch Agent) and Frappe proxy
		// tools are both selectable; disabled/unavailable ones never are.
		frm.set_query("tool", "allowed_tools", () => ({
			filters: { disabled: 0, available: 1 },
		}));
		frm.set_query("sub_agent", "sub_agents", () => {
			const filters = [
				["agent_level", "=", SUB_AGENT_LEVEL],
				["disabled", "=", 0],
			];
			if (frm.doc.name) {
				filters.push(["name", "!=", frm.doc.name]);
			}
			const taken = frm.taken_sub_agents || [];
			if (taken.length) {
				filters.push(["name", "not in", taken]);
			}
			return { filters };
		});
	},

	onload(frm) {
		frm.last_agent_level = frm.doc.agent_level || WORKER_LEVEL;
	},

	refresh(frm) {
		frm.last_agent_level = frm.doc.agent_level || WORKER_LEVEL;
		void load_taken_sub_agents(frm);

		if (frm.is_new()) return;

		frm.add_custom_button(__("Resolve Agent"), () => {
			const dialog = new frappe.ui.Dialog({
				title: __("Resolve AI Agent"),
				fields: [
					{
						fieldname: "context",
						label: __("Context"),
						fieldtype: "JSON",
						default: "{}",
					},
				],
				primary_action_label: __("Resolve"),
				primary_action(values) {
					frappe
						.call("afaa.ai.runtime.get_resolved_ai_agent", {
							agent_name: frm.doc.name,
							context: values.context,
						})
						.then(({ message }) => {
							frappe.msgprint({
								title: __("Resolved AI Agent"),
								message: `<pre>${frappe.utils.escape_html(
									JSON.stringify(message, null, 2)
								)}</pre>`,
								wide: true,
							});
						});
				},
			});
			dialog.show();
		});
	},

	agent_level(frm) {
		const level = frm.doc.agent_level || WORKER_LEVEL;
		if (level === WORKER_LEVEL) {
			frm.last_agent_level = level;
			return;
		}
		if (!(frm.doc.sub_agents || []).length) {
			frm.last_agent_level = level;
			return;
		}
		// Only Worker Agents may delegate: mirror the server-side rejection and
		// let the operator either drop the links or keep the Worker level.
		frappe.confirm(
			__("Only Worker Agents may configure Sub Agents. Clear the Sub Agents table?"),
			() => {
				frm.clear_table("sub_agents");
				frm.refresh_field("sub_agents");
				frm.last_agent_level = level;
			},
			() => {
				frm.set_value("agent_level", frm.last_agent_level || WORKER_LEVEL);
			}
		);
	},
});
