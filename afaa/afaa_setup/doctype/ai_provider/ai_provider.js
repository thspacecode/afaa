// Copyright (c) 2026, SpaceCode and contributors
// For license information, please see license.txt

frappe.ui.form.on("AI Provider", {
	setup(frm) {
		frappe.call("afaa.ai.provider.get_available_provider_types").then(({ message }) => {
			frm.__afaa_provider_types = message || [];
			toggle_base_url(frm);
		});
	},

	refresh(frm) {
		toggle_base_url(frm);
	},

	provider_type(frm) {
		frm.set_value("provider_name", frm.doc.provider_type);
		toggle_base_url(frm);
	},

	before_save(frm) {
		frm.set_value("provider_name", frm.doc.provider_type);
	},
});

function toggle_base_url(frm) {
	const provider = (frm.__afaa_provider_types || []).find(
		(item) => item.value === frm.doc.provider_type
	);
	frm.toggle_display(
		["endpoint_section", "base_url"],
		provider ? provider.supports_base_url_override : false
	);
}
