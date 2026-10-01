import json
import frappe

from frappe import _
from frappe.utils import flt, getdate, today

from hrms.hr.doctype.leave_application.leave_application import (
    get_leave_balance_on,
    get_leave_details,
    get_number_of_leave_days,
)


# ============================================================
# CONSTANTS
# ============================================================

STATE_PREFIX = "whatsapp_hr_bot:"
PROCESSED_PREFIX = "whatsapp_hr_bot:processed:"

SESSION_TIMEOUT = 1800
PROCESSED_TIMEOUT = 3600

# Text that opens the leave flow without going through "Hii" first,
# mirroring ONBOARDING_KEYWORDS in onboarding.py. Matched on the whole
# normalised message, so "leave balance" is not caught by "leave".

LEAVE_KEYWORDS = [
    "leave",
    "apply leave",
    "apply_leave",
    "apply for leave",
    "leave apply",
    "leave application",
    "take leave",
    "new leave",
]

# Step of the expense claim flow that offers the optional bill/receipt
# upload, between "remark" and "confirmation".

BILL_STEP = "bill_attachment"

# Replies that decline the optional bill/receipt. Kept separate from the
# confirm/cancel words: at BILL_STEP "no" means "continue without an
# attachment", never "cancel the claim".

SKIP_BILL_WORDS = [
    "no",
    "n",
    "nope",
    "skip",
    "no thanks",
    "no thank you",
    "not now",
    "later",
    "continue",
]

# Inbound content types that carry a file. frappe_whatsapp downloads
# these into a File attached to the WhatsApp Message (its
# utils/webhook.py), which handle_inbound_whatsapp_file picks up.

MEDIA_CONTENT_TYPES = [
    "image",
    "document",
    "audio",
    "video",
]


# ============================================================
# MAIN WHATSAPP MESSAGE HANDLER
# ============================================================

def handle_whatsapp_message(doc, method=None):

    # --------------------------------------------------------
    # Only process incoming messages
    # --------------------------------------------------------

    if doc.get("type") != "Incoming":
        return

    phone = doc.get("from")

    if not phone:
        return

    phone = str(phone).strip()

    # --------------------------------------------------------
    # Get message identifier
    # --------------------------------------------------------

    message_id = (
        doc.get("message_id")
        or doc.get("id")
        or doc.get("name")
    )

    if message_id:

        cache_key = f"{PROCESSED_PREFIX}{message_id}"

        # ----------------------------------------------------
        # Duplicate protection
        # ----------------------------------------------------

        if frappe.cache().get_value(cache_key):
            return

        frappe.cache().set_value(
            cache_key,
            "1",
            expires_in_sec=PROCESSED_TIMEOUT
        )

    # --------------------------------------------------------
    # Content information
    # --------------------------------------------------------

    content_type = (
        doc.get("content_type") or ""
    ).strip().lower()

    message = (
        doc.get("message") or ""
    ).strip()

    # --------------------------------------------------------
    # Extract interactive/button ID
    # --------------------------------------------------------

    button_id = extract_button_id(doc)

    # --------------------------------------------------------
    # Process button response
    # --------------------------------------------------------

    if button_id:

        handle_button(
            doc,
            phone,
            button_id
        )

        return

    # --------------------------------------------------------
    # Empty message
    # --------------------------------------------------------

    if not message:
        return

    # --------------------------------------------------------
    # Normalize text
    # --------------------------------------------------------

    text = normalize_text(message)

    attendance_command = get_attendance_command(text)
    if attendance_command:
        handle_attendance_command(doc, phone, attendance_command)
        return

    if text in {"upcoming holiday", "next holiday", "holiday"}:
        handle_upcoming_holiday_command(doc, phone)
        return

    # --------------------------------------------------------
    # Expense approvals, pulled by the approver
    #
    # Checked here, before the session/state routing below, for the
    # same reason the greeting is: it has to work as a way in even
    # when the sender has a conversation open. An approver messaging
    # the bot at all is what re-opens Meta's 24-hour window, so this
    # is how an approval request that Meta refused to deliver
    # actually reaches them - see expense_approval.py.
    #
    # Only recognised for a number that belongs to somebody actually
    # named as an Expense Approver, so for everybody else "approvals"
    # falls through to the existing handling untouched.
    # --------------------------------------------------------

    from whatsapp_hr_bot import expense_approval

    if expense_approval.is_pending_approval_keyword(text) and (
        expense_approval.is_expense_approver(phone)
    ):

        expense_approval.send_pending_approvals(doc, phone)

        return

    # --------------------------------------------------------
    # Main menu / restart
    # --------------------------------------------------------

    if text in [
        "hi",
        "hii",
        "hiii",
        "hello",
        "hey",
        "start",
        "menu"
    ]:

        clear_state(phone)

        send_main_menu(doc)

        return

    # --------------------------------------------------------
    # Onboarding keywords
    #
    # These open the onboarding menu directly, without the
    # employee having to type *Hii* first. Handled before the
    # state check (like the greeting above) so they also work
    # as an escape hatch out of a stale session.
    # --------------------------------------------------------

    from whatsapp_hr_bot import onboarding

    if onboarding.is_onboarding_keyword(text):

        clear_state(phone)

        onboarding.handle_onboarding_message(
            doc,
            phone,
            onboarding.BUTTON_MY_ONBOARDING
        )

        return

    # --------------------------------------------------------
    # Leave keywords
    #
    # Same shortcut for the leave flow: typing *leave* or
    # *apply leave* starts it straight away instead of making
    # the employee restart with *Hii*. Reuses the existing
    # apply_leave handler rather than repeating its logic.
    # --------------------------------------------------------

    if is_leave_keyword(text):

        clear_state(phone)

        handle_button(
            doc,
            phone,
            "apply_leave"
        )

        return

    # --------------------------------------------------------
    # Existing conversation
    # --------------------------------------------------------

    state = get_state(phone)

    if state:

        # Leave sessions carry no "flow" key (and now carry
        # "leave"), so they keep routing to handle_leave_flow
        # exactly as before.

        flow = state.get("flow")

        if flow == onboarding.ONBOARDING_FLOW:

            onboarding.handle_onboarding_message(
                doc,
                phone,
                message
            )

            return

        if flow == "expense_claim":

            handle_expense_claim_flow(
                doc,
                phone,
                message
            )

            return

        handle_leave_flow(
            doc,
            phone,
            message
        )

        return

    # --------------------------------------------------------
    # Onboarding task status update
    #
    # "<activity> : <status>" also works with no session open,
    # so an employee can answer an onboarding reminder straight
    # away instead of walking the menu first. Only a message
    # that parses as a status update is taken here - anything
    # else falls through to the prompt below.
    #
    # Checked after the state block above, so a leave session in
    # progress keeps routing to handle_leave_flow untouched.
    # --------------------------------------------------------

    if onboarding.handle_status_update(doc, phone, message):
        return

    # --------------------------------------------------------
    # No active conversation
    # --------------------------------------------------------

    send_text(
        doc,
        "I couldn't find an active request.\n\n"
        "Please type *Hii* to start."
    )


# ============================================================
# EXTRACT INTERACTIVE BUTTON ID
# ============================================================

