// Kept in sync with ALLOWED_BILL_EXTENSIONS in expense_attachment.py,
// which enforces the same rule server-side (the REST API and the attach
// dialog's "link" tab never go through this restriction).
const ALLOWED_BILL_EXTENSIONS = [".pdf", ".jpg", ".jpeg", ".png"];

frappe.ui.form.on("Expense Claim", {
	onload(frm) {
		// frappe.ui.form.ControlAttach merges df.options into the file
		// uploader's options, so this limits the picker (and the drag &
		// drop target) to bill-shaped files.
		frm.set_df_property("custom_bill_attachment", "options", {
			restrictions: { allowed_file_types: ALLOWED_BILL_EXTENSIONS },
		});
	},

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
