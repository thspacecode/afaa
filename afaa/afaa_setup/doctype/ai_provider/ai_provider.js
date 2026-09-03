// Copyright (c) 2026, SpaceCode and contributors
// For license information, please see license.txt

frappe.ui.form.on("AI Provider", {
	setup(frm) {
		frappe.call("afaa.ai.provider.get_available_provider_types").then(({ message }) => {
			frm.fields_dict.provider_type.set_data(message || []);
		});
	},

	provider_type(frm) {
		frm.set_value("provider_name", frm.doc.provider_type);
	},

	before_save(frm) {
		frm.set_value("provider_name", frm.doc.provider_type);
	},
});
