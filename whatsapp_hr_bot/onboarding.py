"""Employee Onboarding self-service for the WhatsApp HR bot.

Source of truth is HRMS's own **Employee Onboarding** DocType (the form
titled "New Employee Onboarding" when you create one) and its
``activities`` child table (**Employee Boarding Activity**).

No new onboarding DocType and no new status system are introduced:

* HRMS creates one **Task** per activity on submit
  (``EmployeeBoardingController.create_task_and_notify_user``) and stores
  it on ``activity.task``. That Task's ``status`` is the existing status
  field for an onboarding activity, so it is what we report here - HR
  updating the Task in Desk is reflected on WhatsApp immediately.
* The Task also already carries the holiday-adjusted ``exp_start_date`` /
  ``exp_end_date`` that HRMS computed from the activity's ``begin_on`` /
  ``duration`` day offsets, so those are the dates we display.

Only when a Task does not exist yet (onboarding still in Draft - tasks
are created on submit) do we fall back to computing the dates from
``boarding_begins_on + begin_on`` and reporting the activity as Pending.
"""

import frappe

from frappe.utils import add_days, cint, formatdate, getdate, today


# ============================================================
# CONSTANTS
# ============================================================

ONBOARDING_FLOW = "onboarding"

STATUS_PENDING = "Pending"
STATUS_IN_PROGRESS = "In Progress"
STATUS_COMPLETED = "Completed"

STATUS_ICONS = {
    STATUS_PENDING: "⏳",
    STATUS_IN_PROGRESS: "\U0001f504",
    STATUS_COMPLETED: "✅",
}

# ERPNext Task statuses -> the three statuses this flow reports.
# "Cancelled" is grouped with "Completed" because HRMS itself treats it
# as closed (see EmployeeOnboarding.validate_employee_creation) - there
# is nothing left for the employee to do either way.

TASK_STATUS_MAP = {
    "Open": STATUS_PENDING,
    "Template": STATUS_PENDING,
    "Working": STATUS_IN_PROGRESS,
    "Pending Review": STATUS_IN_PROGRESS,
    "Overdue": STATUS_IN_PROGRESS,
    "Completed": STATUS_COMPLETED,
    "Cancelled": STATUS_COMPLETED,
}

# Text that opens the onboarding menu without going through "Hii" first.

ONBOARDING_KEYWORDS = [
    "onboarding",
    "onboarding status",
    "my onboarding",
    "onboard",
    "joining status",
]

# Interactive ids owned by this flow.

BUTTON_MY_ONBOARDING = "my_onboarding"
BUTTON_CHECKLIST = "onboarding_checklist"
BUTTON_PROGRESS = "onboarding_progress"
BUTTON_PENDING = "onboarding_pending"

ONBOARDING_BUTTON_IDS = [
    BUTTON_MY_ONBOARDING,
    BUTTON_CHECKLIST,
    BUTTON_PROGRESS,
    BUTTON_PENDING,
]

# WhatsApp rejects a body over 4096 characters.

MAX_BODY_LENGTH = 3800

NO_ONBOARDING_MESSAGE = (
    "Sorry, I couldn't find an active onboarding record for your "
    "employee profile. Please contact HR."
)

NO_EMPLOYEE_MESSAGE = (
    "❌ I could not find your employee record in HRMS.\n\n"
    "Please contact HR."
)

COMPLETION_MESSAGE = (
    "\U0001f389 Congratulations!\n\n"
    "Your onboarding checklist is complete.\n\n"
    "All onboarding activities have been completed."
)


# ============================================================
# KEYWORD / BUTTON RECOGNITION
# ============================================================

def is_onboarding_keyword(text):
    """``text`` is expected to be normalised by ``normalize_text``."""

    return text in ONBOARDING_KEYWORDS


def is_onboarding_button_id(button_id):

    return (button_id or "").strip() in ONBOARDING_BUTTON_IDS


# ============================================================
# EMPLOYEE LOOKUP
# ============================================================

def get_employee_from_whatsapp(phone):
    """Resolve the Employee for a WhatsApp number.

    Reuses the bot's existing Employee/contact mapping
    (``Employee.cell_number``, then the linked ``User.mobile_no``)
    rather than introducing a second one.
    """

    from whatsapp_hr_bot.whatsapp_handler import get_employee

    return get_employee(phone)


