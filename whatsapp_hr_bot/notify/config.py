"""Registry of WhatsApp notification rules.

This module (and notify/engine.py, api.py, the fixtures/custom_field.json
Custom Fields, and the per-DocType public/js/*.js "Send WhatsApp" buttons)
is a self-contained addition to this app - it does not touch
whatsapp_handler.py or the existing "WhatsApp Message" -> after_insert
leave-bot hook in hooks.py. It reuses the existing WhatsApp Account and
the frappe_whatsapp WhatsApp Message doctype as its send log, exactly
like whatsapp_handler.py already does.

Each rule maps a DocType + one or more doc events to:

- a ``recipient`` block describing *how* to find the phone number
  (which link field to follow, which doctype it points to, and which
  fields on that doctype/the document itself may hold a phone number)
- **either** a ``message`` builder - a ``lambda doc: "..."`` that
  composes a plain-text WhatsApp message from the document - **or** a
  ``template`` (a `WhatsApp Templates` record name, already approved
  with Meta). ``message`` needs no template approval, so it's the
  faster way to get going; switch a rule to ``template`` once Meta
  approves one, for messages sent outside Meta's 24-hour customer
  service window (see the note below).
- an optional ``condition`` - only send when it returns True
- an optional ``dedupe_key`` - used to tell "the same notification for
  this event" apart from "a different notification for the same
  document" (e.g. Expense Claim approved vs rejected), so repeated
  triggers of the same event never create duplicate messages while a
  genuinely different outcome still gets its own message.
- an optional ``attach_bill`` - send the document's bill/receipt
  (``custom_bill_attachment``, see expense_attachment.py) to the same
  recipient as a second, media message right after this rule's own. Used
  by the Expense Claim approval request, whose interactive Approve/Reject
  message cannot carry an attachment itself; see
  ``engine._send_bill_attachment``. Unrelated to ``attach_pdf``, which
  attaches a rendered print format to the rule's own message.
- an optional ``preference_field`` - an Employee Check fieldname the
  recipient must have enabled (or leave unset/missing) for the message
  to send; see ``engine._run_rule``. Backed by the "WhatsApp
  Notification Categories" fields added in fixtures/custom_field.json
  (``custom_whatsapp_leave``, ``_onboarding``, ``_attendance``,
  ``_finance``, ``_hr_announcements``) - one Check per category,
  default enabled so an un-configured site keeps sending exactly as
  before. Only Expense Claim (``custom_whatsapp_finance``) is wired to
  one of these today; the rest exist for future rules to opt into via
  the same ``preference_field`` key, no engine changes required. These
  are unrelated to the ``custom_whatsapp_apply_leave`` /
  ``_leave_balance`` / ``_my_requests`` / ``_my_onboarding`` checkboxes
  above them on the Employee form, which control the *inbound* chatbot
  menu (whatsapp_handler.py), not this outbound engine.

Meta's 24-hour rule
--------------------
A free-form ``message`` can only be delivered if the recipient has
messaged your WhatsApp Business number in the last 24 hours - Meta
rejects unsolicited business-initiated text with a "re-engagement"
error (code 131047) otherwise. An approved ``template`` is the only
way to reach someone outside that window. Until your templates are
approved, test with a number that has just messaged your business
number, or expect that error for a cold number - it means the *code*
worked and Meta's messaging policy is what's blocking it.

To support a new DocType, add one entry here (or call
``register_rule`` from another app) and point the relevant doc
event(s) at ``whatsapp_hr_bot.notify.engine.on_doc_event`` in
``hooks.py``. No new doctype and no new send/log code is needed.

``recipient`` keys:
    link_field          fieldname on the triggering document that links to
                         the recipient's master (e.g. "supplier"). Omit to
                         use the triggering document itself as the
                         recipient record.
    doctype             doctype of the linked recipient record.
    phone_fields        fieldnames tried in order, on the recipient record.
    name_field          fieldname (checked on the document, then on the
                         recipient record) used as the display name for
                         logging.
    user_fallback_field fieldname on the recipient record that links to a
                         User (e.g. "user_id") - tried if none of
                         phone_fields has a value.
"""

import frappe


def _po_message(doc) -> str:
    return (
        f"Hello {doc.supplier_name},\n\n"
        f"A new Purchase Order *{doc.name}* has been raised with you.\n"
        f"Amount: {doc.get('currency', '')} {doc.get('grand_total', 0):,.2f}\n"
        f"Delivery Date: {doc.get('schedule_date', '-')}\n\n"
        "Please confirm receipt. Thank you."
    )


