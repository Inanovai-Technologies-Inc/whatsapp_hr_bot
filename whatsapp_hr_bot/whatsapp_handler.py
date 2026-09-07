import json
import frappe

from frappe.utils import getdate, today

from hrms.hr.doctype.leave_application.leave_application import (
    get_leave_balance_on,
    get_number_of_leave_days,
)


# ============================================================
# CONSTANTS
# ============================================================

STATE_PREFIX = "whatsapp_hr_bot:"
PROCESSED_PREFIX = "whatsapp_hr_bot:processed:"

SESSION_TIMEOUT = 1800
PROCESSED_TIMEOUT = 3600


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
    # Existing conversation
    # --------------------------------------------------------

    state = get_state(phone)

    if state:

        handle_leave_flow(
            doc,
            phone,
            message
        )

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

    return False


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

    send_interactive(
        doc,
        "Hello 👋\n\n"
        "How can I help you?\n\n"
        "Please select an option:",
        [
            {
                "id": "apply_leave",
                "title": "Apply Leave"
            },
            {
                "id": "leave_balance",
                "title": "Leave Balance"
            },
            {
                "id": "my_requests",
                "title": "My Requests"
            }
        ]
    )


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

        # ----------------------------------------------------
        # Get submitted allocations
        # ----------------------------------------------------

        allocations = frappe.get_all(
            "Leave Allocation",
            filters={
                "employee": employee,
                "docstatus": 1
            },
            fields=[
                "name",
                "leave_type",
                "from_date",
                "to_date"
            ],
            order_by="leave_type asc"
        )

        if not allocations:

            send_text(
                doc,
                "No leave allocation was found for your employee record."
            )

            return

        message = "📊 Your Leave Balance\n\n"

        processed_leave_types = set()

        for allocation in allocations:

            leave_type = allocation.leave_type

            if leave_type in processed_leave_types:
                continue

            processed_leave_types.add(leave_type)

            try:

                balance_data = get_leave_balance_on(
                    employee,
                    leave_type,
                    getdate(today()),
                    consider_all_leaves_in_the_allocation_period=True,
                    for_consumption=True,
                )

                balance = (
                    balance_data.get(
                        "leave_balance_for_consumption"
                    )
                    or 0
                )

            except Exception:

                balance = 0

            message += (
                f"{leave_type}: {balance}\n"
            )

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

    try:

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