def extract_button_id(doc):

    direct_fields = [
        "button_id",
        "selected_id",
        "reply_id",
        "interactive_id",
        "interactive_reply_id",
        "button_reply_id",
        "interactive_button_id",
        "list_reply_id",
        "selected_button_id",
    ]

    for field in direct_fields:

        value = doc.get(field)

        if value:

            result = extract_valid_id(value)

            if result:
                return result

    payload_fields = [
        "payload",
        "interactive",
        "button",
        "button_reply",
        "interactive_reply",
        "reply",
        "data",
        "response",
        "raw_payload",
        "whatsapp_payload",
    ]

    for field in payload_fields:

        value = doc.get(field)

        if not value:
            continue

        result = find_valid_id_recursive(value)

        if result:
            return result

    # --------------------------------------------------------
    # Search complete document
    # --------------------------------------------------------

    try:

        document_data = doc.as_dict()

        result = find_valid_id_recursive(
            document_data
        )

        if result:
            return result

    except Exception:
        pass

    # --------------------------------------------------------
    # Quick Reply on an approved template
    #
    # Checked before the text fallbacks below because there is nothing
    # in the message itself to match on: a template's buttons carry only
    # their label ("Approve"), and which Expense Claim it belongs to
    # comes from the message it replies to. See
    # expense_approval.resolve_template_reply - it returns None for
    # every other inbound message.
    # --------------------------------------------------------

    from whatsapp_hr_bot import expense_approval

    template_reply = expense_approval.resolve_template_reply(doc)

    if template_reply:
        return template_reply

    # --------------------------------------------------------
    # Fallback to message
    # --------------------------------------------------------

    message = (
        doc.get("message") or ""
    ).strip()

    if message:

        state = get_state(
            doc.get("from")
        )

        if state:

            step = state.get("step")
            flow = state.get("flow")

            # ------------------------------------------------
            # Leave type
            # ------------------------------------------------

            if step == "leave_type":

                leave_type_id = (
                    find_leave_type_from_message(
                        message
                    )
                )

                if leave_type_id:
                    return leave_type_id

            # ------------------------------------------------
            # Expense claim type
            # ------------------------------------------------

            if step == "expense_type":

                expense_type_id = (
                    find_expense_type_from_message(
                        message
                    )
                )

                if expense_type_id:
                    return expense_type_id

            # ------------------------------------------------
            # Confirmation
            #
            # Which confirm/cancel id a typed "confirm"/"cancel"
            # maps to depends on which flow's session is open -
            # each flow's own confirmation step (see
            # handle_leave_flow / handle_expense_claim_flow) also
            # accepts these words directly, so this fallback only
            # matters for payload shapes extract_valid_id couldn't
            # parse above.
            # ------------------------------------------------

            normalized = normalize_text(message)

            if flow == "expense_claim":

                # At the bill/receipt step "No" means "continue
                # without an attachment", not "cancel the claim" - that
                # step parses those replies itself in
                # handle_expense_claim_flow. Everything else, "cancel"
                # included, keeps working from this step as before.

                if step == BILL_STEP and normalized in SKIP_BILL_WORDS:
                    return None

                if normalized in ["confirm", "yes", "submit"]:
                    return "confirm_expense_claim"

                if normalized in ["cancel", "no"]:
                    return "cancel_expense_claim"

            else:

                if normalized in [
                    "confirm",
                    "yes",
                    "submit",
                    "confirm leave"
                ]:

                    return "confirm_leave"

                if normalized in [
                    "cancel",
                    "no",
                    "cancel leave"
                ]:

                    return "cancel_leave"

    return None


# ============================================================
# VALID INTERACTIVE ID
# ============================================================

def extract_valid_id(value):

    if value is None:
        return None

    if isinstance(value, dict):

        return find_valid_id_recursive(value)

    if isinstance(value, str):

        value = value.strip()

        if not value:
            return None

        if is_valid_button_id(value):
            return value

        try:

            parsed = json.loads(value)

            return find_valid_id_recursive(parsed)

        except Exception:
            pass

    return None


# ============================================================
# RECURSIVE ID SEARCH
# ============================================================

def find_valid_id_recursive(value):

    if value is None:
        return None

    if isinstance(value, dict):

        priority_keys = [
            "id",
            "button_id",
            "selected_id",
            "reply_id",
            "interactive_id",
            "payload",
            "title",
            "text"
        ]

        for key in priority_keys:

            if key in value:

                result = find_valid_id_recursive(
                    value.get(key)
                )

                if result:
                    return result

        for child in value.values():

            result = find_valid_id_recursive(
                child
            )

            if result:
                return result

        return None

    if isinstance(value, (list, tuple)):

        for item in value:

            result = find_valid_id_recursive(
                item
            )

            if result:
                return result

        return None

    if isinstance(value, str):

        value = value.strip()

        if is_valid_button_id(value):
            return value

        try:

            parsed = json.loads(value)

            return find_valid_id_recursive(
                parsed
            )

        except Exception:
            return None

    return None


# ============================================================
# VALID BUTTON IDS
# ============================================================

def is_valid_button_id(value):

    if not isinstance(value, str):
        return False

    value = value.strip()

    if value in [
        "apply_leave",
        "leave_balance",
        "my_requests",
        "confirm_leave",
        "cancel_leave",
        "expense_claim_status",
        "apply_expense_claim",
        "confirm_expense_claim",
        "cancel_expense_claim",
    ]:
        return True

    if value.startswith("leave_type:"):
        return True

    if value.startswith("expense_type:"):
        return True

    from whatsapp_hr_bot import expense_currency

    if value.startswith(expense_currency.CURRENCY_BUTTON_PREFIX):
        return True

    from whatsapp_hr_bot import onboarding

    if onboarding.is_onboarding_button_id(value):
        return True

    from whatsapp_hr_bot import expense_approval

    if expense_approval.is_expense_approval_button_id(value):
        return True

    return False


# ============================================================
# LEAVE KEYWORDS
# ============================================================

def is_leave_keyword(text):
    """``text`` is expected to be normalised by ``normalize_text``."""

    return text in LEAVE_KEYWORDS


def get_attendance_command(text):
    if text in {"check-in", "check in", "check-inn", "check inn", "checkin"}:
        return "IN"
    if text in {"check-out", "check out", "checkout"}:
        return "OUT"
    return None


def handle_attendance_command(doc, phone, log_type):
    employee = get_employee(phone)
    if not employee:
        send_text(doc, "I could not find an active employee profile for your WhatsApp number. Please contact HR.")
        return

    from frappe.utils import format_datetime, now_datetime

    checkin = frappe.new_doc("Employee Checkin")
    checkin.employee = employee
    checkin.log_type = log_type
    checkin.time = now_datetime()
    checkin.flags.skip_whatsapp_notification = True

    try:
        checkin.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WhatsApp HR Bot - Employee Checkin Failed")
        send_text(doc, "I could not record your attendance. Please contact HR or try again later.")
        return

    send_text(
        doc,
        f"{log_type.title()} recorded successfully.\n"
        f"Employee: {checkin.employee_name}\n"
        f"Type: {log_type}\n"
        f"Time: {format_datetime(checkin.time)}",
    )


def handle_upcoming_holiday_command(doc, phone):
    employee = get_employee(phone)
    if not employee:
        send_text(doc, "I could not find an active employee profile for your WhatsApp number. Please contact HR.")
        return

    from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee
    from frappe.utils import formatdate

    holiday_list = get_holiday_list_for_employee(employee, raise_exception=False)
    if not holiday_list:
        send_text(doc, "No Holiday List is assigned to your employee profile or company.")
        return

    holiday = frappe.get_all(
        "Holiday",
        filters={"parent": holiday_list, "holiday_date": [">=", today()]},
        fields=["holiday_date", "description"],
        order_by="holiday_date asc",
        limit=1,
    )
    if not holiday:
        send_text(doc, "There are no upcoming holidays on your Holiday List.")
        return

    next_holiday = holiday[0]
    description = frappe.utils.strip_html(str(next_holiday.description or "")).strip()
    send_text(
        doc,
        f"Your next holiday is {description or 'Holiday'} "
        f"on {formatdate(next_holiday.holiday_date, 'd MMMM YYYY')}.",
    )


# ============================================================
# FIND LEAVE TYPE FROM BUTTON TITLE
# ============================================================

def find_leave_type_from_message(message):

    message = (
        message or ""
    ).strip()

    if not message:
        return None

    leave_type = frappe.db.exists(
        "Leave Type",
        message
    )

    if leave_type:

        return f"leave_type:{message}"

    lines = [
        line.strip()
        for line in message.splitlines()
        if line.strip()
    ]

    for line in reversed(lines):

        exists = frappe.db.exists(
            "Leave Type",
            line
        )

        if exists:

            return f"leave_type:{line}"

    return None


# ============================================================
# FIND EXPENSE CLAIM TYPE FROM BUTTON TITLE
# ============================================================

def find_expense_type_from_message(message):

    message = (
        message or ""
    ).strip()

    if not message:
        return None

    expense_type = frappe.db.exists(
        "Expense Claim Type",
        message
    )

    if expense_type:

        return f"expense_type:{message}"

    lines = [
        line.strip()
        for line in message.splitlines()
        if line.strip()
    ]

    for line in reversed(lines):

        exists = frappe.db.exists(
            "Expense Claim Type",
            line
        )

        if exists:

            return f"expense_type:{line}"

    return None


# ============================================================
# MAIN MENU
# ============================================================

def send_main_menu(doc):

    # With more than 3 options frappe_whatsapp renders this as a
    # WhatsApp *list* message instead of reply buttons (see
    # WhatsAppMessage.send -> content_type == "interactive"), so the
    # same helper covers both. Descriptions are filled in because
    # list rows go out with a "description" key either way.
    #
    # The button *ids* are unchanged - only the titles gained emoji -
    # so every existing leave handler keeps matching.

    from whatsapp_hr_bot import onboarding

    employee = get_employee(doc.get("from"))
    menu_options = get_whatsapp_menu_options(employee, onboarding)

    if not menu_options:
        send_text(
            doc,
            "No WhatsApp options are enabled for your employee profile. "
            "Please contact HR."
        )
        return

    send_interactive(
        doc,
        "Hello 👋\n\n"
        "How can I help you?\n\n"
        "Please select an option:",
        menu_options
    )