def _so_message(doc) -> str:
    return (
        f"Hello {doc.customer_name},\n\n"
        f"Your Sales Order *{doc.name}* has been confirmed.\n"
        f"Amount: {doc.get('currency', '')} {doc.get('grand_total', 0):,.2f}\n"
        f"Delivery Date: {doc.get('delivery_date', '-')}\n\n"
        "Thank you for your business!"
    )


def _expense_claim_currency(doc) -> str:
    """Currency this claim's amounts are in - ``custom_expense_currency``,
    which the employee picked over WhatsApp and every Currency field on
    the doctype now reads (see expense_currency.py). Named in the message
    so a claim raised in USD is never read as rupees.
    """
    from whatsapp_hr_bot import expense_currency

    return expense_currency.get_claim_currency(doc)


def _expense_claim_message(doc, outcome: str) -> str:
    return (
        f"Hello {doc.employee_name},\n\n"
        f"Your Expense Claim *{doc.name}* has been {outcome}.\n"
        f"Amount: {_expense_claim_currency(doc)} {doc.get('total_claimed_amount', 0):,.2f}\n\n"
        + ("It will be processed for payment shortly." if outcome == "approved" else "Please contact HR for details.")
    )


def _expense_claim_grand_total(doc) -> float:
    # The stock ``grand_total`` is driven by each row's sanctioned_amount.
    # Every claim this app creates now sets that to the claimed amount -
    # the same default the desk form applies (see whatsapp_handler.py's
    # expense claim creation) - so grand_total is correct from the moment
    # the claim is inserted, and is what gets reported here.
    #
    # The fallback covers claims created before that, and any other route
    # that leaves sanctioned_amount at 0: there the claimed amount is the
    # only total that means anything. Same fallback the "Expense Claim
    # WhatsApp PDF" print format applies.
    grand_total = doc.get("grand_total") or 0

    if grand_total:
        return grand_total

    return (
        (doc.get("total_claimed_amount") or 0)
        + (doc.get("total_taxes_and_charges") or 0)
        - (doc.get("total_advance_amount") or 0)
    )


def expense_approval_request_message(doc) -> str:
    return (
        "New Expense Claim awaiting your approval:\n\n"
        f"Employee: {doc.employee_name}\n"
        f"Claim ID: {doc.name}\n"
        f"Grand Total: {_expense_claim_currency(doc)} {_expense_claim_grand_total(doc):,.2f}\n\n"
        "Please Approve or Reject this claim."
    )


# The template used to reach an Expense Approver whose 24-hour window is
# shut - named as Meta knows it, since the record's own name depends on the
# language the doctype appended. Created and submitted by
# notify/templates.py; until Meta approves it the engine keeps sending the
# interactive message instead.
from whatsapp_hr_bot.notify.templates import TEMPLATE_ACTUAL_NAME as EXPENSE_APPROVAL_TEMPLATE


def expense_approval_template_params(doc) -> dict:
    """Body parameters for :data:`EXPENSE_APPROVAL_TEMPLATE`, in order.

    Meta rejects a parameter that is empty or carries a newline, so every
    one of these is a single non-empty line. The bill goes in as a link
    rather than an attachment: a template's media has to be declared in
    its header, which would mean one template for claims with a bill and
    another for claims without.
    """
    from whatsapp_hr_bot import expense_attachment

    bill = None

    try:
        bill = expense_attachment.get_bill_delivery_url(doc)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"WhatsApp Notify: bill link failed for {doc.doctype} {doc.name}",
        )

    return {
        "employee_name": doc.employee_name or doc.employee,
        "claim_id": doc.name,
        "grand_total": (
            f"{_expense_claim_currency(doc)} {_expense_claim_grand_total(doc):,.2f}"
        ),
        "bill": bill or "not attached",
    }


def expense_approval_buttons(doc) -> list:
    # button id carries the claim name after ":" - same convention as
    # the leave flow's "leave_type:<value>" buttons (whatsapp_handler.py).
    # Parsed back out in expense_approval.handle_expense_approval_button.
    return [
        {"id": f"approve_expense:{doc.name}", "title": "Approve"},
        {"id": f"reject_expense:{doc.name}", "title": "Reject"},
    ]


