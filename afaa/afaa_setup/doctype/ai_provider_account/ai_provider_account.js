// Copyright (c) 2026, SpaceCode and contributors
// For license information, please see license.txt

frappe.ui.form.on("AI Provider Account", {
	refresh(frm) {
		configure_authentication_fields(frm);
		configure_actions(frm);
	},

	provider(frm) {
		configure_authentication_fields(frm, true);
	},

	before_save(frm) {
		frm.__sync_models_after_insert =
			frm.is_new() &&
			frm.doc.authentication_method === "API Key" &&
			Boolean(frm.doc.api_key);
	},

	after_save(frm) {
		if (!frm.__sync_models_after_insert) return;

		frm.__sync_models_after_insert = false;
		sync_models(frm);
	},
});

async function configure_authentication_fields(frm, set_authentication_method = false) {
	if (!frm.doc.provider) {
		set_oauth_fields_visible(frm, false);
		return;
	}

	const providerName = frm.doc.provider;
	const { message: providerType } = await frappe.db.get_value(
		"AI Provider",
		providerName,
		"provider_type"
	);
	if (frm.doc.provider !== providerName) return;

	const isCodex = providerType?.provider_type === "openai_codex";
	set_oauth_fields_visible(frm, isCodex);
	frm.set_df_property("authentication_method", "read_only", 1);

	if (set_authentication_method) {
		await frm.set_value("authentication_method", isCodex ? "OAuth Subscription" : "API Key");
	}
}

function set_oauth_fields_visible(frm, visible) {
	frm.toggle_display("api_key", !visible);
	frm.toggle_display("connected_app", visible);
	frm.toggle_display("oauth_status", visible);
	frm.toggle_display("connected_user", visible);
	frm.toggle_display("oauth_identity_section", visible);
}

async function configure_actions(frm) {
	frm.clear_custom_buttons();
	if (frm.is_new()) return;

	const accountName = frm.doc.name;
	const { message: authentication } = await frm.call("get_authentication_status");
	if (frm.doc.name !== accountName) return;

	if (authentication.supports_oauth_connection) {
		show_authentication_indicator(frm, authentication.status);
		if (!frm.doc.disabled) {
			const connectLabel =
				authentication.status === "Not Connected"
					? __("Connect ChatGPT")
					: __("Reconnect");
			frm.add_custom_button(connectLabel, () => connect_chatgpt(frm));

			if (authentication.authenticated) {
				frm.add_custom_button(__("Disconnect"), () => confirm_disconnect(frm));
			}
		}
	}

	if (!frm.doc.disabled && authentication.authenticated) {
		frm.add_custom_button(__("Sync Models"), () => sync_models(frm));
	}
}

function show_authentication_indicator(frm, status) {
	const colors = {
		Connected: "green",
		Expired: "orange",
		Error: "red",
		"Not Connected": "gray",
	};
	frm.dashboard.add_indicator(
		__("ChatGPT: {0}", [__(status || "Not Connected")]),
		colors[status] || "gray"
	);
}

