
"""Generic WhatsApp notification dispatcher.

Single entry point: :func:`dispatch`. Everything else in this app - the
``doc_events`` hooks and the per-DocType "Send WhatsApp" buttons - only
calls into this module. DocType-specific knowledge lives entirely in
``whatsapp_hr_bot.notify.config.NOTIFICATION_RULES``.

This module is independent of ``whatsapp_handler.py`` (the leave bot):
it only reacts to the DocTypes listed in ``config.NOTIFICATION_RULES``
via the doc_events wired in hooks.py, and never touches the leave-bot's
after_insert handler on WhatsApp Message.

Design notes
------------
- ``WhatsApp Message.before_insert`` (in ``frappe_whatsapp``) sends the
  message synchronously the moment the doc is inserted, and raises on
  failure. So a normal ``.insert()`` never leaves a "Failed" row behind -
  the insert is rolled back before any row exists. To keep a real audit
  trail of failures (missing phone, API error, ...) we build the failed
  log row ourselves and write it with ``doc.db_insert()``, which writes
  the row directly and skips ``before_insert`` - so it never re-attempts
  a send or throws.
- Event-triggered sends are deferred with ``frappe.db.after_commit`` and
  handed to a background worker (same pattern ``frappe_whatsapp`` itself
  uses for its own notifications). This keeps a slow/failed WhatsApp API
  call from ever blocking or failing the Purchase Order/Sales
  Order/Expense Claim submission that triggered it.
- Button-triggered sends call :func:`dispatch` directly (synchronously)
  so the user gets an immediate result.
"""

import re

import frappe
from frappe import _

from whatsapp_hr_bot.notify.config import get_rules

DONE_STATUSES = ("Sent", "Success")


def on_doc_event(doc, method=None):
    """``doc_events`` target - wired in hooks.py for each configured DocType."""
    event = method
    if not get_rules(doc.doctype, event):
        return

    doctype, docname = doc.doctype, doc.name

    def _enqueue():
        frappe.enqueue(
            "whatsapp_hr_bot.notify.engine.dispatch",
            queue="short",
            doctype=doctype,
            docname=docname,
            event=event,
        )

    # Defer past commit: avoids sending based on data that a later error
    # in the same request could still roll back, and keeps the network
    # call off the request/transaction that triggered it.
    if hasattr(frappe.db, "after_commit"):
        frappe.db.after_commit.add(_enqueue)
    else:
        _enqueue()


def dispatch(doctype: str, docname: str, event: str | None = None, force: bool = False) -> list[dict]:
    """Evaluate every rule for ``doctype`` (optionally scoped to ``event``)
    against the current state of ``docname`` and send what applies.

    Never raises - every failure (missing config, missing phone, API
    error, ...) is caught, logged, and reflected in the returned list
    instead, so this is always safe to call from a doc event, a
    background job, or a whitelisted button handler.
    """
    results = []

    try:
        doc = frappe.get_doc(doctype, docname)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"WhatsApp Notify: {doctype} {docname} not found")
        return [{"ok": False, "reason": "Document no longer exists."}]

    for rule in get_rules(doctype, event):
        results.append(_run_rule(doc, rule, force=force))

    return results


def _run_rule(doc, rule: dict, force: bool) -> dict:
    condition = rule.get("condition")
    try:
        if condition and not condition(doc):
            return {"ok": True, "skipped": True, "skip_reason": "condition_not_met"}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WhatsApp Notify - condition evaluation failed")
        return {"ok": False, "reason": "Could not evaluate the notification condition."}

    dedupe_key_fn = rule.get("dedupe_key")
    dedupe_key_value = dedupe_key_fn(doc) if dedupe_key_fn else "default"

    if not force and _already_sent(doc.doctype, doc.name, dedupe_key_value):
        return {"ok": True, "skipped": True, "skip_reason": "already_sent"}

    recipient = resolve_recipient(doc, rule.get("recipient") or {})

    if not recipient or not recipient.phone:
        error = _("No WhatsApp-capable phone number was found for this document's recipient.")
        _create_failed_log(doc, rule, dedupe_key_value, recipient, error)
        frappe.log_error(error, f"WhatsApp Notify: {doc.doctype} {doc.name}")
        return {"ok": False, "reason": error}

    try:
        wa_message = _send(doc, rule, dedupe_key_value, recipient)
        return {"ok": True, "message": wa_message.name}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"WhatsApp Notify failed: {doc.doctype} {doc.name}")
        _create_failed_log(doc, rule, dedupe_key_value, recipient, e)
        return {"ok": False, "reason": str(e)}