def get_whatsapp_menu_options(employee, onboarding):

    options = [
        {
            "id": "apply_leave",
            "title": "📝 Apply Leave",
            "description": "Submit a new leave request",
            "preference": "custom_whatsapp_apply_leave",
        },
        {
            "id": "leave_balance",
            "title": "📊 Leave Balance",
            "description": "See your available leave",
            "preference": "custom_whatsapp_leave_balance",
        },
        {
            "id": "my_requests",
            "title": "📋 My Leave Requests",
            "description": "Your recent leave applications",
            "preference": "custom_whatsapp_my_requests",
        },
        {
            "id": onboarding.BUTTON_MY_ONBOARDING,
            "title": "👤 My Onboarding",
            "description": "Checklist, progress and pending tasks",
            "preference": "custom_whatsapp_my_onboarding",
        },
        {
            "id": "apply_expense_claim",
            "title": "🧾 Apply Expense Claim",
            "description": "Submit a new expense claim",
            "preference": "custom_whatsapp_expense_claim",
        },
        {
            "id": "expense_claim_status",
            "title": "📄 Expense Claim Status",
            "description": "Check your expense claim status",
            "preference": "custom_whatsapp_expense_claim",
        },
    ]

    if not employee:
        return [
            {key: value for key, value in option.items() if key != "preference"}
            for option in options
        ]

    preferences = frappe.db.get_value(
        "Employee",
        employee,
        [option["preference"] for option in options],
        as_dict=True,
    ) or {}

    return [
        {key: value for key, value in option.items() if key != "preference"}
        for option in options
        if preferences.get(option["preference"], 1)
    ]


# ============================================================
# BUTTON HANDLER
# ============================================================

def handle_button(doc, phone, button_id):

    button_id = (
        button_id or ""
    ).strip()

    frappe.logger().info(
        f"WhatsApp HR Bot button received: {button_id}"
    )

    # ========================================================
    # ONBOARDING
    # ========================================================

    from whatsapp_hr_bot import onboarding

    if onboarding.is_onboarding_button_id(button_id):

        onboarding.handle_onboarding_message(
            doc,
            phone,
            button_id
        )

        return

    # ========================================================
    # EXPENSE CLAIM APPROVAL (Approve/Reject from the approver)
    # ========================================================

    from whatsapp_hr_bot import expense_approval

    if expense_approval.is_expense_approval_button_id(button_id):

        expense_approval.handle_expense_approval_button(
            doc,
            phone,
            button_id
        )

        return

    # ========================================================
    # APPLY LEAVE
    # ========================================================

    if button_id == "apply_leave":

        employee = get_employee(phone)

        if not employee:

            clear_state(phone)

            send_text(
                doc,
                "❌ I could not find your employee record in HRMS.\n\n"
                "Please contact HR."
            )

            return

        leave_types = frappe.get_all(
            "Leave Type",
            filters={
                "is_lwp": 0
            },
            pluck="name",
            order_by="name asc"
        )

        if not leave_types:

            clear_state(phone)

            send_text(
                doc,
                "❌ No leave types are currently available in HRMS."
            )

            return

        buttons = []

        for leave_type in leave_types[:3]:

            buttons.append(
                {
                    "id": f"leave_type:{leave_type}",
                    "title": leave_type[:20]
                }
            )

        set_state(
            phone,
            {
                # Tags this session as the leave flow so it is never
                # confused with an onboarding session (handle_whatsapp_
                # message routes on "flow"). Leave steps are unchanged.
                "flow": "leave",
                "employee": employee,
                "step": "leave_type"
            }
        )

        send_interactive(
            doc,
            "Please select your leave type:",
            buttons
        )

        return

    # ========================================================
    # DYNAMIC LEAVE TYPE
    # ========================================================

    if button_id.startswith("leave_type:"):

        handle_leave_flow(
            doc,
            phone,
            button_id
        )

        return

    # ========================================================
    # CONFIRM
    # ========================================================

    if button_id == "confirm_leave":

        process_confirmation(
            doc,
            phone,
            "confirm_leave"
        )

        return

    # ========================================================
    # CANCEL
    # ========================================================

    if button_id == "cancel_leave":

        process_confirmation(
            doc,
            phone,
            "cancel_leave"
        )

        return

    # ========================================================
    # APPLY EXPENSE CLAIM
    # ========================================================

    if button_id == "apply_expense_claim":

        employee = get_employee(phone)

        if not employee:

            clear_state(phone)

            send_text(
                doc,
                "❌ I could not find your employee record in HRMS.\n\n"
                "Please contact HR."
            )

            return

        expense_types = frappe.get_all(
            "Expense Claim Type",
            pluck="name",
            order_by="name asc"
        )

        if not expense_types:

            clear_state(phone)

            send_text(
                doc,
                "❌ No expense claim types are currently available in HRMS."
            )

            return

        buttons = []

        for expense_type in expense_types[:3]:

            buttons.append(
                {
                    "id": f"expense_type:{expense_type}",
                    "title": expense_type[:20]
                }
            )

        set_state(
            phone,
            {
                # Tags this session as the expense claim flow, kept
                # separate from "leave" the same way onboarding is -
                # see handle_whatsapp_message's routing on "flow".
                "flow": "expense_claim",
                "employee": employee,
                # The currency every amount on the claim ends up stored
                # in, whatever the employee enters it in - see
                # expense_currency.py.
                "company_currency": get_session_company_currency(employee),
                "step": "expense_type"
            }
        )

        send_interactive(
            doc,
            "Please select the expense type:",
            buttons
        )

        return

    # ========================================================
    # DYNAMIC EXPENSE CURRENCY
    #
    # The currency step is offered as an interactive list (more than
    # three options), whose reply frappe_whatsapp delivers exactly like
    # a button reply - so it lands here and is handed straight to the
    # flow, same as the expense type below.
    # ========================================================

    from whatsapp_hr_bot import expense_currency

    if button_id.startswith(expense_currency.CURRENCY_BUTTON_PREFIX):

        handle_expense_claim_flow(
            doc,
            phone,
            button_id
        )

        return

    # ========================================================
    # DYNAMIC EXPENSE TYPE
    # ========================================================

    if button_id.startswith("expense_type:"):

        handle_expense_claim_flow(
            doc,
            phone,
            button_id
        )

        return

    # ========================================================
    # CONFIRM EXPENSE CLAIM
    # ========================================================

    if button_id == "confirm_expense_claim":

        process_expense_claim_confirmation(
            doc,
            phone,
            "confirm_expense_claim"
        )

        return

    # ========================================================
    # CANCEL EXPENSE CLAIM
    # ========================================================

    if button_id == "cancel_expense_claim":

        process_expense_claim_confirmation(
            doc,
            phone,
            "cancel_expense_claim"
        )

        return

    # ========================================================
    # LEAVE BALANCE
    # ========================================================

    if button_id == "leave_balance":

        employee = get_employee(phone)

        if not employee:

            send_text(
                doc,
                "❌ I could not find your employee record in HRMS."
            )

            return

        message = build_leave_balance_message(employee)

        send_text(
            doc,
            message
        )

        return

    # ========================================================
    # MY REQUESTS
    # ========================================================

    if button_id == "my_requests":

        employee = get_employee(phone)

        if not employee:

            send_text(
                doc,
                "❌ I could not find your employee record in HRMS."
            )

            return

        requests = frappe.get_all(
            "Leave Application",
            filters={
                "employee": employee
            },
            fields=[
                "name",
                "leave_type",
                "from_date",
                "to_date",
                "status"
            ],
            order_by="creation desc",
            limit_page_length=5
        )

        if not requests:

            send_text(
                doc,
                "You don't have any leave requests yet."
            )

            return

        message = (
            "📋 Your Recent Leave Requests\n\n"
        )

        for request in requests:

            message += (
                f"{request.leave_type}\n"
                f"{request.from_date} → {request.to_date}\n"
                f"Status: {request.status}\n\n"
            )

        send_text(
            doc,
            message
        )

        return

    # ========================================================
    # EXPENSE CLAIM STATUS
    # ========================================================

    if button_id == "expense_claim_status":

        employee = get_employee(phone)

        if not employee:

            send_text(
                doc,
                "❌ I could not find your employee record in HRMS."
            )

            return

        message = build_expense_claim_status_message(employee)

        send_text(
            doc,
            message
        )

        return

    # ========================================================
    # UNKNOWN BUTTON
    # ========================================================

    send_text(
        doc,
        "I couldn't understand that option.\n\n"
        "Please type *Hii* to open the main menu."
    )


# ============================================================
# LEAVE BALANCE
# ============================================================

