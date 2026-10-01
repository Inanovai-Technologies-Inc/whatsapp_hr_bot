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

:func:`send_pending_approvals` is the way in when that outbound half
could not be delivered. Meta refuses a free-form or interactive message
to anyone who has not written to the business number in the last 24
hours (notify/delivery.py), and an Expense Approver's window is usually
shut - which is why an approval request can go out, be accepted by
Meta's API, and never arrive. An approver who sends the bot *anything*
opens that window, so the "approvals" keyword (wired in
whatsapp_handler.py) answers with every claim still waiting on them,
each with its own Approve/Reject buttons and its own bill - a reply to
them, which Meta always delivers.
"""

import re

import frappe
from frappe.utils import now_datetime

from whatsapp_hr_bot.notify.config import (
    expense_approval_buttons,
    expense_approval_request_message,
)
from whatsapp_hr_bot.notify.engine import dispatch

APPROVE_PREFIX = "approve_expense:"
REJECT_PREFIX = "reject_expense:"

# Savepoint the claim's submit is wrapped in - see below.
SUBMIT_SAVEPOINT = "whatsapp_expense_submit"

# What an approver can type to pull the claims waiting on them. Matched on
# the whole normalised message, like whatsapp_handler's own keyword lists.
PENDING_APPROVAL_KEYWORDS = [
    "approvals",
    "approval",
    "pending approvals",
    "pending approval",
    "expense approvals",
    "expense approval",
    "my approvals",
]

# At most this many claims per reply, newest first - each one costs two
# WhatsApp messages (the request and its bill).
MAX_PENDING_APPROVALS = 5


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

        # Approving a claim posts GL Entries (HRMS skips them only while
        # nothing is sanctioned), so it can fail on accounting
        # configuration the approver has no way of seeing from a chat
        # message - a missing Payable Account or Cost Center, no default
        # account on the Expense Claim Type, a closed accounting period.
        # By then ``submit`` has already written docstatus 1, since
        # ``make_gl_entries`` runs in on_submit: without a rollback the
        # claim would be left submitted with no GL Entries behind it.
        #
        # Rolled back to a savepoint rather than outright, so undoing the
        # approval does not also discard the inbound WhatsApp Message this
        # webhook is still inside the transaction of.
        frappe.db.savepoint(SUBMIT_SAVEPOINT)

        try:
            claim.submit()
        except Exception as exc:
            frappe.db.rollback(save_point=SUBMIT_SAVEPOINT)
            frappe.log_error(
                frappe.get_traceback(),
                f"WhatsApp HR Bot - Expense Claim {claim_name} {outcome} Failed",
            )

            reason = frappe.utils.strip_html(str(exc)).strip() or "HRMS rejected the change."

            send_text(
                doc,
                f"Expense Claim {claim_name} could not be {outcome.lower()}.\n\n"
                f"{reason}\n\n"
                "It is still in Draft. Please ask Accounts to check the claim's "
                "setup, then try again.",
            )
            return

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


# ============================================================
# PENDING APPROVALS (approver-initiated)
# ============================================================

def is_pending_approval_keyword(text: str) -> bool:
    """``text`` is expected to be normalised by ``normalize_text``."""

    return text in PENDING_APPROVAL_KEYWORDS


def find_approver_users(phone: str) -> list[str]:
    """Enabled Users reachable at ``phone``.

    Resolved the other way round from :func:`_get_approver_phone` - from
    the number back to the Users it belongs to - so an approver is
    recognised by their WhatsApp number without having to be an Employee,
    exactly as the approval buttons already recognise them. One query,
    then the same last-digits comparison :func:`_same_phone` makes.
    """

    users = frappe.get_all(
        "User",
        filters={"enabled": 1},
        or_filters=[["mobile_no", "is", "set"], ["phone", "is", "set"]],
        fields=["name", "mobile_no", "phone"],
    )

    return [
        user.name
        for user in users
        if _same_phone(user.mobile_no, phone) or _same_phone(user.phone, phone)
    ]


def is_expense_approver(phone: str) -> bool:
    """Whether ``phone`` belongs to somebody named as an Expense Approver.

    Gates the "approvals" keyword in whatsapp_handler.py, so that word
    only changes behaviour for an actual approver and keeps falling
    through to the existing handling for everybody else.
    """

    users = find_approver_users(phone)

    if not users:
        return False

    return bool(
        frappe.db.exists("Employee", {"expense_approver": ["in", users]})
        or frappe.db.exists("Expense Claim", {"expense_approver": ["in", users]})
    )


def get_pending_claims_for_approver(phone: str) -> list[str]:
    """Draft Expense Claims whose Expense Approver is reachable at ``phone``."""

    users = find_approver_users(phone)

    if not users:
        return []

    return frappe.get_all(
        "Expense Claim",
        filters={
            "docstatus": 0,
            "approval_status": "Draft",
            "expense_approver": ["in", users],
        },
        pluck="name",
        order_by="creation desc",
        limit_page_length=MAX_PENDING_APPROVALS,
    )


def send_pending_approvals(doc, phone) -> None:
    """Answer an approver with every claim still waiting on them.

    ``doc`` is the inbound WhatsApp Message, so this is a reply - which
    means Meta delivers it whatever the state of the 24-hour window that
    blocked the original request (notify/delivery.py). Each claim is sent
    as its own interactive Approve/Reject message, followed by its
    bill/receipt where there is one, so the approver decides on the same
    evidence the employee attached.
    """

    from whatsapp_hr_bot.whatsapp_handler import send_interactive, send_text

    claim_names = get_pending_claims_for_approver(phone)

    if not claim_names:
        send_text(
            doc,
            "You have no expense claims waiting for your approval.",
        )
        return

    send_text(
        doc,
        f"🧾 Expense Claims awaiting your approval: {len(claim_names)}",
    )

    for claim_name in claim_names:
        claim = frappe.get_doc("Expense Claim", claim_name)

        send_interactive(
            doc,
            expense_approval_request_message(claim),
            expense_approval_buttons(claim),
        )

        _send_bill(doc, claim)


def _send_bill(doc, claim) -> None:
    """Send ``claim``'s bill/receipt as its own media message, if it has one.

    Same two-message shape as the notification engine uses, and for the
    same reason: an interactive message carries no attachment. Failures
    are logged and swallowed - the approval request itself has already
    gone out and must not be undone by a missing attachment.
    """

    from whatsapp_hr_bot import expense_attachment
    from whatsapp_hr_bot.whatsapp_handler import send_media

    try:
        link = expense_attachment.get_bill_delivery_url(claim)

        if not link:
            return

        send_media(
            doc,
            expense_attachment.get_bill_media_type(link),
            link,
            f"Bill / receipt attached to Expense Claim {claim.name}.",
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"WhatsApp HR Bot - Could not send the bill for {claim.name}",
        )


# ============================================================
# APPROVE / REJECT FROM AN APPROVED TEMPLATE
# ============================================================

# What the two Quick Reply buttons on the approval template are labelled.
# Meta sends back the *label* a recipient tapped and nothing else, so these
# are the whole vocabulary of a template reply.
TEMPLATE_APPROVE_LABEL = "approve"
TEMPLATE_REJECT_LABEL = "reject"


def resolve_template_reply(doc) -> str | None:
    """Turn a Quick Reply tap on the approval template into one of this
    module's ``approve_expense:<claim>`` / ``reject_expense:<claim>`` ids.

    A template's buttons cannot carry a payload of our own - Meta defines
    them when it approves the template, and a tap arrives as
    ``content_type`` "button" whose message is just the button's label
    ("Approve"), with no claim anywhere in it. What it *does* carry is
    ``reply_to_message_id``: the id of the message being replied to. That
    is the template message this app sent, and its WhatsApp Message row
    records which Expense Claim it was about - so the claim comes from
    there.

    Returns ``None`` for anything that is not such a reply, which is every
    other inbound message; the caller then carries on as before.
    Authorisation is unchanged either way -
    :func:`handle_expense_approval_button` still checks that the sender is
    that claim's Expense Approver and that the claim is still in Draft.
    """

    if (doc.get("content_type") or "").strip().lower() != "button":
        return None

    label = " ".join(str(doc.get("message") or "").lower().split())

    if label == TEMPLATE_APPROVE_LABEL:
        prefix = APPROVE_PREFIX
    elif label == TEMPLATE_REJECT_LABEL:
        prefix = REJECT_PREFIX
    else:
        return None

    replied_to = doc.get("reply_to_message_id")

    if not replied_to:
        return None

    sent = frappe.db.get_value(
        "WhatsApp Message",
        {"message_id": replied_to},
        ["reference_doctype", "reference_name"],
        as_dict=True,
    )

    if not sent or sent.reference_doctype != "Expense Claim" or not sent.reference_name:
        return None

    return f"{prefix}{sent.reference_name}"
