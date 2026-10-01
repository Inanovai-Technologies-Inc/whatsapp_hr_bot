"""The WhatsApp template that reaches an Expense Approver whose 24-hour
window is shut.

Meta will not deliver a free-form or interactive message to someone who
has not written to the business number in the last 24 hours; it accepts
the message and then drops it with error 131047 (see notify/delivery.py).
An Expense Approver is exactly that person - they read approval requests,
they do not chat - so their requests are the ones that go missing. A
template approved by Meta is the only message type that crosses the
window, which is why one exists for this.

:func:`ensure_expense_approval_template` creates it and submits it to Meta
for approval. It is idempotent, so it is safe to call from
``after_migrate`` and by hand. Meta then approves (or rejects) it
asynchronously and reports back on the ``message_template_status_update``
webhook, which frappe_whatsapp writes to the record's ``status``; nothing
here waits on that, and the notification engine keeps sending the
interactive message until the status actually reads APPROVED.

The body's four parameters are filled per claim by
``config.expense_approval_template_params``. The two Quick Reply buttons
are what the approver taps; Meta sends back only the label they pressed,
so the claim is recovered from the message it replies to - see
``expense_approval.resolve_template_reply``.
"""

import frappe

# What the template is called. Two names matter and they are not the same:
# TEMPLATE_NAME is the human one on the WhatsApp Templates record, and
# TEMPLATE_ACTUAL_NAME is the one Meta knows it by, which the doctype
# derives from it (lower case, underscores). The record's *own* name is a
# third thing again - the doctype appends the language, giving
# "Expense Approval Request-en" - so nothing looks a record up by
# TEMPLATE_NAME; :func:`get_record_name` resolves it from Meta's name.
TEMPLATE_NAME = "Expense Approval Request"
TEMPLATE_ACTUAL_NAME = "expense_approval_request"

# Meta's UTILITY category is the right one: this is a transaction update
# the recipient has to act on, not marketing.
TEMPLATE_CATEGORY = "UTILITY"
TEMPLATE_LANGUAGE_CODE = "en"

# {{1}} employee, {{2}} claim id, {{3}} grand total, {{4}} bill link.
# Kept to one line per parameter because Meta rejects a parameter
# containing a newline.
TEMPLATE_BODY = (
    "New Expense Claim awaiting your approval.\n\n"
    "Employee: {{1}}\n"
    "Claim ID: {{2}}\n"
    "Grand Total: {{3}}\n"
    "Bill / receipt: {{4}}\n\n"
    "Please Approve or Reject this claim."
)

TEMPLATE_SAMPLE_VALUES = "Asha Menon,HR-EXP-2026-00001,USD 100.00,not attached"

# Order matters: these line up with {{1}}..{{4}} above and with the keys
# config.expense_approval_template_params returns.
TEMPLATE_FIELD_NAMES = "employee_name,claim_id,grand_total,bill"

TEMPLATE_BUTTONS = ("Approve", "Reject")


def get_record_name() -> str | None:
    """The WhatsApp Templates record for this template, whatever the
    doctype named it.
    """

    if not frappe.db.exists("DocType", "WhatsApp Templates"):
        return None

    return frappe.db.get_value("WhatsApp Templates", {"actual_name": TEMPLATE_ACTUAL_NAME}, "name")