def build_leave_balance_message(employee):
    """Build the '📊 Your Leave Balance' summary for an employee.

    Every number comes from ERPNext / HRMS itself via
    ``get_leave_details`` - the exact same source the Leave
    Application form uses for its allocation dashboard
    (Allocated / Taken / Pending Approval / Available).
    Nothing is hard-coded.
    """

    on_date = getdate(today())

    # --------------------------------------------------------
    # ERPNext's leave APIs enforce ``validate_leave_access``.
    # The WhatsApp webhook runs as the *Guest* user, so the call
    # would raise PermissionError (previously swallowed -> 0).
    # Elevate to a permitted user for this read-only lookup and
    # restore the original user afterwards.
    # --------------------------------------------------------

    original_user = frappe.session.user

    try:

        frappe.set_user("Administrator")

        details = get_leave_details(employee, on_date) or {}

    except Exception:

        frappe.log_error(
            frappe.get_traceback(),
            "WhatsApp HR Bot - Leave Balance Error",
        )

        details = {}

    finally:

        frappe.set_user(original_user)

    allocation = details.get("leave_allocation") or {}

    if not allocation:

        return (
            "No leave allocation was found for your employee record."
        )

    lines = []

    for leave_type in sorted(allocation.keys()):

        row = allocation.get(leave_type) or {}

        total = flt(row.get("total_leaves"))
        taken = flt(row.get("leaves_taken"))
        pending = flt(row.get("leaves_pending_approval"))
        expired = flt(row.get("expired_leaves"))
        remaining = flt(row.get("remaining_leaves"))

        # Available = what the employee can still apply for.
        # Approved leaves already reduce ERPNext's `remaining`;
        # `pending` (applied but not yet approved) is subtracted
        # here so a just-submitted request is reflected at once.
        available = remaining - pending

        _log_leave_balance_debug(
            employee,
            leave_type,
            total,
            taken,
            pending,
            expired,
            remaining,
            available,
        )

        line = f"{leave_type}: {available:g} days"

        breakdown = [f"allocated {total:g}"]

        if taken:
            breakdown.append(f"approved {taken:g}")

        if pending:
            breakdown.append(f"pending {pending:g}")

        if expired:
            breakdown.append(f"expired {expired:g}")

        if len(breakdown) > 1:
            line += "\n   (" + ", ".join(breakdown) + ")"

        lines.append(line)

    return "📊 Your Leave Balance\n\n" + "\n".join(lines)


def _log_leave_balance_debug(
    employee,
    leave_type,
    total,
    taken,
    pending,
    expired,
    remaining,
    available,
):
    """TEMPORARY server-side debug logging for leave balance.

    Writes to sites/<site>/logs/whatsapp_hr_bot.log only - these
    details are never sent to the WhatsApp user. Remove once the
    balance feature is verified in production.
    """

    # Frappe's default log level is WARNING, so force INFO.
    debug_logger = frappe.logger("whatsapp_hr_bot")
    debug_logger.setLevel("INFO")

    leave_applications = frappe.get_all(
        "Leave Application",
        filters={
            "employee": employee,
            "leave_type": leave_type,
        },
        fields=[
            "name",
            "status",
            "docstatus",
            "from_date",
            "to_date",
            "total_leave_days",
        ],
        order_by="from_date asc",
    )

    debug_logger.info(
        "LEAVE BALANCE DEBUG | "
        f"Employee={employee} | "
        f"Leave Type={leave_type} | "
        f"Allocation Found=True | "
        f"Total Leaves Allocated={total:g} | "
        f"Leave Applications Found={len(leave_applications)} "
        f"{[(a['name'], a['status'], a['total_leave_days']) for a in leave_applications]} | "
        f"Used Leave Days (approved={taken:g}, pending={pending:g}, expired={expired:g}) | "
        f"Remaining per ERPNext={remaining:g} | "
        f"Final Calculated Balance={available:g}"
    )


# ============================================================
# EXPENSE CLAIM STATUS
# ============================================================

def build_expense_claim_status_message(employee):
    """Build the '🧾 Your Expense Claims' status summary for an employee.

    Read-only lookup, independent of the outbound "Send WhatsApp"
    notification system (notify/config.py + engine.py) - this only
    reports whatever approval_status already sits on the Expense Claim,
    it never sends or triggers anything on its own.
    """

    claims = frappe.get_all(
        "Expense Claim",
        filters={
            "employee": employee
        },
        fields=[
            "name",
            "approval_status",
            "expense_approver",
            "total_claimed_amount",
            "posting_date",
            "company",
            "custom_expense_currency",
        ],
        order_by="creation desc",
        limit_page_length=5
    )

    if not claims:

        return "You don't have any expense claims yet."

    message = "🧾 Your Recent Expense Claims\n\n"

    status_icons = {
        "Draft": "🕓",
        "Approved": "✅",
        "Rejected": "❌",
    }

    from whatsapp_hr_bot import expense_currency

    for claim in claims:

        icon = status_icons.get(claim.approval_status, "•")

        approver_name = None

        if claim.expense_approver:

            approver_name = frappe.db.get_value(
                "User",
                claim.expense_approver,
                "full_name"
            )

        amount = expense_currency.format_money(
            claim.total_claimed_amount,
            expense_currency.get_claim_currency(claim)
        )

        message += (
            f"{claim.name}\n"
            f"Amount: {amount}\n"
            f"Date: {claim.posting_date}\n"
            f"Status: {icon} {claim.approval_status}\n"
            f"Approver: {approver_name or claim.expense_approver or '-'}\n\n"
        )

    return message.strip()


# ============================================================
# LEAVE STATUS NOTIFICATION (Leave Application form button)
# ============================================================

@frappe.whitelist()
def notify_leave_status(leave_application):
    """Send the employee a WhatsApp message with the approval /
    rejection outcome of a Leave Application, followed by their
    updated leave balance.

    Called from the 'Notify Employee on WhatsApp' button on the
    Leave Application form (public/js/leave_application.js).
    """

    la = frappe.get_doc("Leave Application", leave_application)

    if la.docstatus != 1 or la.status not in ("Approved", "Rejected"):

        frappe.throw(
            _(
                "Notify the employee only after the Leave Application "
                "is approved or rejected."
            )
        )

    number = _get_employee_whatsapp_number(la.employee)

    if not number:

        frappe.throw(
            _(
                "No mobile number found on Employee {0}. "
                "Add a Mobile Number and try again."
            ).format(la.employee)
        )

    if la.status == "Approved":

        heading = "✅ Your leave request has been *approved*."

    else:

        heading = "❌ Your leave request has been *rejected*."

    # get the updated balance in an authorised context
    balance_message = build_leave_balance_message(la.employee)

    message = (
        f"{heading}\n\n"
        f"Request: {la.name}\n"
        f"Leave Type: {la.leave_type}\n"
        f"From: {la.from_date}\n"
        f"To: {la.to_date}\n"
        f"Days: {flt(la.total_leave_days):g}\n\n"
        f"{balance_message}"
    )

    frappe.get_doc(
        {
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": number,
            "message": message,
            "content_type": "text",
            "reference_doctype": "Leave Application",
            "reference_name": la.name,
        }
    ).insert(ignore_permissions=True)

    return _("WhatsApp notification sent to {0}.").format(number)


def _get_employee_whatsapp_number(employee):
    """Resolve a WhatsApp-capable number for an employee:
    Employee.cell_number first, then the linked User's mobile/phone.
    """

    cell = frappe.db.get_value("Employee", employee, "cell_number")

    if cell:
        return str(cell).strip()

    user_id = frappe.db.get_value("Employee", employee, "user_id")

    if user_id:

        for field in ("mobile_no", "phone"):

            value = frappe.db.get_value("User", user_id, field)

            if value:
                return str(value).strip()

    return None


# ============================================================
# LEAVE FLOW
# ============================================================

