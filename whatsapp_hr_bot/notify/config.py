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


def _expense_claim_message(doc, outcome: str) -> str:
    return (
        f"Hello {doc.employee_name},\n\n"
        f"Your Expense Claim *{doc.name}* has been {outcome}.\n"
        f"Amount: {doc.get('total_claimed_amount', 0):,.2f}\n\n"
        + ("It will be processed for payment shortly." if outcome == "approved" else "Please contact HR for details.")
    )


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


NOTIFICATION_RULES = [
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
        "events": ["on_submit"],
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
