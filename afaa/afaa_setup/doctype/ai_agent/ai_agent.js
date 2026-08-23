// Copyright (c) 2026, SpaceCode and contributors
// For license information, please see license.txt

frappe.ui.form.on("AI Agent", {
	setup(frm) {
		frm.set_query("model", () => ({ filters: { available: 1 } }));
		frm.set_query("provider_account", () => ({
			filters: { provider: frm.doc.provider || "" },
		}));
	},

	model(frm) {
		frm.set_value("provider_account", null);
	},

	refresh(frm) {
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
});