def handle_leave_flow(doc, phone, message):

    state = get_state(phone)

    if not state:

        send_text(
            doc,
            "Your leave session has expired.\n\n"
            "Please type *Hii* to start again."
        )

        return

    step = state.get("step")

    message = (
        message or ""
    ).strip()

    # ========================================================
    # LEAVE TYPE
    # ========================================================

    if step == "leave_type":

        if message.startswith("leave_type:"):

            leave_type = message.split(
                "leave_type:",
                1
            )[1].strip()

        else:

            leave_type = message

            if "\n" in leave_type:

                lines = [
                    line.strip()
                    for line in leave_type.splitlines()
                    if line.strip()
                ]

                if lines:

                    leave_type = lines[-1]

        exists = frappe.db.exists(
            "Leave Type",
            leave_type
        )

        if not exists:

            send_text(
                doc,
                "❌ Invalid leave type.\n\n"
                "Please select one of the leave types shown above."
            )

            return

        state["leave_type"] = leave_type
        state["step"] = "from_date"

        set_state(
            phone,
            state
        )

        send_text(
            doc,
            f"Leave type selected: {leave_type}\n\n"
            "Please enter the start date.\n\n"
            "Example: 2026-09-10"
        )

        return

    # ========================================================
    # START DATE
    # ========================================================

    if step == "from_date":

        try:

            from_date = getdate(message)

        except Exception:

            send_text(
                doc,
                "❌ Invalid date format.\n\n"
                "Please use:\n"
                "YYYY-MM-DD\n\n"
                "Example: 2026-09-10"
            )

            return

        if from_date < getdate(today()):

            send_text(
                doc,
                "❌ You cannot apply leave for a past date.\n\n"
                "Please enter today or a future date."
            )

            return

        state["from_date"] = str(from_date)
        state["step"] = "to_date"

        set_state(
            phone,
            state
        )

        send_text(
            doc,
            "Please enter the end date.\n\n"
            "Example: 2026-09-11"
        )

        return

    # ========================================================
    # END DATE
    # ========================================================

    if step == "to_date":

        try:

            to_date = getdate(message)

        except Exception:

            send_text(
                doc,
                "❌ Invalid date format.\n\n"
                "Please use:\n"
                "YYYY-MM-DD\n\n"
                "Example: 2026-09-11"
            )

            return

        from_date = getdate(
            state.get("from_date")
        )

        if to_date < from_date:

            send_text(
                doc,
                "❌ End date cannot be before the start date.\n\n"
                "Please enter a valid end date."
            )

            return

        state["to_date"] = str(to_date)
        state["step"] = "reason"

        set_state(
            phone,
            state
        )

        send_text(
            doc,
            "Please enter the reason for your leave."
        )

        return

    # ========================================================
    # REASON
    # ========================================================

    if step == "reason":

        reason = message.strip()

        if not reason:

            send_text(
                doc,
                "❌ Please enter a reason for your leave."
            )

            return

        state["reason"] = reason
        state["step"] = "confirmation"

        set_state(
            phone,
            state
        )

        send_interactive(
            doc,
            build_confirmation_message(state),
            [
                {
                    "id": "confirm_leave",
                    "title": "Confirm"
                },
                {
                    "id": "cancel_leave",
                    "title": "Cancel"
                }
            ]
        )

        return

    # ========================================================
    # CONFIRMATION
    # ========================================================

    if step == "confirmation":

        normalized = normalize_text(message)

        if normalized in [
            "confirm",
            "yes",
            "submit"
        ]:

            process_confirmation(
                doc,
                phone,
                "confirm_leave"
            )

            return

        if normalized in [
            "cancel",
            "no"
        ]:

            process_confirmation(
                doc,
                phone,
                "cancel_leave"
            )

            return

        send_text(
            doc,
            "Please select *Confirm* or *Cancel*."
        )

        return

    # ========================================================
    # UNKNOWN STATE
    # ========================================================

    clear_state(phone)

    send_text(
        doc,
        "Your leave session was reset.\n\n"
        "Please type *Hii* to start again."
    )


# ============================================================
# CONFIRMATION MESSAGE
# ============================================================

def build_confirmation_message(state):

    return (
        "Please confirm your leave request:\n\n"
        f"Leave Type: {state.get('leave_type')}\n"
        f"From: {state.get('from_date')}\n"
        f"To: {state.get('to_date')}\n"
        f"Reason: {state.get('reason')}\n\n"
        "Do you want to submit this request?"
    )


# ============================================================
# PROCESS CONFIRMATION
# ============================================================

def process_confirmation(doc, phone, button_id):

    state = get_state(phone)

    if not state:

        send_text(
            doc,
            "Your leave session has expired.\n\n"
            "Please type *Hii* to start again."
        )

        return

    # ========================================================
    # CANCEL
    # ========================================================

    if button_id == "cancel_leave":

        clear_state(phone)

        send_text(
            doc,
            "❌ Leave request cancelled."
        )

        send_main_menu(doc)

        return

    # ========================================================
    # CONFIRM
    # ========================================================

    if button_id != "confirm_leave":
        return

    # --------------------------------------------------------
    # ERPNext's leave APIs (get_leave_balance_on, and the Leave
    # Application controller's own validate()) enforce
    # ``validate_leave_access``, which rejects the *Guest* user
    # that the WhatsApp webhook runs as. Elevate to a permitted
    # user for the whole create flow and restore afterwards.
    # --------------------------------------------------------

    original_user = frappe.session.user

    try:

        frappe.set_user("Administrator")

        employee = state.get("employee")

        if not employee:

            raise Exception(
                "Employee missing from WhatsApp session state."
            )

        employee_doc = frappe.get_doc(
            "Employee",
            employee
        )

        employee_name = employee_doc.employee_name

        leave_type = state.get("leave_type")

        from_date = getdate(
            state.get("from_date")
        )

        to_date = getdate(
            state.get("to_date")
        )

        reason = state.get("reason")

        # ====================================================
        # CALCULATE LEAVE DAYS USING ERPNext
        # ====================================================

        total_days = get_number_of_leave_days(
            employee,
            leave_type,
            from_date,
            to_date,
            0,
            None,
        )

        if total_days <= 0:

            send_text(
                doc,
                "❌ The selected dates do not contain any valid "
                "leave days.\n\n"
                "Please choose different dates."
            )

            return

        # ====================================================
        # GET ERPNext LEAVE BALANCE
        # ====================================================

        leave_balance_data = get_leave_balance_on(
            employee,
            leave_type,
            from_date,
            to_date,
            consider_all_leaves_in_the_allocation_period=True,
            for_consumption=True,
        )

        available_days = (
            leave_balance_data.get(
                "leave_balance_for_consumption"
            )
            or 0
        )

        available_days = float(available_days)

        # ====================================================
        # LOG BALANCE INFORMATION
        # ====================================================

        frappe.logger().info(
            "WhatsApp HR Bot Leave Balance | "
            f"Employee={employee} | "
            f"Leave Type={leave_type} | "
            f"From={from_date} | "
            f"To={to_date} | "
            f"Requested={total_days} | "
            f"Available={available_days} | "
            f"Balance Data={leave_balance_data}"
        )

        # ====================================================
        # CHECK AVAILABLE BALANCE
        # ====================================================

        allow_negative = frappe.db.get_value(
            "Leave Type",
            leave_type,
            "allow_negative"
        )

        if (
            not allow_negative
            and
            available_days < total_days
        ):

            send_text(
                doc,
                "❌ Insufficient leave balance.\n\n"
                f"Requested: {total_days} day(s)\n"
                f"Available: {available_days:g} day(s)"
            )

            return

        # ====================================================
        # CREATE LEAVE APPLICATION
        # ====================================================

        leave_application = frappe.get_doc(
            {
                "doctype": "Leave Application",
                "employee": employee,
                "employee_name": employee_name,
                "leave_type": leave_type,
                "company": employee_doc.company,
                "department": employee_doc.department,
                "from_date": str(from_date),
                "to_date": str(to_date),
                "description": reason
            }
        )

        # ----------------------------------------------------
        # Insert only.
        #
        # ERPNext will perform its own validation.
        # ----------------------------------------------------

        leave_application.insert(
            ignore_permissions=True
        )

        frappe.db.commit()

        # ====================================================
        # SAVE REQUEST NAME
        # ====================================================

        request_name = leave_application.name

        # ====================================================
        # CLEAR SESSION
        # ====================================================

        clear_state(phone)

        # ====================================================
        # SUCCESS MESSAGE
        # ====================================================

        send_text(
            doc,
            "✅ Leave request submitted successfully!\n\n"
            f"Request: {request_name}\n"
            f"Leave Type: {leave_type}\n"
            f"From: {from_date}\n"
            f"To: {to_date}\n"
            f"Days: {total_days}\n\n"
            "Your request has been created in HRMS."
        )

        # ====================================================
        # RETURN TO MAIN MENU
        # ====================================================

        send_main_menu(doc)

    except frappe.ValidationError as exc:

        # ERPNext rejected the request (e.g. max continuous days,
        # overlap, insufficient balance). Show its actual reason
        # instead of a generic failure so the user can fix it.

        frappe.log_error(
            frappe.get_traceback(),
            "WhatsApp HR Bot - Leave Application Error"
        )

        reason = (
            frappe.utils.strip_html(str(exc)).strip()
            or "Your request could not be validated by HRMS."
        )

        send_text(
            doc,
            "❌ Your leave request could not be created.\n\n"
            f"{reason}\n\n"
            "Please adjust the details and try again, "
            "or type *Hii* to restart."
        )

    except Exception:

        frappe.log_error(
            frappe.get_traceback(),
            "WhatsApp HR Bot - Leave Application Error"
        )

        send_text(
            doc,
            "❌ There was a problem creating your leave request.\n\n"
            "Your information has not been cleared.\n"
            "Please try confirming again or type *Hii* to restart."
        )

    finally:

        frappe.set_user(original_user)


# ============================================================
# EXPENSE CLAIM FLOW
# ============================================================

