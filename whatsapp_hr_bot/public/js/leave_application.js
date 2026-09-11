frappe.ui.form.on("Leave Application", {
	refresh(frm) {
		const notifiable =
			frm.doc.docstatus === 1 &&
			["Approved", "Rejected"].includes(frm.doc.status);

		if (!notifiable) {
			return;
		}

		frm.add_custom_button(
			__("Notify Employee on WhatsApp"),
			() => {
				frappe.call({
					method: "whatsapp_hr_bot.whatsapp_handler.notify_leave_status",
					args: { leave_application: frm.doc.name },
					freeze: true,
					freeze_message: __("Sending WhatsApp notification..."),
					callback(r) {
						if (!r.exc) {
							frappe.show_alert({
								message: r.message || __("WhatsApp notification sent"),
								indicator: "green",
							});
						}
					},
				});
			},
			__("WhatsApp")
		);
	},
});