# ============================================================
# ONBOARDING RECORD LOOKUP
# ============================================================

def get_active_onboarding(employee):
    """Return the employee's active Employee Onboarding record, or None.

    "Active" means not cancelled (``docstatus < 2``). A record whose
    ``boarding_status`` is already "Completed" is only used when it is
    the sole candidate, so a finished onboarding still reports the
    completion message instead of "no record found".
    """

    if not employee:
        return None

    filter_sets = [
        {"employee": employee, "docstatus": ["<", 2]},
    ]

    # Employee Onboarding is usually raised from a Job Applicant before
    # the Employee record exists, so ``employee`` can still be empty.
    # Fall back to matching on the name in that case.

    employee_name = frappe.db.get_value(
        "Employee",
        employee,
        "employee_name"
    )

    if employee_name:

        filter_sets.append(
            {
                "employee": ["in", ["", None]],
                "employee_name": employee_name,
                "docstatus": ["<", 2],
            }
        )

    for filters in filter_sets:

        records = frappe.get_all(
            "Employee Onboarding",
            filters=filters,
            fields=[
                "name",
                "employee_name",
                "boarding_status",
                "boarding_begins_on",
                "date_of_joining",
                "designation",
                "department",
                "company",
                "docstatus",
            ],
            order_by="docstatus desc, modified desc",
        )

        if not records:
            continue

        for record in records:

            if record.get("boarding_status") != STATUS_COMPLETED:
                return record

        return records[0]

    return None


# ============================================================
# ACTIVITIES
# ============================================================

def get_onboarding_activities(onboarding):
    """Read the Activities child table and resolve each row's status and
    dates from the Task HRMS created for it.
    """

    if not onboarding:
        return []

    rows = frappe.get_all(
        "Employee Boarding Activity",
        filters={
            "parent": onboarding.get("name"),
            "parenttype": "Employee Onboarding",
        },
        fields=[
            "activity_name",
            "user",
            "begin_on",
            "duration",
            "task",
        ],
        order_by="idx asc",
    )

    boarding_begins_on = onboarding.get("boarding_begins_on")

    activities = []

    for row in rows:

        task = _get_task(row.get("task"))

        activity = {
            "activity_name": row.get("activity_name"),
            "user": row.get("user"),
            "begin_on": row.get("begin_on"),
            "duration": row.get("duration"),
            "task": row.get("task"),
            "status": _resolve_status(task),
            "begin_date": _resolve_begin_date(
                task,
                row,
                boarding_begins_on
            ),
            "target_date": _resolve_target_date(
                task,
                row,
                boarding_begins_on
            ),
        }

        activity["is_overdue"] = _is_overdue(activity)

        activities.append(activity)

    return activities


def _get_task(task_name):

    if not task_name:
        return None

    return frappe.db.get_value(
        "Task",
        task_name,
        [
            "name",
            "status",
            "exp_start_date",
            "exp_end_date",
        ],
        as_dict=True,
    )


def _resolve_status(task):
    """Tasks only exist once the onboarding is submitted; until then
    nothing has started, so the activity is Pending.
    """

    if not task:
        return STATUS_PENDING

    return TASK_STATUS_MAP.get(
        task.get("status"),
        STATUS_PENDING
    )


def _resolve_begin_date(task, row, boarding_begins_on):
    """Prefer the Task's ``exp_start_date`` - HRMS already shifted it off
    the holiday list. Otherwise recompute it the way HRMS does:
    ``boarding_begins_on + begin_on`` days.
    """

    if task and task.get("exp_start_date"):
        return getdate(task.get("exp_start_date"))

    if not boarding_begins_on or row.get("begin_on") is None:
        return None

    return getdate(
        add_days(
            boarding_begins_on,
            cint(row.get("begin_on"))
        )
    )


def _resolve_target_date(task, row, boarding_begins_on):
    """The date the activity is expected to be finished by:
    ``boarding_begins_on + begin_on + duration`` days.
    """

    if task and task.get("exp_end_date"):
        return getdate(task.get("exp_end_date"))

    if not boarding_begins_on or row.get("begin_on") is None:
        return None

    return getdate(
        add_days(
            boarding_begins_on,
            cint(row.get("begin_on")) + cint(row.get("duration")),
        )
    )


