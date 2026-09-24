// Copyright (c) 2026, SpaceCode and contributors
// For license information, please see license.txt

frappe.ui.form.on("AI MCP Server", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.disabled) return;

		frm.add_custom_button(__("Discover Tools"), () => discover_tools(frm));
	},
});

async function discover_tools(frm) {
	const { message } = await frappe.call({
		method: "afaa.ai.mcp.test_connection",
		type: "POST",
		args: { mcp_server: frm.doc.name },
		freeze: true,
		freeze_message: __("Connecting to MCP server..."),
	});

	if (!message?.ok) return;

	show_discovered_tools(frm, message);
}

function show_discovered_tools(frm, discovery) {
	const existingTools = new Set(
		(frm.doc.allowed_tools || []).map((row) => row.tool_name).filter(Boolean)
	);
	const tools = (discovery.tools || []).map((tool) => ({
		selected: 0,
		tool_name: tool.name,
		description: tool.description,
		already_allowed: existingTools.has(tool.name) ? __("Yes") : __("No"),
	}));

	const dialog = new frappe.ui.Dialog({
		title: __("Discovered MCP Tools"),
		size: "extra-large",
		fields: [
			{
				fieldname: "server_summary",
				fieldtype: "HTML",
				options: discovery_summary(discovery),
			},
			{
				fieldname: "tools",
				fieldtype: "Table",
				label: __("Tools"),
				cannot_add_rows: true,
				cannot_delete_rows: true,
				in_place_edit: true,
				data: tools,
				fields: [
					{
						fieldname: "selected",
						fieldtype: "Check",
						label: __("Add"),
						in_list_view: 1,
						columns: 1,
					},
					{
						fieldname: "tool_name",
						fieldtype: "Data",
						label: __("Tool Name"),
						read_only: 1,
						in_list_view: 1,
						columns: 3,
					},
					{
						fieldname: "description",
						fieldtype: "Small Text",
						label: __("Description"),
						read_only: 1,
						in_list_view: 1,
						columns: 6,
					},
					{
						fieldname: "already_allowed",
						fieldtype: "Data",
						label: __("Already Allowed"),
						read_only: 1,
						in_list_view: 1,
						columns: 2,
					},
				],
			},
		],
		primary_action_label: __("Add Selected Tools"),
		primary_action(values) {
			const selected = (values.tools || []).filter(
				(row) => row.selected && !existingTools.has(row.tool_name)
			);
			if (!selected.length) {
				frappe.msgprint(__("Select at least one tool that is not already allowed."));
				return;
			}

			if ((frm.doc.allowed_tools || []).length + selected.length > 100) {
				frappe.msgprint(__("An MCP server may allow at most 100 tools."));
				return;
			}

			selected.forEach((tool) => {
				const row = frm.add_child("allowed_tools");
				row.tool_name = tool.tool_name;
				row.enabled = 1;
			});
			frm.refresh_field("allowed_tools");
			frm.dirty();
			dialog.hide();
			frappe.show_alert({
				message: __("Added {0} tool(s). Save the document to apply the allowlist.", [
					selected.length,
				]),
				indicator: "green",
			});
		},
	});

	dialog.show();
}

function discovery_summary(discovery) {
	const serverName = frappe.utils.escape_html(discovery.serverName || __("Unknown server"));
	const serverVersion = frappe.utils.escape_html(
		discovery.serverVersion || __("Unknown version")
	);
	const transport = frappe.utils.escape_html(discovery.transport || "");
	const count = discovery.tools?.length || 0;

	return `<p class="text-muted">${__(
		"Connected to {0} ({1}) using {2}. Discovered {3} tool(s). Select the tools to add to the allowlist.",
		[serverName, serverVersion, transport, count]
	)}</p>`;
}
