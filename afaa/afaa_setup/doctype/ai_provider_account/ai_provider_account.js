// Copyright (c) 2026, SpaceCode and contributors
// For license information, please see license.txt

frappe.ui.form.on("AI Provider Account", {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__("Sync Models"), () => sync_models(frm));
	},

	before_save(frm) {
		frm.__sync_models_after_insert = frm.is_new() && Boolean(frm.doc.api_key);
	},

	after_save(frm) {
		if (!frm.__sync_models_after_insert) return;

		frm.__sync_models_after_insert = false;
		sync_models(frm);
	},
});

function sync_models(frm) {
	return frm.call("sync_models").then(({ message }) => {
		if (!message.ok) {
			frappe.show_alert({ message: message.message, indicator: "red" }, 10);
			frm.reload_doc();
			return;
		}

		frappe.show_alert({
			message: __("Models synchronized: {0} created, {1} updated, {2} unavailable", [
				message.created.length,
				message.updated.length,
				message.unavailable.length,
			]),
			indicator: "green",
		});
		frm.reload_doc();
	});
}