def _is_overdue(activity):

    if activity.get("status") == STATUS_COMPLETED:
        return False

    target_date = activity.get("target_date")

    if not target_date:
        return False

    return getdate(target_date) < getdate(today())


# ============================================================
# PROGRESS
# ============================================================

def get_onboarding_progress(activities):
    """Counts and percentage, derived entirely from the activities."""

    activities = activities or []

    total = len(activities)

    completed = 0
    in_progress = 0
    pending = 0

    for activity in activities:

        status = activity.get("status")

        if status == STATUS_COMPLETED:
            completed += 1

        elif status == STATUS_IN_PROGRESS:
            in_progress += 1

        else:
            pending += 1

    percent = 0

    if total:
        percent = int(round((completed * 100.0) / total))

    return {
        "total": total,
        "completed": completed,
        "in_progress": in_progress,
        "pending": pending,
        "percent": percent,
        "is_complete": bool(total) and completed == total,
    }


# ============================================================
# MESSAGE FORMATTING
# ============================================================

def format_onboarding_checklist(activities):

    if not activities:

        return (
            "\U0001f4cb Your Onboarding Checklist\n\n"
            "No onboarding activities have been added to your record "
            "yet.\n\n"
            "Please contact HR."
        )

    lines = ["\U0001f4cb Your Onboarding Checklist", ""]

    for index, activity in enumerate(activities, start=1):

        status = activity.get("status") or STATUS_PENDING

        icon = STATUS_ICONS.get(status, "⏳")

        lines.append(
            f"{index}. {activity.get('activity_name') or 'Activity'}"
        )

        begin_date = activity.get("begin_date")

        if begin_date:
            lines.append(f"   Begin: {_format_date(begin_date)}")

        duration = cint(activity.get("duration"))

        if duration:
            lines.append(f"   Duration: {duration} day(s)")

        lines.append(f"   Status: {icon} {status}")

        lines.append("")

    return _truncate("\n".join(lines).rstrip())


def format_onboarding_progress(progress):

    return (
        "\U0001f4ca Your Onboarding Progress\n\n"
        f"Completed: {progress.get('completed')}/{progress.get('total')}\n"
        f"Progress: {progress.get('percent')}%\n\n"
        f"✅ Completed: {progress.get('completed')}\n"
        f"\U0001f504 In Progress: {progress.get('in_progress')}\n"
        f"⏳ Pending: {progress.get('pending')}"
    )


def format_pending_tasks(activities):

    pending = [
        activity
        for activity in (activities or [])
        if activity.get("status") != STATUS_COMPLETED
    ]

    if not pending:
        return COMPLETION_MESSAGE

    lines = ["⏳ Pending Onboarding Tasks", ""]

    for index, activity in enumerate(pending, start=1):

        lines.append(
            f"{index}. {activity.get('activity_name') or 'Activity'}"
        )

        target_date = activity.get("target_date")

        if target_date:
            lines.append(f"   Target: {_format_date(target_date)}")

        if activity.get("is_overdue"):
            lines.append("   ⚠️ Overdue")

        lines.append("")

    return _truncate("\n".join(lines).rstrip())


def _format_date(value):

    if not value:
        return ""

    return formatdate(getdate(value), "d MMM yyyy")


def _truncate(message):

    if len(message) <= MAX_BODY_LENGTH:
        return message

    return message[:MAX_BODY_LENGTH].rstrip() + "\n\n… (list truncated)"


# ============================================================
# COMPLETION
# ============================================================

def mark_onboarding_complete_if_done(onboarding, progress):
    """Move the parent ``boarding_status`` to the DocType's own
    "Completed" option once every activity is done.

    HRMS normally drives this from the linked Project's
    ``percent_complete`` (``update_employee_boarding_status``); this only
    closes the gap when the activities are all finished but the parent
    has not caught up. No new status values are introduced.
    """

    if not onboarding or not progress.get("is_complete"):
        return

    if onboarding.get("docstatus") != 1:
        return

    if onboarding.get("boarding_status") == STATUS_COMPLETED:
        return

    try:

        frappe.db.set_value(
            "Employee Onboarding",
            onboarding.get("name"),
            "boarding_status",
            STATUS_COMPLETED,
        )

        frappe.db.commit()

        onboarding["boarding_status"] = STATUS_COMPLETED

    except Exception:

        frappe.log_error(
            frappe.get_traceback(),
            "WhatsApp HR Bot - Onboarding Status Update Error",
        )


