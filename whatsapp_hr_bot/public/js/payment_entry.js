frappe.ui.form.on("Payment Entry", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1) {
			return;
		}
		frm.add_custom_button(
			__("Send WhatsApp"),
			() => whatsapp_send_button.send_now(frm),
			__("WhatsApp")
		);
	},
});