def handle_expense_claim_flow(doc, phone, message):

    state = get_state(phone)

    if not state:

        send_text(
            doc,
            "Your expense claim session has expired.\n\n"
            "Please type *Hii* to start again."
        )

        return

    step = state.get("step")

    message = (
        message or ""
    ).strip()

    # ========================================================
    # EXPENSE TYPE
    # ========================================================

    if step == "expense_type":

        if message.startswith("expense_type:"):

            expense_type = message.split(
                "expense_type:",
                1
            )[1].strip()

        else:

            expense_type = message

            if "\n" in expense_type:

                lines = [
                    line.strip()
                    for line in expense_type.splitlines()
                    if line.strip()
                ]

                if lines:

                    expense_type = lines[-1]

        exists = frappe.db.exists(
            "Expense Claim Type",
            expense_type
        )

        if not exists:

            send_text(
                doc,
                "❌ Invalid expense type.\n\n"
                "Please select one of the expense types shown above."
            )

            return

        state["expense_type"] = expense_type
        state["step"] = "expense_date"

        set_state(
            phone,
            state
        )

        send_text(
            doc,
            f"Expense type selected: {expense_type}\n\n"
            "Please enter the expense date.\n\n"
            "Example: 2026-09-10"
        )

        return

    # ========================================================
    # EXPENSE DATE
    # ========================================================

    if step == "expense_date":

        try:

            expense_date = getdate(message)

        except Exception:

            send_text(
                doc,
                "❌ Invalid date format.\n\n"
                "Please use:\n"
                "YYYY-MM-DD\n\n"
                "Example: 2026-09-10"
            )

            return

        if expense_date > getdate(today()):

            send_text(
                doc,
                "❌ Expense date cannot be in the future.\n\n"
                "Please enter today or a past date."
            )

            return

        state["expense_date"] = str(expense_date)
        state["step"] = "description"

        set_state(
            phone,
            state
        )

        send_text(
            doc,
            "Please enter a description for this expense."
        )

        return

    # ========================================================
    # DESCRIPTION
    # ========================================================

    if step == "description":

        description = message.strip()

        if not description:

            send_text(
                doc,
                "❌ Please enter a description for this expense."
            )

            return

        state["description"] = description
        state["step"] = "currency"

        set_state(
            phone,
            state
        )

        send_expense_currency_prompt(
            doc,
            phone,
            state
        )

        return

    # ========================================================
    # CURRENCY
    #
    # Offered as an interactive list of the currencies enabled on the
    # site, company currency first, but any Currency the site has is
    # accepted when its code is typed - see expense_currency.py.
    # ========================================================

    if step == "currency":

        from whatsapp_hr_bot import expense_currency

        # Mutates the session rather than returning something used here:
        # the amount step and the currency list both read
        # state["company_currency"], and a session opened before that key
        # existed has to pick it up somewhere. This step is the first
        # place that writes the session back, so it is the cheapest one.
        resolve_session_company_currency(state)

        currency, candidates = expense_currency.resolve_currency(message)

        if not currency:

            if candidates:

                send_text(
                    doc,
                    build_ambiguous_currency_text(candidates)
                )

                return

            send_expense_currency_prompt(
                doc,
                phone,
                state,
                prefix="❌ I don't recognise that currency.\n\n"
            )

            return

        state["currency"] = currency
        state["step"] = "amount"

        set_state(
            phone,
            state
        )

        send_text(
            doc,
            f"Currency selected: {currency}\n\n"
            f"Please enter the amount in {currency}.\n\n"
            "Example: 1500"
        )

        return

    # ========================================================
    # AMOUNT
    #
    # Parsed together with an optional currency, so "1500",
    # "USD 100" and "₹1500" all work and a currency named here
    # overrides the one picked at the step above.
    #
    # Stored exactly as entered - the claim is raised in the
    # employee's currency and nothing is converted, see
    # expense_currency.py.
    # ========================================================

    if step == "amount":

        from whatsapp_hr_bot import expense_currency

        company_currency = resolve_session_company_currency(state)
        selected_currency = state.get("currency") or company_currency

        currency, amount, candidates = expense_currency.parse_amount(
            message,
            selected_currency
        )

        if candidates:

            send_text(
                doc,
                build_ambiguous_currency_text(candidates)
            )

            return

        if not currency or not amount or amount <= 0:

            send_text(
                doc,
                "❌ Please enter a valid amount greater than 0.\n\n"
                "Example: 1500\n"
                f"Example: {selected_currency} 1500"
            )

            return

        state["currency"] = currency
        state["amount"] = amount
        state["step"] = "remark"

        set_state(
            phone,
            state
        )

        send_text(
            doc,
            "Please enter a remark for this expense claim."
        )

        return

    # ========================================================
    # REMARK
    # ========================================================

    if step == "remark":

        remark = message.strip()

        if not remark:

            send_text(
                doc,
                "❌ Please enter a remark for this expense claim."
            )

            return

        state["remark"] = remark
        state["step"] = BILL_STEP

        set_state(
            phone,
            state
        )

        send_text(
            doc,
            "Would you like to attach a bill/receipt for this expense?\n\n"
            "Please send the file, or reply *No* to continue without an "
            "attachment."
        )

        return

    # ========================================================
    # BILL / RECEIPT (OPTIONAL)
    #
    # The file itself never arrives here: media is delivered as a
    # File attached to the inbound WhatsApp Message a moment later
    # in the same webhook request, and is picked up by
    # handle_inbound_whatsapp_file. This step only handles what the
    # employee *types* - "No" to skip, anything else a re-prompt.
    # ========================================================

    if step == BILL_STEP:

        if (doc.get("content_type") or "").strip().lower() in MEDIA_CONTENT_TYPES:

            # A caption sent alongside the file - ignored, so the
            # employee gets one reply (the file's) instead of two.

            return

        normalized = normalize_text(message)

        if normalized in SKIP_BILL_WORDS:

            state["bill_file"] = None
            state["bill_file_name"] = None

            send_expense_claim_confirmation(
                doc,
                phone,
                state
            )

            return

        send_text(
            doc,
            "Please send the bill/receipt as a PDF, JPG, JPEG or PNG "
            "file, or reply *No* to continue without an attachment."
        )

        return

    # ========================================================
    # CONFIRMATION
    # ========================================================

    if step == "confirmation":

        normalized = normalize_text(message)

        if normalized in [
            "confirm",
            "yes",
            "submit"
        ]:

            process_expense_claim_confirmation(
                doc,
                phone,
                "confirm_expense_claim"
            )

            return

        if normalized in [
            "cancel",
            "no"
        ]:

            process_expense_claim_confirmation(
                doc,
                phone,
                "cancel_expense_claim"
            )

            return

        send_text(
            doc,
            "Please select *Confirm* or *Cancel*."
        )

        return

    # ========================================================
    # UNKNOWN STATE
    # ========================================================

    clear_state(phone)

    send_text(
        doc,
        "Your expense claim session was reset.\n\n"
        "Please type *Hii* to start again."
    )


# ============================================================
# INBOUND FILE (BILL / RECEIPT)
# ============================================================

def handle_inbound_whatsapp_file(file_doc, method=None):
    """``File`` after_insert - take an inbound WhatsApp file as the
    bill/receipt for the expense claim its sender is in the middle of.

    frappe_whatsapp inserts the inbound WhatsApp Message first and only
    then downloads the media into a File attached to it (its
    utils/webhook.py), so the file does not exist yet when
    handle_whatsapp_message runs on that message's after_insert - this
    hook is the first point where it does. Every other File created on
    the site returns on the first check.
    """

    if file_doc.get("attached_to_doctype") != "WhatsApp Message":
        return

    if not file_doc.get("attached_to_name"):
        return

    try:

        handle_expense_bill_file(file_doc)

    except Exception:

        # An inbound file must never break the conversation or the
        # WhatsApp Message / File records it arrived on.

        frappe.log_error(
            frappe.get_traceback(),
            "WhatsApp HR Bot - Expense Bill Attachment Error"
        )


def handle_expense_bill_file(file_doc):

    message = frappe.get_doc(
        "WhatsApp Message",
        file_doc.attached_to_name
    )

    if message.get("type") != "Incoming":
        return

    phone = str(
        message.get("from") or ""
    ).strip()

    if not phone:
        return

    state = get_state(phone)

    # Only the expense claim flow's bill step takes files. Anywhere
    # else - no session, the leave or onboarding flows, an earlier or
    # later expense step - an inbound file is ignored, as before.

    if not state:
        return

    if state.get("flow") != "expense_claim":
        return

    if state.get("step") != BILL_STEP:
        return

    from whatsapp_hr_bot import expense_attachment

    file_name = (
        file_doc.get("file_name")
        or file_doc.get("file_url")
        or ""
    )

    if not expense_attachment.is_allowed_bill_file(file_name):

        send_text(
            message,
            "❌ Unsupported file type.\n\n"
            "Please send the bill/receipt as a PDF, JPG, JPEG or PNG "
            "file, or reply *No* to continue without an attachment."
        )

        return

    # Only remembered on the session here: the File that ends up on the
    # claim is created from this one when the claim is confirmed
    # (expense_attachment.stage_bill_file), so a claim that is never
    # confirmed leaves nothing behind.

    state["bill_file"] = file_doc.name
    state["bill_file_name"] = file_name

    send_expense_claim_confirmation(
        message,
        phone,
        state
    )