def ensure_expense_approval_template(force: bool = False) -> dict:
    """Create the approval template and submit it to Meta.

    Returns what happened, so a caller (or the log) can see whether Meta
    approved it. Never raises - a site with no WhatsApp Account
    configured, or a Meta API error, must not fail the migrate this runs
    from.

    ``force`` deletes an existing record first, which also deletes it at
    Meta (``WhatsAppTemplates.on_trash``) - use it only to re-submit after
    changing the body above, since an approved template's text cannot be
    edited in place without re-approval.
    """

    if not frappe.db.exists("DocType", "WhatsApp Templates"):
        return {"ok": False, "reason": "frappe_whatsapp is not installed"}

    record = get_record_name()
    existing = (
        frappe.db.get_value("WhatsApp Templates", record, ["name", "status"], as_dict=True)
        if record
        else None
    )

    if existing and not force:
        return {"ok": True, "created": False, "template": existing.name, "status": existing.status}

    if existing and force:
        try:
            frappe.delete_doc("WhatsApp Templates", existing.name, force=True, ignore_permissions=True)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "WhatsApp HR Bot - Could not delete the old approval template",
            )
            return {"ok": False, "reason": "the existing template could not be replaced"}

    doc = frappe.get_doc(
        {
            "doctype": "WhatsApp Templates",
            "template_name": TEMPLATE_NAME,
            "category": TEMPLATE_CATEGORY,
            "language_code": TEMPLATE_LANGUAGE_CODE,
            "template": TEMPLATE_BODY,
            "sample_values": TEMPLATE_SAMPLE_VALUES,
            "field_names": TEMPLATE_FIELD_NAMES,
            "for_doctype": "Expense Claim",
            "buttons": [
                {"button_type": "Quick Reply", "button_label": label} for label in TEMPLATE_BUTTONS
            ],
        }
    )

    try:
        # WhatsAppTemplates.after_insert posts it to Meta and stores the id
        # and the status Meta replied with.
        doc.insert(ignore_permissions=True)
    except Exception as e:
        frappe.log_error(
            frappe.get_traceback(),
            "WhatsApp HR Bot - Could not submit the approval template to Meta",
        )
        return {"ok": False, "reason": frappe.utils.strip_html(str(e))[:500]}

    doc.reload()

    return {"ok": True, "created": True, "template": doc.name, "status": doc.status, "id": doc.id}


@frappe.whitelist()
def refresh_template_status() -> dict:
    """Ask Meta what the approval template's status is now, and record it.

    Meta reports approval on the ``message_template_status_update``
    webhook, which only arrives if the site happened to be publicly
    reachable at that moment - so a site behind a tunnel that was down
    misses it and the record sits at PENDING for ever, even though Meta
    approved the template. (Every other template on this site's WhatsApp
    account was in exactly that state.) Asking Meta directly is the only
    reliable answer, so that is what this does; the engine reads the
    status this writes, and until it says APPROVED it keeps sending the
    interactive message.

    Queries this one template by name rather than using
    frappe_whatsapp's ``fetch()``, which pulls every template on the
    account and creates a WhatsApp Templates record for each - side
    effects that have nothing to do with knowing this one's status.
    """

    record = get_record_name()

    if not record:
        return {"ok": False, "reason": "the approval template does not exist yet"}

    before = frappe.db.get_value("WhatsApp Templates", record, "status")

    try:
        remote = _fetch_template_from_meta(TEMPLATE_ACTUAL_NAME)
    except Exception as e:
        frappe.log_error(
            frappe.get_traceback(),
            "WhatsApp HR Bot - Could not read the template status from Meta",
        )
        return {"ok": False, "reason": frappe.utils.strip_html(str(e))[:500], "status": before}

    if not remote:
        return {"ok": False, "reason": "Meta does not have this template", "status": before}

    status = remote.get("status")

    if status and status != before:
        frappe.db.set_value("WhatsApp Templates", record, "status", status, update_modified=False)

    return {
        "ok": True,
        "status": status,
        "changed": status != before,
        "rejected_reason": remote.get("rejected_reason"),
    }


def _fetch_template_from_meta(actual_name: str) -> dict | None:
    """The template as Meta currently holds it, or ``None``."""

    from frappe.integrations.utils import make_get_request
    from frappe_whatsapp.utils import get_whatsapp_account

    # Returns the WhatsApp Account document itself, not its name.
    account = get_whatsapp_account(account_type="outgoing")

    if not account:
        frappe.throw(frappe._("No default outgoing WhatsApp Account is configured."))

    response = make_get_request(
        f"{account.url}/{account.version}/{account.business_id}/message_templates",
        headers={
            "authorization": f"Bearer {account.get_password('token')}",
            "content-type": "application/json",
        },
        params={"name": actual_name, "limit": 10},
    )

    for template in response.get("data") or []:
        if template.get("name") == actual_name:
            return template

    return None