async function connect_chatgpt(frm) {
	const { message: flow } = await frm.call("start_oauth_login");
	const expiresAt = Date.now() + flow.expires_in * 1000;
	let stopped = false;
	let pollTimer;
	let countdownTimer;
	let statusMessage = __("Waiting for authorization in ChatGPT...");
	let statusIndicator = "blue";

	const dialog = new frappe.ui.Dialog({
		title: __("Connect ChatGPT"),
		fields: [
			{
				fieldname: "instructions",
				fieldtype: "HTML",
				options: `<p class="text-muted">${frappe.utils.escape_html(
					__("Open the verification page, sign in to ChatGPT, and enter this code.")
				)}</p>`,
			},
			{
				fieldname: "user_code",
				fieldtype: "Data",
				label: __("User Code"),
				default: flow.user_code,
				read_only: 1,
			},
			{
				fieldname: "verification_url",
				fieldtype: "Data",
				label: __("Verification URL"),
				default: flow.verification_url,
				read_only: 1,
			},
			{ fieldtype: "Section Break" },
			{
				fieldname: "copy_code",
				fieldtype: "Button",
				label: __("Copy Code"),
				click: () =>
					frappe.utils.copy_to_clipboard(flow.user_code, __("ChatGPT code copied")),
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "open_verification_url",
				fieldtype: "Button",
				label: __("Open Verification URL"),
				click: () => open_verification_url(flow.verification_url),
			},
			{ fieldtype: "Section Break" },
			{ fieldname: "authorization_status", fieldtype: "HTML" },
			{ fieldname: "authorization_countdown", fieldtype: "HTML" },
		],
		primary_action_label: __("Close"),
		primary_action() {
			dialog.hide();
		},
		onhide() {
			stopped = true;
			clearTimeout(pollTimer);
			clearInterval(countdownTimer);
		},
	});

	function update_status(message, indicator = "blue") {
		statusMessage = message;
		statusIndicator = indicator;
		dialog.fields_dict.authorization_status.$wrapper.html(
			`<p class="indicator ${statusIndicator}">${frappe.utils.escape_html(
				statusMessage
			)}</p>`
		);
	}

	function update_countdown() {
		const seconds = Math.max(Math.ceil((expiresAt - Date.now()) / 1000), 0);
		const minutes = Math.floor(seconds / 60);
		const remainder = String(seconds % 60).padStart(2, "0");
		dialog.fields_dict.authorization_countdown.$wrapper.html(
			`<p class="text-muted">${frappe.utils.escape_html(
				__("Time remaining: {0}:{1}", [minutes, remainder])
			)}</p>`
		);
		if (seconds === 0 && !stopped) {
			stopped = true;
			clearTimeout(pollTimer);
			clearInterval(countdownTimer);
			update_status(__("Authorization expired; restart connection."), "red");
		}
	}

	async function poll() {
		if (stopped) return;
		try {
			const { message: result } = await frm.call("poll_oauth_login", {
				flow_id: flow.flow_id,
			});
			if (stopped) return;

			if (result.status === "complete") {
				stopped = true;
				clearInterval(countdownTimer);
				update_status(__("ChatGPT account connected."), "green");
				frappe.show_alert({
					message: __("ChatGPT account connected"),
					indicator: "green",
				});
				dialog.hide();
				await frm.reload_doc();
				return;
			}

			if (result.status === "expired") {
				stopped = true;
				clearInterval(countdownTimer);
				update_status(
					result.message || __("Authorization expired; restart connection."),
					"red"
				);
				return;
			}

			if (result.status === "failed") {
				stopped = true;
				clearInterval(countdownTimer);
				update_status(
					result.message || __("Unable to complete ChatGPT authorization."),
					"red"
				);
				return;
			}

			update_status(__("Waiting for authorization in ChatGPT..."));
			const retryAfter = Math.max(result.retry_after || result.poll_interval || 1, 1);
			pollTimer = setTimeout(poll, retryAfter * 1000);
		} catch (error) {
			stopped = true;
			clearInterval(countdownTimer);
			update_status(__("Unable to check ChatGPT authorization; restart connection."), "red");
		}
	}

	dialog.show();
	update_status(statusMessage, statusIndicator);
	update_countdown();
	countdownTimer = setInterval(update_countdown, 1000);
	pollTimer = setTimeout(poll, Math.max(flow.poll_interval, 1) * 1000);
}

function open_verification_url(url) {
	try {
		const verificationUrl = new URL(url);
		if (verificationUrl.protocol !== "https:") throw new Error("Unsupported URL protocol");
		window.open(verificationUrl.toString(), "_blank", "noopener,noreferrer");
	} catch (error) {
		frappe.msgprint(__("Unable to open the ChatGPT verification URL."));
	}
}

function confirm_disconnect(frm) {
	frappe.confirm(
		__("Disconnect this ChatGPT subscription? Local OAuth credentials will be removed."),
		async () => {
			await frm.call("disconnect_oauth");
			frappe.show_alert({ message: __("ChatGPT account disconnected"), indicator: "green" });
			await frm.reload_doc();
		}
	);
}

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