# ============================================================
# EXPENSE CLAIM ACCOUNTING DEFAULTS
# ============================================================

def get_expense_claim_accounting_defaults(employee_doc):
    """``(payable_account, cost_center)`` for a claim raised over WhatsApp.

    The desk form fills both in for the user - the parent's Cost Center
    through the field's own ``fetch_from`` on Company, the expense rows'
    through client script (``set_child_cost_center`` in hrms' own
    expense_claim.js), and the Payable Account through
    ``fetch_from: company.default_expense_claim_payable_account``. A
    server-side insert gets the two ``fetch_from`` ones for free and the
    client-script one not at all.

    That did not matter while every claim this app created had
    ``grand_total`` 0, because HRMS skips ``make_gl_entries`` entirely
    when nothing is sanctioned. Now that a claim carries its amount as
    sanctioned (see the expense row built below), approving it books real
    GL Entries - which need a Cost Center on each row and an account to
    credit - so both have to be set here, from the same defaults the desk
    form uses.

    Falls back to ERPNext's own party account resolution for the payable
    side when the company has no Default Expense Claim Payable Account:
    that is where an Employee payable goes for every other ERPNext
    document. Either value may still come back ``None`` on a company with
    nothing configured; the claim is created regardless (Draft needs
    neither) and ``expense_approval`` reports what is missing if the
    approver then tries to approve it.
    """

    import erpnext

    company = employee_doc.company

    payable_account = frappe.get_cached_value(
        "Company",
        company,
        "default_expense_claim_payable_account"
    )

    if not payable_account:

        try:

            from erpnext.accounts.party import get_party_account

            payable_account = get_party_account(
                "Employee",
                employee_doc.name,
                company
            )

        except Exception:

            # Nothing configured to fall back to - leave it empty rather
            # than guessing an account to post to.
            payable_account = None

    return payable_account, erpnext.get_default_cost_center(company)


# ============================================================
# EXPENSE CLAIM CURRENCY
# ============================================================

def get_session_company_currency(employee):
    """Currency the employee's company keeps its books in.

    Offered first at the currency step and used as the default when the
    employee's amount names no currency of its own. It is *not* what the
    claim is stored in - that is whatever they picked, unconverted (see
    expense_currency.py).
    """

    from whatsapp_hr_bot import expense_currency

    company = frappe.db.get_value(
        "Employee",
        employee,
        "company"
    )

    return expense_currency.get_company_currency(company)


def resolve_session_company_currency(state):
    """``state["company_currency"]``, filled in if it is missing.

    A session opened before this field existed carries no company
    currency, and expiring those mid-claim would lose the employee's
    answers - so it is resolved from the employee instead.
    """

    company_currency = state.get("company_currency")

    if not company_currency:

        company_currency = get_session_company_currency(
            state.get("employee")
        )

        state["company_currency"] = company_currency

    return company_currency


def send_expense_currency_prompt(doc, phone, state, prefix=""):

    from whatsapp_hr_bot import expense_currency

    company_currency = resolve_session_company_currency(state)

    set_state(
        phone,
        state
    )

    buttons = []

    for code in expense_currency.get_currency_options(company_currency):

        symbol = expense_currency.get_currency_symbol(code)

        description = symbol or code

        if code == company_currency:
            description = f"{description} - company currency"

        buttons.append(
            {
                "id": f"{expense_currency.CURRENCY_BUTTON_PREFIX}{code}",
                "title": code[:20],
                "description": description[:72]
            }
        )

    send_interactive(
        doc,
        prefix
        + "Please select the currency of this expense.\n\n"
        + "The claim is recorded in the currency you pick, exactly as you "
        "enter it.\n\n"
        "You can also type a currency code, for example CAD.",
        buttons
    )


def build_ambiguous_currency_text(candidates):
    """A symbol like "$" is shared by USD, CAD, AUD, SGD and two dozen
    more, so it can never be resolved on its own - ask for the code.
    """

    return (
        "❌ That symbol is used by more than one currency.\n\n"
        "Please reply with the currency code, for example: "
        + ", ".join(candidates[:5])
    )


# ============================================================
# EXPENSE CLAIM CONFIRMATION MESSAGE
# ============================================================

def send_expense_claim_confirmation(doc, phone, state):

    state["step"] = "confirmation"

    set_state(
        phone,
        state
    )

    send_interactive(
        doc,
        build_expense_claim_confirmation_message(state),
        [
            {
                "id": "confirm_expense_claim",
                "title": "Confirm"
            },
            {
                "id": "cancel_expense_claim",
                "title": "Cancel"
            }
        ]
    )


def build_expense_claim_confirmation_message(state):

    from whatsapp_hr_bot import expense_currency

    # frappe_whatsapp names inbound media after a random hash (its
    # utils/webhook.py), so the stored file name means nothing to the
    # employee - show the format they sent instead.

    bill_file_name = state.get("bill_file_name")

    if bill_file_name:

        bill_label = "Attached ({0})".format(
            bill_file_name.rsplit(".", 1)[-1].upper()
        )

    else:

        bill_label = "Not attached"

    currency = state.get("currency") or state.get("company_currency")

    amount = expense_currency.format_money(
        state.get("amount"),
        currency
    )

    return (
        "Please confirm your expense claim:\n\n"
        f"Expense Type: {state.get('expense_type')}\n"
        f"Date: {state.get('expense_date')}\n"
        f"Description: {state.get('description')}\n"
        f"Amount: {amount}\n"
        f"Remark: {state.get('remark')}\n"
        f"Bill/Receipt: {bill_label}\n\n"
        "Do you want to submit this claim?"
    )


# ============================================================
# PROCESS EXPENSE CLAIM CONFIRMATION
# ============================================================

