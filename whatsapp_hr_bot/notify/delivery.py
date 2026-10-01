"""Why a WhatsApp message did or did not arrive.

Two things live here.

**Meta's reason, on the message row.** frappe_whatsapp logs every
inbound webhook payload to ``WhatsApp Notification Log`` and then, in
``update_message_status``, keeps only ``statuses[0].status`` off it - so a
row that says "failed" says nothing about *why*, and the explanation is
buried in a JSON blob among hundreds of others.
:func:`record_delivery_error` reads the same payload as it is logged and
writes Meta's code, title and explanation onto the matching WhatsApp
Message's ``custom_error`` (the field this app already adds), so the send
log is self-explanatory.

**Meta's 24-hour window.** Meta only delivers a free-form message -
plain text, an interactive Approve/Reject, a media attachment - to
someone who has messaged the business number in the last 24 hours;
outside that it rejects the message with error 131047 ("Re-engagement
message"), and only a template approved by Meta can get through. This is
the single most common reason an Expense Approver never sees an approval
request: the approver is not a person who chats with the bot all day, so
their window is usually shut.

:func:`is_within_service_window` answers that question from this site's
own inbound message log, which is the same thing Meta is measuring.
:func:`explain_unreachable` turns it into a sentence for the send log.
The engine uses both to say plainly, on the failed row, that the
recipient was out of window - rather than leaving "failed" with no
explanation - and ``expense_approval.send_pending_approvals`` is how an
approver gets those requests once their window is open again.
"""

import json
import re

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

# Meta's "more than 24 hours since the customer last replied" error.
RE_ENGAGEMENT_ERROR_CODE = 131047

SERVICE_WINDOW_HOURS = 24


def record_delivery_error(doc, method=None):
    """``WhatsApp Notification Log`` after_insert - copy Meta's delivery
    error onto the WhatsApp Message it refers to.

    Runs for every webhook payload the site receives, so it returns on
    the first check for anything that is not a delivery report. Never
    raises: this is diagnostics, and it must not break the webhook that
    carries inbound messages.
    """

    try:
        statuses = _get_statuses(doc.get("meta_data"))
    except Exception:
        return

    for status in statuses:
        message_id = status.get("id")
        errors = status.get("errors") or []

        if not message_id or not errors:
            continue

        name = frappe.db.get_value("WhatsApp Message", {"message_id": message_id}, "name")

        if not name:
            continue

        try:
            frappe.db.set_value(
                "WhatsApp Message",
                name,
                "custom_error",
                _format_errors(errors)[:1000],
                update_modified=False,
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "WhatsApp HR Bot - Could not record the delivery error",
            )


def _get_statuses(meta_data) -> list[dict]:
    if not meta_data:
        return []

    data = json.loads(meta_data) if isinstance(meta_data, str) else meta_data

    entries = data.get("entry") or []

    if isinstance(entries, dict):
        entries = [entries]

    statuses = []

    for entry in entries:
        for change in entry.get("changes") or []:
            statuses.extend((change.get("value") or {}).get("statuses") or [])

    return statuses


def _format_errors(errors: list[dict]) -> str:
    parts = []

    for error in errors:
        code = error.get("code")
        title = error.get("title") or error.get("message") or ""
        details = (error.get("error_data") or {}).get("details") or ""

        parts.append(" ".join(str(bit) for bit in (code, title, "-", details) if bit).strip(" -"))

        if code == RE_ENGAGEMENT_ERROR_CODE:
            parts.append(
                "Meta only delivers free-form messages to someone who has messaged "
                "this WhatsApp number in the last 24 hours. Ask the recipient to send "
                "the bot any message, then retry - or use an approved template."
            )

    return "\n".join(parts)


def is_within_service_window(phone: str) -> bool:
    """Whether ``phone`` has messaged this WhatsApp number recently enough
    for Meta to deliver a free-form message to them.

    Measured against the inbound WhatsApp Messages this site has recorded,
    which is the same conversation Meta is timing. Compares on the last
    10 digits so a stored number with or without a country code still
    matches the webhook's international form - the same comparison
    ``expense_approval._same_phone`` makes.
    """

    digits = re.sub(r"\D", "", str(phone or ""))

    if len(digits) < 10:
        return False

    since = add_to_date(now_datetime(), hours=-SERVICE_WINDOW_HOURS)

    last_inbound = frappe.db.get_value(
        "WhatsApp Message",
        {
            "type": "Incoming",
            "from": ["like", f"%{digits[-10:]}"],
            "creation": [">=", get_datetime(since)],
        },
        "creation",
    )

    return bool(last_inbound)


def get_approved_template(actual_name: str | None) -> str | None:
    """The WhatsApp Templates *record* for ``actual_name`` if Meta has
    approved it, else ``None``.

    ``actual_name`` is the name Meta knows the template by; the record's
    own name has the language appended to it, and that is what has to go
    into a WhatsApp Message's ``template`` link - hence the lookup rather
    than passing the name straight through.

    A template is the only thing Meta delivers outside the 24-hour window,
    but only once it is APPROVED - sending a PENDING or REJECTED one just
    fails differently. So a rule's fallback template is used only when Meta
    says it is ready, and otherwise the caller carries on with the
    free-form message it would have sent anyway.
    """

    if not actual_name:
        return None

    record = frappe.db.get_value(
        "WhatsApp Templates", {"actual_name": actual_name}, ["name", "status"], as_dict=True
    )

    if not record:
        return None

    return record.name if (record.status or "").upper() == "APPROVED" else None


def explain_unreachable(phone: str) -> str | None:
    """A sentence for the send log when ``phone`` is outside the window,
    or ``None`` when they are inside it and a free-form send should work.
    """

    if is_within_service_window(phone):
        return None

    return (
        f"{phone} has not messaged this WhatsApp number in the last "
        f"{SERVICE_WINDOW_HOURS} hours, so Meta will reject a free-form message "
        "with error 131047 (Re-engagement). Ask them to send the bot any "
        "message - replying to it, or sending 'approvals' - and it will be "
        "delivered; an approved template is the only way to reach them "
        "unprompted."
    )
