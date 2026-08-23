// Copyright (c) 2026, SpaceCode and contributors
// For license information, please see license.txt

frappe.listview_settings["AI Tool"] = {
	onload(listview) {
		listview.page.add_inner_button(__("Sync Registered Tools"), () => {
			frappe.call("afaa.ai.tools.sync_registered_tools").then(({ message }) => {
				frappe.show_alert({
					message: __("Tools synchronized: {0} created, {1} updated", [
						message.created.length,
						message.updated.length,
					]),
					indicator: "green",
				});
				listview.refresh();
			});
		});
	},
};
