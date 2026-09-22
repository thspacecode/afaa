// Copyright (c) 2026, SpaceCode and contributors
// For license information, please see license.txt

frappe.ui.form.on("AI Skill Repo", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		frm.add_custom_button(__("Sync Skill"), () => run_repo_action(frm, "sync"));
		frm.get_field("skills").grid.add_custom_button(__("Fetch Skill"), () =>
			run_repo_action(frm, "fetch")
		);
	},
});

async function run_repo_action(frm, action) {
	const method =
		action === "fetch"
			? "afaa.ai.skill_repos.fetch_skills"
			: "afaa.ai.skill_repos.sync_skills";
	const response = await frappe.call({
		method,
		args: { repo: frm.doc.name },
		type: "POST",
		freeze: true,
		freeze_message:
			action === "fetch"
				? __("Fetching skills from the repository…")
				: __("Syncing skills from the pinned commit…"),
	});
	const message = response.message || {};
	if (action === "fetch") {
		show_fetch_summary(frm, message);
	} else {
		show_sync_summary(frm, message);
	}
	frm.reload_doc();
}

function show_fetch_summary(frm, message) {
	const rows = [
		["branch", __("Branch"), message.branch],
		["commit", __("Pinned commit"), short_commit(message.commit)],
		["discovered", __("Skills discovered"), message.discovered],
		["removed", __("Rows removed"), message.removed],
	];
	frappe.msgprint({
		indicator: "green",
		title: __("Fetch Skill"),
		message: `<table class="table table-bordered">
			${rows
				.map(
					([, label, value]) =>
						`<tr><td>${label}</td><td><code>${frappe.utils.escape_html(
							String(value ?? "—")
						)}</code></td></tr>`
				)
				.join("")}
		</table>${
			message.invalidKeys?.length
				? `<div class="alert alert-warning">${__(
						"These folders cannot produce a valid skill key and will be skipped by Sync:"
				  )} <strong>${frappe.utils.escape_html(
						message.invalidKeys.join(", ")
				  )}</strong></div>`
				: ""
		}`,
	});
}

function show_sync_summary(frm, message) {
	const counts = message.counts || {};
	const summary = [
		["created", __("Created")],
		["updated", __("Updated")],
		["unchanged", __("Unchanged")],
		["skipped", __("Skipped")],
	]
		.map(([key, label]) => `${label}: <strong>${counts[key] ?? 0}</strong>`)
		.join(" &middot; ");
	const rows = (message.results || [])
		.map((result) => {
			const indicator = {
				created: "green",
				updated: "blue",
				unchanged: "grey",
				skipped: "orange",
			}[result.status];
			return `<tr>
				<td>${frappe.utils.escape_html(result.skill || "")}</td>
				<td>${frappe.utils.escape_html(result.skillName || "")}</td>
				<td><code>${frappe.utils.escape_html(result.skillKey || "")}</code></td>
				<td><span class="indicator ${indicator}">${__(result.status)}</span></td>
				<td>${frappe.utils.escape_html(result.reason || "")}</td>
			</tr>`;
		})
		.join("");
	frappe.msgprint({
		indicator: "green",
		title: __("Sync Skill"),
		message: `<p>${summary} &middot; ${__("Pinned commit")}: <code>${frappe.utils.escape_html(
			short_commit(message.commit)
		)}</code></p>
		<div class="table-responsive"><table class="table table-bordered">
			<thead><tr>
				<th>${__("Folder")}</th><th>${__("Skill")}</th><th>${__("Key")}</th>
				<th>${__("Status")}</th><th>${__("Reason")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table></div>`,
	});
}

function short_commit(commit) {
	return commit ? String(commit).slice(0, 12) : "—";
}