def _purchase_receipt_message(doc) -> str:
    return (
        f"Hello {doc.supplier_name},\n\n"
        f"We confirm receipt of goods against *{doc.name}* "
        f"(against {doc.get('purchase_order') or 'your Purchase Order'}) on {doc.get('posting_date', '-')}.\n"
        f"Amount: {doc.get('currency', '')} {doc.get('grand_total', 0):,.2f}\n\n"
        "Thank you for the delivery."
    )


def _delivery_note_message(doc) -> str:
    return (
        f"Hello {doc.customer_name},\n\n"
        f"Your order has been shipped/delivered via *{doc.name}* on {doc.get('posting_date', '-')}.\n"
        f"Amount: {doc.get('currency', '')} {doc.get('grand_total', 0):,.2f}\n\n"
        "Thank you for shopping with us!"
    )


def _sales_invoice_message(doc) -> str:
    return (
        f"Hello {doc.customer_name},\n\n"
        f"Invoice *{doc.name}* for {doc.get('currency', '')} {doc.get('grand_total', 0):,.2f} has been raised.\n"
        f"Due Date: {doc.get('due_date', '-')}\n"
        f"Outstanding: {doc.get('currency', '')} {doc.get('outstanding_amount', 0):,.2f}\n\n"
        "Thank you for your business!"
    )


def _payment_received_message(doc) -> str:
    return (
        f"Hello {doc.party_name},\n\n"
        f"We have received your payment of {doc.get('paid_from_account_currency') or doc.get('paid_to_account_currency') or ''} "
        f"{doc.get('paid_amount', 0):,.2f} (Payment Entry *{doc.name}*) on {doc.get('posting_date', '-')}.\n\n"
        "Thank you!"
    )


def _payment_made_message(doc) -> str:
    return (
        f"Hello {doc.party_name},\n\n"
        f"We have processed a payment of {doc.get('paid_to_account_currency') or doc.get('paid_from_account_currency') or ''} "
        f"{doc.get('paid_amount', 0):,.2f} to you (Payment Entry *{doc.name}*) on {doc.get('posting_date', '-')}.\n\n"
        "Thank you for your business."
    )


def _reimbursement_message(doc) -> str:
    return (
        f"Hello {doc.party_name},\n\n"
        f"Your expense reimbursement of {doc.get('paid_to_account_currency') or doc.get('paid_from_account_currency') or ''} "
        f"{doc.get('paid_amount', 0):,.2f} has been paid out (Payment Entry *{doc.name}*) on {doc.get('posting_date', '-')}.\n\n"
        "Thank you."
    )


def _employee_checkin_message(doc) -> str:
    from frappe.utils import format_datetime

    return (
        f"Hello {doc.employee_name},\n\n"
        f"Your attendance has been recorded.\n"
        f"Check-in Type: {doc.log_type}\n"
        f"Check-in Time: {format_datetime(doc.time)}"
    )


def _upcoming_holiday_message(doc) -> str:
    from frappe.utils import formatdate

    description = frappe.utils.strip_html(str(doc.description or "")).strip()
    return (
        f"Hello {doc.employee_name},\n\n"
        f"Upcoming holiday: {description or 'Holiday'} on {formatdate(doc.holiday_date, 'd MMMM')}."
    )