# ============================================================
# ONBOARDING MENU
# ============================================================

def send_onboarding_menu(doc, phone, employee=None):
    """Show the onboarding sub-menu using the bot's existing interactive
    button implementation.
    """

    from whatsapp_hr_bot.whatsapp_handler import send_interactive, set_state

    set_state(
        phone,
        {
            "flow": ONBOARDING_FLOW,
            "step": "onboarding_menu",
            "employee": employee,
        },
    )

    send_interactive(
        doc,
        "\U0001f464 Employee Onboarding\n\n"
        "What would you like to check?",
        [
            {
                "id": BUTTON_CHECKLIST,
                "title": "\U0001f4cb My Checklist",
            },
            {
                "id": BUTTON_PROGRESS,
                "title": "\U0001f4ca My Progress",
            },
            {
                "id": BUTTON_PENDING,
                "title": "⏳ Pending Tasks",
            },
        ],
    )


# ============================================================
# ENTRY POINT
# ============================================================

def handle_onboarding_message(doc, phone, action):
    """Handle one step of the onboarding flow.

    ``action`` is either an interactive id (``my_onboarding``,
    ``onboarding_checklist``, ...) or the free text the employee typed
    while an onboarding session is open.
    """

    from whatsapp_hr_bot.whatsapp_handler import (
        clear_state,
        normalize_text,
        send_text,
    )

    action = (action or "").strip()

    # --------------------------------------------------------
    # Identify the employee behind this WhatsApp number
    # --------------------------------------------------------

    employee = get_employee_from_whatsapp(phone)

    if not employee:

        clear_state(phone)

        send_text(doc, NO_EMPLOYEE_MESSAGE)

        return

    normalized = normalize_text(action)

    # --------------------------------------------------------
    # Open / re-open the onboarding menu
    # --------------------------------------------------------

    if (
        action == BUTTON_MY_ONBOARDING
        or is_onboarding_keyword(normalized)
        or normalized in ["back", "onboarding menu"]
    ):

        # Check the record up front so an employee with no onboarding is
        # told so straight away, instead of being shown a menu whose
        # every option then dead-ends.

        if not get_active_onboarding(employee):

            clear_state(phone)

            send_text(doc, NO_ONBOARDING_MESSAGE)

            return

        send_onboarding_menu(doc, phone, employee)

        return

    if action not in [
        BUTTON_CHECKLIST,
        BUTTON_PROGRESS,
        BUTTON_PENDING,
    ]:

        send_text(
            doc,
            "Please select one of the onboarding options shown above, "
            "or type *Hii* to open the main menu.",
        )

        return

    # --------------------------------------------------------
    # Every option below needs the onboarding record
    # --------------------------------------------------------

    onboarding = get_active_onboarding(employee)

    if not onboarding:

        clear_state(phone)

        send_text(doc, NO_ONBOARDING_MESSAGE)

        return

    activities = get_onboarding_activities(onboarding)

    progress = get_onboarding_progress(activities)

    mark_onboarding_complete_if_done(onboarding, progress)

    # --------------------------------------------------------
    # MY CHECKLIST
    # --------------------------------------------------------

    if action == BUTTON_CHECKLIST:

        send_text(doc, format_onboarding_checklist(activities))

    # --------------------------------------------------------
    # MY PROGRESS
    # --------------------------------------------------------

    elif action == BUTTON_PROGRESS:

        send_text(doc, format_onboarding_progress(progress))

        if progress.get("is_complete"):
            send_text(doc, COMPLETION_MESSAGE)

    # --------------------------------------------------------
    # PENDING TASKS
    # --------------------------------------------------------

    elif action == BUTTON_PENDING:

        send_text(doc, format_pending_tasks(activities))

    # --------------------------------------------------------
    # Keep the onboarding session open for the next selection
    # --------------------------------------------------------

    send_onboarding_menu(doc, phone, employee)
