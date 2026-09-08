// Copyright (c) 2026, SpaceCode and contributors
// For license information, please see license.txt

frappe.ui.form.on("AI Skill", {
	refresh(frm) {
		frm.set_df_property("skill_key", "read_only", !frm.is_new());
		if (!frm.is_new()) {
			render_bundle_manager(frm);
		}
	},
});

async function render_bundle_manager(frm) {
	const wrapper = frm.get_field("bundle_manager").$wrapper;
	wrapper.html(`<div class="text-muted">${__("Loading skill bundle…")}</div>`);
	try {
		const response = await frappe.call({
			method: "afaa.ai.skill_bundles.get_bundle_tree",
			args: { skill_name: frm.doc.name },
			type: "GET",
		});
		show_bundle_tree(frm, wrapper, response.message);
	} catch (error) {
		wrapper.html(
			`<div class="alert alert-danger">${__(
				"The skill bundle could not be validated."
			)}</div>`
		);
	}
}

function show_bundle_tree(frm, wrapper, bundle) {
	const can_write = frm.perm[0]?.write;
	const rows = bundle.items
		.map((item, index) => {
			const icon = item.type === "folder" ? "folder-normal" : item.isText ? "file" : "image";
			const type =
				item.type === "folder"
					? __("Folder")
					: item.isText
					? __("Text file")
					: __("Binary file");
			const size = item.size === null ? "—" : frappe.form.formatters.FileSize(item.size);
			return `<tr>
				<td>${frappe.utils.icon(icon, "sm")} ${frappe.utils.escape_html(item.path)}</td>
				<td>${type}</td><td>${size}</td>
				<td class="text-right">
					${
						item.type === "file"
							? `<button class="btn btn-xs btn-default bundle-read" data-index="${index}">${__(
									"Read"
							  )}</button>`
							: ""
					}
					${
						can_write
							? `<button class="btn btn-xs btn-default bundle-move" data-index="${index}">${__(
									"Move"
							  )}</button>
					<button class="btn btn-xs btn-danger bundle-delete" data-index="${index}">${__("Delete")}</button>`
							: ""
					}
				</td>
			</tr>`;
		})
		.join("");
	wrapper.html(`
		<div class="mb-3">
			<div><strong>${__("Bundle root")}:</strong> <code>${frappe.utils.escape_html(
		bundle.root
	)}</code></div>
			<div class="text-muted">${__(
				"Changes apply to future threads only. Existing threads keep their pinned version."
			)}</div>
		</div>
		${
			can_write
				? `<div class="mb-3"><button class="btn btn-sm btn-default bundle-new-folder">${__(
						"New Folder"
				  )}</button>
		<button class="btn btn-sm btn-primary bundle-upload">${__("Upload Files")}</button></div>`
				: ""
		}
		<div class="table-responsive"><table class="table table-bordered">
			<thead><tr><th>${__("Path")}</th><th>${__("Type")}</th><th>${__("Size")}</th><th></th></tr></thead>
			<tbody>${
				rows ||
				`<tr><td colspan="4" class="text-muted">${__(
					"This skill bundle is empty."
				)}</td></tr>`
			}</tbody>
		</table></div>
	`);

	wrapper.find(".bundle-new-folder").on("click", () => prompt_new_folder(frm));
	wrapper.find(".bundle-upload").on("click", () => prompt_upload(frm));
	wrapper
		.find(".bundle-read")
		.on("click", (event) =>
			read_bundle_item(frm, bundle.items[event.currentTarget.dataset.index])
		);
	wrapper
		.find(".bundle-move")
		.on("click", (event) =>
			move_bundle_item(frm, bundle.items[event.currentTarget.dataset.index])
		);
	wrapper
		.find(".bundle-delete")
		.on("click", (event) =>
			delete_bundle_item(frm, bundle.items[event.currentTarget.dataset.index])
		);
}

function prompt_new_folder(frm) {
	frappe.prompt(
		{ fieldname: "path", label: __("Relative folder path"), fieldtype: "Data", reqd: 1 },
		async ({ path }) => {
			await frappe.call({
				method: "afaa.ai.skill_bundles.create_bundle_folder",
				args: { skill_name: frm.doc.name, relative_path: path },
				type: "POST",
			});
			render_bundle_manager(frm);
		},
		__("Create Bundle Folder"),
		__("Create")
	);
}

function prompt_upload(frm) {
	frappe.prompt(
		{ fieldname: "folder", label: __("Relative target folder (optional)"), fieldtype: "Data" },
		({ folder }) => {
			const input = document.createElement("input");
			input.type = "file";
			input.multiple = true;
			input.onchange = async () => {
				for (const file of input.files) {
					const content = await read_file_as_base64(file);
					const path = [folder?.replace(/\/$/, ""), file.name].filter(Boolean).join("/");
					await frappe.call({
						method: "afaa.ai.skill_bundles.upload_bundle_file",
						args: { skill_name: frm.doc.name, relative_path: path, content },
						type: "POST",
					});
				}
				render_bundle_manager(frm);
			};
			input.click();
		},
		__("Upload Bundle Files"),
		__("Choose Files")
	);
}

function read_file_as_base64(file) {
	return new Promise((resolve, reject) => {
		const reader = new FileReader();
		reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
		reader.onerror = reject;
		reader.readAsDataURL(file);
	});
}

async function read_bundle_item(frm, item) {
	const response = await frappe.call({
		method: "afaa.ai.skill_bundles.read_bundle_file",
		args: { skill_name: frm.doc.name, relative_path: item.path },
		type: "GET",
	});
	if (!response.message.isText) {
		frappe.msgprint(
			__("This is a binary file. Its exact bytes are retained in bundle versions.")
		);
		return;
	}
	const dialog = new frappe.ui.Dialog({
		title: item.path,
		fields: [{ fieldname: "content", fieldtype: "Code", read_only: 1 }],
		size: "large",
	});
	dialog.set_value("content", response.message.text);
	dialog.show();
}

function move_bundle_item(frm, item) {
	frappe.prompt(
		{
			fieldname: "path",
			label: __("New relative path"),
			fieldtype: "Data",
			reqd: 1,
			default: item.path,
		},
		async ({ path }) => {
			await frappe.call({
				method: "afaa.ai.skill_bundles.move_bundle_member",
				args: { skill_name: frm.doc.name, source_path: item.path, target_path: path },
				type: "POST",
			});
			render_bundle_manager(frm);
		},
		__("Move Bundle Resource"),
		__("Move")
	);
}

function delete_bundle_item(frm, item) {
	frappe.confirm(
		__("Delete bundle resource {0}?", [frappe.utils.escape_html(item.path)]),
		async () => {
			await frappe.call({
				method: "afaa.ai.skill_bundles.delete_bundle_member",
				args: { skill_name: frm.doc.name, relative_path: item.path },
				type: "POST",
			});
			render_bundle_manager(frm);
		}
	);
}