def resolve_recipient(doc, recipient_cfg: dict):
    """Resolve who to message and their phone number.

    Never raises - returns ``None`` if the configured link field is
    empty or the linked record no longer exists.
    """
    link_field = recipient_cfg.get("link_field")

    if link_field:
        linked_name = doc.get(link_field)
        if not linked_name:
            return None

        recipient_doctype = recipient_cfg.get("doctype")
        if not frappe.db.exists(recipient_doctype, linked_name):
            return None

        recipient_doc = frappe.get_doc(recipient_doctype, linked_name)
    else:
        recipient_doctype = doc.doctype
        linked_name = doc.name
        recipient_doc = doc

    phone = None
    for fieldname in recipient_cfg.get("phone_fields") or []:
        value = recipient_doc.get(fieldname)
        if value:
            phone = clean_phone(value)
            if phone:
                break

    user_fallback_field = recipient_cfg.get("user_fallback_field")
    if not phone and user_fallback_field:
        user_id = recipient_doc.get(user_fallback_field)
        if user_id:
            for fieldname in ("mobile_no", "phone"):
                value = frappe.db.get_value("User", user_id, fieldname)
                if value:
                    phone = clean_phone(value)
                    if phone:
                        break

    name_field = recipient_cfg.get("name_field")
    display_name = None
    if name_field:
        display_name = doc.get(name_field) or recipient_doc.get(name_field)
    display_name = display_name or linked_name

    return frappe._dict(
        recipient_doctype=recipient_doctype,
        recipient_name=linked_name,
        phone=phone,
        display_name=display_name,
    )


def clean_phone(phone) -> str | None:
    """Basic phone validation: keep digits (and a leading +), require at
    least 10 digits. Returns ``None`` for anything that doesn't qualify -
    callers must treat that as "no usable number", not hardcode a
    fallback number.
    """
    if not phone:
        return None

    phone = str(phone).strip()
    digits_only = re.sub(r"\D", "", phone)

    if len(digits_only) < 10:
        return None

    return ("+" + digits_only) if phone.startswith("+") else digits_only


def _already_sent(doctype: str, docname: str, dedupe_key_value: str) -> bool:
    return bool(
        frappe.db.exists(
            "WhatsApp Message",
            {
                "reference_doctype": doctype,
                "reference_name": docname,
                "custom_notification_event": dedupe_key_value,
                "status": ["in", list(DONE_STATUSES)],
            },
        )
    )


def _build_send_fields(doc, rule: dict) -> dict:
    """Either a plain-text message (no template approval needed - subject
    to Meta's 24-hour session window, see config.py) or an approved
    template, depending on which the rule defines.
    """
    if rule.get("template"):
        return {"content_type": "text", "template": rule["template"]}

    message_fn = rule.get("message")
    message = message_fn(doc) if message_fn else ""
    return {"content_type": "text", "message": message}


def _send(doc, rule: dict, dedupe_key_value: str, recipient):
    wa_message = frappe.get_doc(
        {
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": recipient.phone,
            "reference_doctype": doc.doctype,
            "reference_name": doc.name,
            "custom_notification_event": dedupe_key_value,
            "custom_recipient_doctype": recipient.recipient_doctype,
            "custom_recipient": recipient.recipient_name,
            "custom_recipient_name": recipient.display_name,
            **_build_send_fields(doc, rule),
        }
    )
    # WhatsAppMessage.before_insert sends the message/template via the
    # Meta API and sets message_id; it raises on failure (caught by the
    # caller).
    wa_message.insert(ignore_permissions=True)

    # frappe_whatsapp sets its own status ("Success", or nothing for a
    # template send) - normalise to "Sent" here so every row in this
    # log uses the same Pending/Sent/Failed vocabulary.
    frappe.db.set_value(
        "WhatsApp Message",
        wa_message.name,
        {"status": "Sent", "custom_sent_at": frappe.utils.now_datetime()},
    )

    return wa_message


def _create_failed_log(doc, rule: dict, dedupe_key_value: str, recipient, error):
    """Write a Failed WhatsApp Message row without going through
    ``insert()`` - that would immediately retry the (already-failed or
    impossible) send via ``before_insert`` and either throw again or
    double-send.
    """
    attempted_message = None
    message_fn = rule.get("message")
    if message_fn:
        try:
            attempted_message = message_fn(doc)
        except Exception:
            pass  # best-effort - never let logging the failure raise a second one

    wa_message = frappe.get_doc(
        {
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": recipient.phone if recipient else "",
            "content_type": "text",
            "template": rule.get("template"),
            "message": attempted_message,
            "status": "Failed",
            "reference_doctype": doc.doctype,
            "reference_name": doc.name,
            "custom_notification_event": dedupe_key_value,
            "custom_recipient_doctype": recipient.recipient_doctype if recipient else None,
            "custom_recipient": recipient.recipient_name if recipient else None,
            "custom_recipient_name": recipient.display_name if recipient else None,
            "custom_sent_at": frappe.utils.now_datetime(),
            "custom_error": str(error)[:1000],
        }
    )
    wa_message.db_insert()
    return wa_message
