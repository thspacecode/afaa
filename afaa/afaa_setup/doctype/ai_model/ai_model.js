// Copyright (c) 2026, SpaceCode and contributors
// For license information, please see license.txt

frappe.ui.form.on("AI Model", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Test Model"), () => {
			const dialog = new frappe.ui.Dialog({
				title: __("Test AI Model"),
				fields: [
					{
						fieldname: "provider_account",
						label: __("Provider Account"),
						fieldtype: "Link",
						options: "AI Provider Account",
						reqd: 1,
						get_query: () => ({
							filters: { provider: frm.doc.provider },
						}),
					},
				],
				primary_action_label: __("Test"),
				primary_action(values) {
					dialog.hide();
					frm.call("test_model", values).then(({ message }) => {
						frappe.show_alert({
							message: message.message,
							indicator: message.ok ? "green" : "red",
						});
						frm.reload_doc();
					});
				},
			});
			dialog.show();
		});
	},
});