NOTIFICATION_RULES = [
    {
        "doctype": "Employee Checkin",
        "events": ["after_insert"],
        "message": _employee_checkin_message,
        "recipient": {
            "link_field": "employee",
            "doctype": "Employee",
            "phone_fields": ["cell_number"],
            "name_field": "employee_name",
            "user_fallback_field": "user_id",
        },
        "preference_field": "custom_whatsapp_attendance",
        "dedupe_key": lambda doc: "checkin",
    },
    {
        "doctype": "Holiday List",
        "events": ["daily"],
        "message": _upcoming_holiday_message,
        "recipient": {
            "link_field": "employee",
            "doctype": "Employee",
            "phone_fields": ["cell_number"],
            "name_field": "employee_name",
            "user_fallback_field": "user_id",
        },
        "preference_field": "custom_whatsapp_hr_announcements",
        "dedupe_key": lambda doc: f"holiday:{doc.holiday_row}:{doc.employee}",
    },
    {
        "doctype": "Purchase Order",
        "events": ["on_submit"],
        "message": _po_message,
        "recipient": {
            "link_field": "supplier",
            "doctype": "Supplier",
            "phone_fields": ["mobile_no"],
            "name_field": "supplier_name",
        },
        "condition": lambda doc: doc.docstatus == 1,
        "dedupe_key": lambda doc: "submitted",
    },
    {
        "doctype": "Sales Order",
        "events": ["on_submit"],
        "message": _so_message,
        "recipient": {
            "link_field": "customer",
            "doctype": "Customer",
            "phone_fields": ["mobile_no"],
            "name_field": "customer_name",
        },
        "condition": lambda doc: doc.docstatus == 1,
        "dedupe_key": lambda doc: "submitted",
    },
    {
        "doctype": "Expense Claim",
        # "on_submit"/"on_update_after_submit" are not in hooks.py's
        # doc_events for Expense Claim, so these two rules only ever fire
        # via the manual "Send WhatsApp" button, or explicitly from
        # expense_approval.handle_expense_approval_button (the WhatsApp
        # Approve/Reject action) - never automatically on submit.
        "events": ["on_submit", "on_update_after_submit"],
        "message": lambda doc: _expense_claim_message(doc, "approved"),
        "recipient": {
            "link_field": "employee",
            "doctype": "Employee",
            "phone_fields": ["cell_number"],
            "name_field": "employee_name",
            "user_fallback_field": "user_id",
        },
        "preference_field": "custom_whatsapp_finance",
        "condition": lambda doc: doc.docstatus == 1 and doc.approval_status == "Approved",
        "dedupe_key": lambda doc: "approved",
        "attach_pdf": True,
        "print_format": "Expense Claim WhatsApp PDF",
    },
    {
        "doctype": "Expense Claim",
        "events": ["on_submit", "on_update_after_submit"],
        "message": lambda doc: _expense_claim_message(doc, "rejected"),
        "recipient": {
            "link_field": "employee",
            "doctype": "Employee",
            "phone_fields": ["cell_number"],
            "name_field": "employee_name",
            "user_fallback_field": "user_id",
        },
        "preference_field": "custom_whatsapp_finance",
        "condition": lambda doc: doc.docstatus == 1 and doc.approval_status == "Rejected",
        "dedupe_key": lambda doc: "rejected",
        "attach_pdf": True,
        "print_format": "Expense Claim WhatsApp PDF",
    },
    {
        # Sent to the Expense Approver (a User, not an Employee - hence
        # the plain "User" recipient doctype) the moment a claim is
        # created, whether from the desk UI or the WhatsApp chatbot's own
        # "apply expense claim" flow (which explicitly inserts in Draft
        # and never submits - see whatsapp_handler.py). This is the only
        # Expense Claim rule wired to a real doc_event (after_insert, in
        # hooks.py) - the interactive Approve/Reject buttons it sends are
        # handled by expense_approval.handle_expense_approval_button,
        # which is what actually moves the claim to Approved/Rejected and
        # triggers the two rules above.
        "doctype": "Expense Claim",
        "events": ["after_insert"],
        "message": expense_approval_request_message,
        "buttons": expense_approval_buttons,
        "recipient": {
            "link_field": "expense_approver",
            "doctype": "User",
            "phone_fields": ["mobile_no", "phone"],
        },
        "condition": lambda doc: doc.docstatus == 0 and doc.approval_status == "Draft",
        "dedupe_key": lambda doc: "approval_requested",
        # The approver decides on the claim from this message, so they get
        # the employee's own bill/receipt with it - the very file attached
        # to the claim, sent as a second (media) message because an
        # interactive one carries no attachment. See
        # engine._send_bill_attachment and
        # expense_attachment.get_bill_delivery_url; a claim with no bill
        # simply sends the request alone, as before.
        "attach_bill": True,
        # An Expense Approver is not someone who chats with the bot, so
        # their 24-hour window is usually shut - and Meta drops a
        # free-form or interactive message sent into that with error
        # 131047, after its API has already accepted it. This template is
        # the only thing it will deliver there. The engine uses it instead
        # of the interactive message exactly when the approver is out of
        # window and Meta has approved it (engine._build_send_fields), and
        # a tap on its Approve/Reject buttons comes back through
        # expense_approval.resolve_template_reply.
        "fallback_template": EXPENSE_APPROVAL_TEMPLATE,
        "template_params": expense_approval_template_params,
    },
    {
        # Confirms to the supplier that the goods against their
        # Purchase Order were received.
        "doctype": "Purchase Receipt",
        "events": ["on_submit"],
        "message": _purchase_receipt_message,
        "recipient": {
            "link_field": "supplier",
            "doctype": "Supplier",
            "phone_fields": ["mobile_no"],
            "name_field": "supplier_name",
        },
        "condition": lambda doc: doc.docstatus == 1,
        "dedupe_key": lambda doc: "received",
    },
    {
        # Sales-side counterpart of Purchase Receipt: tells the
        # customer their order has shipped/been delivered.
        "doctype": "Delivery Note",
        "events": ["on_submit"],
        "message": _delivery_note_message,
        "recipient": {
            "link_field": "customer",
            "doctype": "Customer",
            "phone_fields": ["mobile_no"],
            "name_field": "customer_name",
        },
        "condition": lambda doc: doc.docstatus == 1,
        "dedupe_key": lambda doc: "delivered",
    },
    {
        "doctype": "Sales Invoice",
        "events": ["on_submit"],
        "message": _sales_invoice_message,
        "recipient": {
            "link_field": "customer",
            "doctype": "Customer",
            "phone_fields": ["mobile_no"],
            "name_field": "customer_name",
        },
        "condition": lambda doc: doc.docstatus == 1,
        "dedupe_key": lambda doc: "invoiced",
    },
    {
        # Payment Entry's recipient ("party") can be a Customer, a
        # Supplier, or an Employee (expense claim reimbursements)
        # depending on party_type - split into one rule per
        # party_type/payment_type combination rather than teaching the
        # engine dynamic-doctype recipients, since the condition
        # already pins down which one applies. dedupe_key is suffixed
        # per party_type so two rules can never dedupe against each
        # other even though only one can ever match a given document.
        "doctype": "Payment Entry",
        "events": ["on_submit"],
        "message": _payment_received_message,
        "recipient": {
            "link_field": "party",
            "doctype": "Customer",
            "phone_fields": ["mobile_no"],
            "name_field": "party_name",
        },
        "condition": lambda doc: doc.docstatus == 1
        and doc.payment_type == "Receive"
        and doc.party_type == "Customer",
        "dedupe_key": lambda doc: "receive_customer",
    },
    {
        "doctype": "Payment Entry",
        "events": ["on_submit"],
        "message": _payment_made_message,
        "recipient": {
            "link_field": "party",
            "doctype": "Supplier",
            "phone_fields": ["mobile_no"],
            "name_field": "party_name",
        },
        "condition": lambda doc: doc.docstatus == 1
        and doc.payment_type == "Pay"
        and doc.party_type == "Supplier",
        "dedupe_key": lambda doc: "pay_supplier",
    },
    {
        # Expense Claim reimbursements are paid out via a Payment
        # Entry with party_type "Employee" - same "payment made"
        # message, employee phone resolution (cell_number, falling
        # back to the linked User) like the Expense Claim rules above.
        "doctype": "Payment Entry",
        # No "on_submit" here (unlike every other Payment Entry/rule
        # above): the reimbursement confirmation + PDF must only go out
        # when someone clicks "Send WhatsApp" on the Payment Entry, never
        # automatically on payment. get_rules(..., event=None) - used by
        # the manual button - ignores this list, so the button still
        # works; only the doc_events-triggered on_submit lookup (which
        # filters by event) skips this rule.
        "events": [],
        "message": _reimbursement_message,
        "recipient": {
            "link_field": "party",
            "doctype": "Employee",
            "phone_fields": ["cell_number"],
            "name_field": "party_name",
            "user_fallback_field": "user_id",
        },
        "condition": lambda doc: doc.docstatus == 1
        and doc.payment_type == "Pay"
        and doc.party_type == "Employee",
        "dedupe_key": lambda doc: "pay_employee",
        # Attach the (default) Payment Entry PDF, same mechanism as the
        # Expense Claim rules above.
        "attach_pdf": True,
        "print_format": None,
    },
]


def register_rule(rule: dict) -> None:
    """Register an additional notification rule at runtime.

    Lets another app plug a DocType into this engine without editing
    this file - call this from that app's own module import, then wire
    the doc event(s) to ``whatsapp_hr_bot.notify.engine.on_doc_event``.
    """
    NOTIFICATION_RULES.append(rule)


def get_rules(doctype: str, event: str | None = None) -> list[dict]:
    """Rules configured for a doctype, optionally filtered by event.

    ``event=None`` returns every rule for the doctype regardless of
    which event(s) it is wired to - used by manual/button-triggered
    sends where there is no specific triggering event.
    """
    return [
        rule
        for rule in NOTIFICATION_RULES
        if rule["doctype"] == doctype and (event is None or event in rule["events"])
    ]
