// Shared helper used by every per-DocType "Send WhatsApp" button
// (notify/config.py + notify/engine.py + api.py). Independent of the
// leave-bot's own JS (leave_application.js).
window.whatsapp_send_button = {
	send_now(frm) {
		frappe.call({
			method: "whatsapp_hr_bot.api.send_now",
			args: {
				doctype: frm.doc.doctype,
				docname: frm.doc.name,
			},
			freeze: true,
			freeze_message: __("Sending WhatsApp message..."),
			callback(r) {
				const res = r.message || {};
				if (res.ok) {
					frappe.show_alert({ message: res.message, indicator: "green" });
				} else {
					frappe.msgprint({
						title: __("WhatsApp"),
						message: res.message,
						indicator: "orange",
					});
				}
			},
		});
	},
};
