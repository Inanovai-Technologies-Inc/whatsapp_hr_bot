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

        if state.get("flow") == onboarding.ONBOARDING_FLOW:

            onboarding.handle_onboarding_message(
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
            # Confirmation
            # ------------------------------------------------

            normalized = normalize_text(message)

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
    ]:
        return True

    if value.startswith("leave_type:"):
        return True

    from whatsapp_hr_bot import onboarding

    if onboarding.is_onboarding_button_id(value):
        return True

    return False


# ============================================================
# LEAVE KEYWORDS
# ============================================================

def is_leave_keyword(text):
    """``text`` is expected to be normalised by ``normalize_text``."""

    return text in LEAVE_KEYWORDS


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

    frappe.cache().set_value(
        f"{STATE_PREFIX}{phone}",
        state,
        expires_in_sec=SESSION_TIMEOUT
    )


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

    outgoing = frappe.get_doc(
        {
            "doctype": "WhatsApp Message",
            "type": "Outgoing",
            "to": doc.get("from"),
            "message": message,
            "content_type": "interactive",
            "buttons": buttons,
            "whatsapp_account": doc.get(
                "whatsapp_account"
            )
        }
    )

    outgoing.insert(
        ignore_permissions=True
    )