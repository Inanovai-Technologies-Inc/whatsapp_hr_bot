"""Whitelisted endpoints for the client-side "Send WhatsApp" buttons.

Unrelated to the leave-bot's own whitelisted method
(``whatsapp_handler.notify_leave_status``) - this one is generic across
every DocType configured in ``notify/config.py``.
"""

import frappe
from frappe import _

from whatsapp_hr_bot.notify.config import get_rules
from whatsapp_hr_bot.notify.engine import dispatch


@frappe.whitelist()
def send_now(doctype: str, docname: str) -> dict:
    """Called by the "Send WhatsApp" button. Runs synchronously so the
    user gets an immediate result, unlike the doc-event path which is
    backgrounded.
    """
    frappe.has_permission(doctype, doc=docname, throw=True)

    if not get_rules(doctype):
        return {
            "ok": False,
            "message": _("No WhatsApp notification is configured for {0}.").format(doctype),
        }

    results = dispatch(doctype, docname)

    sent = [r for r in results if r.get("ok") and not r.get("skipped")]
    already_sent = [r for r in results if r.get("skip_reason") == "already_sent"]
    not_due = [r for r in results if r.get("skip_reason") == "condition_not_met"]
    failed = [r for r in results if not r.get("ok")]

    if sent:
        return {"ok": True, "message": _("WhatsApp message sent successfully.")}

    if failed:
        return {
            "ok": False,
            "message": _("Failed to send WhatsApp message: {0}").format(failed[0]["reason"]),
        }

    if already_sent:
        return {
            "ok": True,
            "message": _("A WhatsApp notification has already been sent for this document."),
        }

    if not_due:
        return {
            "ok": False,
            "message": _("This document doesn't currently meet the conditions for a WhatsApp notification."),
        }

    return {
        "ok": False,
        "message": _("No WhatsApp notification is configured for {0}.").format(doctype),
    }