def process_expense_claim_confirmation(doc, phone, button_id):

    state = get_state(phone)

    if not state:

        send_text(
            doc,
            "Your expense claim session has expired.\n\n"
            "Please type *Hii* to start again."
        )

        return

    # ========================================================
    # CANCEL
    # ========================================================

    if button_id == "cancel_expense_claim":

        clear_state(phone)

        send_text(
            doc,
            "❌ Expense claim cancelled."
        )

        send_main_menu(doc)

        return

    # ========================================================
    # CONFIRM
    # ========================================================

    if button_id != "confirm_expense_claim":
        return

    # --------------------------------------------------------
    # Same reasoning as process_confirmation: the WhatsApp webhook
    # runs as Guest, which cannot create documents. Elevate for the
    # create call only and restore the original user afterwards.
    # --------------------------------------------------------

    original_user = frappe.session.user

    try:

        frappe.set_user("Administrator")

        employee = state.get("employee")

        if not employee:

            raise Exception(
                "Employee missing from WhatsApp session state."
            )

        employee_doc = frappe.get_doc(
            "Employee",
            employee
        )

        employee_name = employee_doc.employee_name

        from whatsapp_hr_bot import expense_currency

        expense_type = state.get("expense_type")
        expense_date = getdate(state.get("expense_date"))
        description = state.get("description")
        remark = state.get("remark")

        # ----------------------------------------------------
        # CURRENCY
        #
        # The amount goes onto the claim exactly as the employee entered
        # it, in the currency they picked - nothing is converted.
        # custom_expense_currency is what makes ERPNext render it that
        # way: every Currency field on Expense Claim reads its currency
        # from that field (the Property Setters in
        # fixtures/property_setter.json), so Grand Total comes out in the
        # same currency and the same amount. See expense_currency.py.
        # ----------------------------------------------------

        company_currency = expense_currency.get_company_currency(
            employee_doc.company
        )

        currency = state.get("currency") or company_currency

        amount = flt(state.get("amount"))

        # ----------------------------------------------------
        # Optional bill/receipt from BILL_STEP, copied into a
        # private File now so the claim is inserted with its
        # proof already on it - see
        # expense_attachment.stage_bill_file. A file that has
        # gone missing, or whose type is not allowed, yields
        # None and the claim is created without an attachment
        # rather than failing.
        # ----------------------------------------------------

        bill_file_url = None

        if state.get("bill_file"):

            from whatsapp_hr_bot import expense_attachment

            bill_file_url = expense_attachment.stage_bill_file(
                state.get("bill_file")
            )

        # ====================================================
        # CREATE EXPENSE CLAIM
        #
        # employee / company / department / expense_approver are
        # all taken from the Employee record, never asked over
        # WhatsApp. approval_status is forced to "Draft" and the
        # document is only inserted, never submitted - the only
        # way this claim moves to Approved/Rejected or sends a
        # WhatsApp notification is the existing "Send WhatsApp"
        # button / HR's normal approval flow.
        # ====================================================

        payable_account, cost_center = get_expense_claim_accounting_defaults(
            employee_doc
        )

        expense_claim = frappe.get_doc(
            {
                "doctype": "Expense Claim",
                "employee": employee,
                "employee_name": employee_name,
                "company": employee_doc.company,
                "department": employee_doc.department,
                "expense_approver": employee_doc.expense_approver,
                "payable_account": payable_account,
                "posting_date": str(expense_date),
                "approval_status": "Draft",
                "remark": remark,
                "custom_bill_attachment": bill_file_url,
                "custom_expense_currency": currency,
                "expenses": [
                    {
                        "expense_date": str(expense_date),
                        "expense_type": expense_type,
                        "description": description,
                        "amount": amount,
                        # Same default the desk form applies the moment an
                        # amount is typed (hrms' expense_claim.js sets
                        # sanctioned_amount from amount). Inserting server
                        # side skips that, and without it HRMS'
                        # calculate_taxes - which computes grand_total as
                        # sanctioned + taxes - advances - leaves
                        # total_sanctioned_amount and grand_total at 0.
                        # The Expense Approver can still reduce it before
                        # approving, exactly as for a desk-created claim.
                        "sanctioned_amount": amount,
                        "cost_center": cost_center,
                    }
                ],
            }
        )

        # ----------------------------------------------------
        # Insert only - never submit. ERPNext performs its own
        # validation (missing expense approver, missing default
        # account for the expense type, etc.) and we surface
        # whatever it raises below.
        # ----------------------------------------------------

        expense_claim.insert(
            ignore_permissions=True
        )

        frappe.db.commit()

        # ====================================================
        # SAVE CLAIM NAME
        # ====================================================

        claim_name = expense_claim.name
        claimed_amount = flt(expense_claim.total_claimed_amount) or amount
        grand_total = flt(expense_claim.grand_total) or claimed_amount

        # ====================================================
        # CLEAR SESSION
        # ====================================================

        clear_state(phone)

        # ====================================================
        # SUCCESS MESSAGE
        # ====================================================

        send_text(
            doc,
            "✅ Expense claim submitted successfully!\n\n"
            f"Claim: {claim_name}\n"
            f"Expense Type: {expense_type}\n"
            f"Amount: {expense_currency.format_money(claimed_amount, currency)}\n"
            f"Grand Total: {expense_currency.format_money(grand_total, currency)}\n"
            f"Status: Draft\n\n"
            "Your claim has been created in HRMS and is awaiting "
            "approval."
        )

        # ====================================================
        # RETURN TO MAIN MENU
        # ====================================================

        send_main_menu(doc)

    except frappe.ValidationError as exc:

        # ERPNext rejected the claim (e.g. no expense approver
        # configured, no default account for the expense type).
        # Show its actual reason instead of a generic failure.

        frappe.log_error(
            frappe.get_traceback(),
            "WhatsApp HR Bot - Expense Claim Error"
        )

        reason = (
            frappe.utils.strip_html(str(exc)).strip()
            or "Your claim could not be validated by HRMS."
        )

        send_text(
            doc,
            "❌ Your expense claim could not be created.\n\n"
            f"{reason}\n\n"
            "Please adjust the details and try again, "
            "or type *Hii* to restart."
        )

    except Exception:

        frappe.log_error(
            frappe.get_traceback(),
            "WhatsApp HR Bot - Expense Claim Error"
        )

        send_text(
            doc,
            "❌ There was a problem creating your expense claim.\n\n"
            "Your information has not been cleared.\n"
            "Please try confirming again or type *Hii* to restart."
        )

    finally:

        frappe.set_user(original_user)


# ============================================================
# EMPLOYEE LOOKUP
# ============================================================

def get_employee(phone):

    normalized_phone = normalize_phone(phone)

    if not normalized_phone:
        return None

    employees = frappe.get_all(
        "Employee",
        filters={
            "status": "Active"
        },
        fields=[
            "name",
            "cell_number",
            "user_id"
        ]
    )

    for employee in employees:

        # ----------------------------------------------------
        # Employee cell number
        # ----------------------------------------------------

        if phone_matches(
            normalized_phone,
            employee.cell_number
        ):

            return employee.name

        # ----------------------------------------------------
        # Employee User mobile number
        # ----------------------------------------------------

        if employee.user_id:

            user_mobile = frappe.db.get_value(
                "User",
                employee.user_id,
                "mobile_no"
            )

            if phone_matches(
                normalized_phone,
                user_mobile
            ):

                return employee.name

    return None


# ============================================================
# PHONE MATCH
# ============================================================

def phone_matches(phone1, phone2):

    if not phone1 or not phone2:
        return False

    return (
        normalize_phone(phone1)
        ==
        normalize_phone(phone2)
    )


# ============================================================
# NORMALIZE PHONE
# ============================================================

def normalize_phone(phone):

    if not phone:
        return ""

    phone = str(phone)

    digits = "".join(
        character
        for character in phone
        if character.isdigit()
    )

    if len(digits) >= 10:
        return digits[-10:]

    return digits


# ============================================================
# NORMALIZE TEXT
# ============================================================

def normalize_text(text):

    if not text:
        return ""

    return " ".join(
        str(text)
        .lower()
        .strip()
        .split()
    )


# ============================================================
# STATE MANAGEMENT
# ============================================================

def set_state(phone, state):

    if not phone:
        return

    key = f"{STATE_PREFIX}{phone}"

    frappe.cache().set_value(
        key,
        state,
        expires_in_sec=SESSION_TIMEOUT
    )

    # frappe.cache().get_value() remembers a *miss* in frappe.local.cache,
    # and set_value() deliberately skips that process-local copy whenever
    # expires_in_sec is given (frappe/utils/redis_wrapper.py). So a
    # get_state() that found nothing earlier in the same request keeps
    # returning None afterwards, however many times this writes the
    # session - and the webhook runs in exactly that order whenever Meta
    # delivers more than one message in a single POST (frappe_whatsapp's
    # utils/webhook.py loops over them), or when a second doc event lands
    # in the same request. Keep the local copy in step with Redis so a
    # later read in the same request sees the session that was just
    # written. clear_state's delete_value already drops the local copy.

    frappe.local.cache[frappe.cache().make_key(key)] = state


def get_state(phone):

    if not phone:
        return None

    value = frappe.cache().get_value(
        f"{STATE_PREFIX}{phone}"
    )

    if not value:
        return None

    try:

        if isinstance(value, str):

            return frappe.parse_json(
                value
            )

        return value

    except Exception:

        clear_state(phone)

        return None


def clear_state(phone):

    if not phone:
        return

    frappe.cache().delete_value(
        f"{STATE_PREFIX}{phone}"
    )


# ============================================================
# SEND TEXT
# ============================================================

def send_text(doc, message):

    outgoing = frappe.get_doc(
        {
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": doc.get("from"),
            "message": message,
            "content_type": "text",
            "whatsapp_account": doc.get(
                "whatsapp_account"
            )
        }
    )

    outgoing.insert(
        ignore_permissions=True
    )


# ============================================================
# SEND INTERACTIVE BUTTONS
# ============================================================

def send_interactive(doc, message, buttons):
    """Send ``buttons`` as an interactive reply.

    ``buttons`` is the list every caller builds. It has to reach the
    WhatsApp Message as a JSON *string*, not the list itself: the
    doctype's before_insert (which is what actually calls Meta) copes with
    either, but frappe's own ``get_valid_dict`` then refuses to write a
    list into the field - "Value for Buttons cannot be a list" - and that
    happens *after* the message has gone out. The insert rolls back, so
    the message is delivered but never logged, and the exception aborts
    whatever the handler was in the middle of doing. Encoding it here is
    the same thing notify/engine.py's ``_build_send_fields`` does, for the
    same reason.
    """

    outgoing = frappe.get_doc(
        {
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": doc.get("from"),
            "message": message,
            "content_type": "interactive",
            "buttons": buttons if isinstance(buttons, str) else json.dumps(buttons),
            "whatsapp_account": doc.get(
                "whatsapp_account"
            )
        }
    )

    outgoing.insert(
        ignore_permissions=True
    )


# ============================================================
# SEND MEDIA (image / document)
# ============================================================

def send_media(doc, content_type, link, caption=None):
    """Send a file already published at ``link`` as an image/document.

    ``link`` has to be an absolute URL Meta's servers can fetch - the
    caller builds it (see expense_attachment.get_bill_delivery_url).
    Needed alongside send_interactive because WhatsApp's interactive
    messages carry no attachment, so a bill travels as its own message.
    """

    outgoing = frappe.get_doc(
        {
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": doc.get("from"),
            "message": caption,
            "content_type": content_type,
            "attach": link,
            "whatsapp_account": doc.get(
                "whatsapp_account"
            )
        }
    )

    outgoing.insert(
        ignore_permissions=True
    )