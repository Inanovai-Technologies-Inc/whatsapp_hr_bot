"""Inbound half of the Expense Claim WhatsApp approval workflow.

The outbound half - sending the Expense Approver an interactive
Approve/Reject message the moment a claim is created (in Draft) - is
just another rule in notify/config.py ("approval_requested"), wired to
Expense Claim's after_insert in hooks.py. This module is the other half:
handling the approver's tap on one of those buttons. whatsapp_handler.py
delegates to it from handle_button, the same way it already delegates to
whatsapp_hr_bot.onboarding for that flow.

Only this module ever moves a claim to Approved/Rejected over WhatsApp,
and it does so only in direct response to that tap - never automatically
- and it explicitly triggers the employee confirmation message itself
(via notify.engine.dispatch) right after, since Expense Claim carries no
on_submit doc_event that could otherwise do that on its behalf (see
hooks.py and notify/config.py).
"""

import re

import frappe
from frappe.utils import now_datetime

from whatsapp_hr_bot.notify.engine import dispatch

APPROVE_PREFIX = "approve_expense:"
REJECT_PREFIX = "reject_expense:"


def is_expense_approval_button_id(button_id: str) -> bool:
    return button_id.startswith(APPROVE_PREFIX) or button_id.startswith(REJECT_PREFIX)


def handle_expense_approval_button(doc, phone, button_id):
    """``doc`` is the inbound WhatsApp Message; ``phone`` is who sent it."""
    from whatsapp_hr_bot.whatsapp_handler import send_text

    approve = button_id.startswith(APPROVE_PREFIX)
    claim_name = button_id.split(":", 1)[1]

    if not frappe.db.exists("Expense Claim", claim_name):
        send_text(doc, f"Expense Claim {claim_name} no longer exists.")
        return

    claim = frappe.get_doc("Expense Claim", claim_name)

    approver_phone = _get_approver_phone(claim)
    if not approver_phone or not _same_phone(approver_phone, phone):
        send_text(doc, "You are not authorised to act on this expense claim.")
        return

    if claim.docstatus != 0 or claim.approval_status != "Draft":
        send_text(
            doc,
            f"Expense Claim {claim.name} has already been {claim.approval_status}.",
        )
        return

    outcome = "Approved" if approve else "Rejected"

    # The webhook runs as Guest, which cannot submit documents - same
    # elevate/restore pattern already used elsewhere in this app for
    # webhook-triggered writes (see whatsapp_handler.py's own comment on
    # this, next to the expense claim creation flow).
    original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")

        claim.approval_status = outcome
        claim.flags.ignore_permissions = True
        claim.submit()

        claim.add_comment(
            "Info",
            f"{outcome} via WhatsApp by the Expense Approver on "
            f"{now_datetime().strftime('%Y-%m-%d %H:%M')}.",
        )

        frappe.db.commit()

        # Explicit, not automatic: this is the one place Expense Claim's
        # approved/rejected employee-notification rules (notify/config.py)
        # ever fire outside the manual "Send WhatsApp" button - triggered
        # here because this *is* the approver's explicit action.
        dispatch("Expense Claim", claim.name)
        frappe.db.commit()
    finally:
        frappe.set_user(original_user)

    send_text(
        doc,
        f"Expense Claim {claim.name} has been {outcome}. The employee has been notified.",
    )


def _get_approver_phone(claim) -> str | None:
    if not claim.expense_approver:
        return None
    for fieldname in ("mobile_no", "phone"):
        value = frappe.db.get_value("User", claim.expense_approver, fieldname)
        if value:
            return value
    return None


def _same_phone(a: str, b: str) -> bool:
    digits_a = re.sub(r"\D", "", str(a or ""))
    digits_b = re.sub(r"\D", "", str(b or ""))
    if not digits_a or not digits_b:
        return False
    # Compare on the shorter length so a stored number with/without a
    # country code still matches the webhook's full international form.
    shortest = min(len(digits_a), len(digits_b))
    return digits_a[-shortest:] == digits_b[-shortest:]
