"""Employee Onboarding self-service for the WhatsApp HR bot.

Source of truth is HRMS's own **Employee Onboarding** DocType (the form
titled "New Employee Onboarding" when you create one) and its
``activities`` child table (**Employee Boarding Activity**).

No new onboarding DocType and no new status system are introduced:

* HRMS creates one **Task** per activity on submit
  (``EmployeeBoardingController.create_task_and_notify_user``) and stores
  it on ``activity.task``. That Task's ``status`` is the existing status
  field for an onboarding activity, so it is what we report here, in
  ERPNext's own words (Open / Working / Pending Review / Overdue /
  Completed / Cancelled) - HR updating the Task in Desk is reflected on
  WhatsApp immediately, and an employee updating it from WhatsApp is
  reflected in Desk.
* The Task also already carries the holiday-adjusted ``exp_start_date`` /
  ``exp_end_date`` that HRMS computed from the activity's ``begin_on`` /
  ``duration`` day offsets, so those are the dates we display.

Only when a Task does not exist yet (onboarding still in Draft - tasks
are created on submit) do we fall back to computing the dates from
``boarding_begins_on + begin_on`` and reporting the activity as Open,
the status ERPNext will open its Task with.

The three-word model (Pending / In Progress / Completed) survives only
as the *progress rollup* - ``TASK_STATUS_MAP`` folds the Task statuses
into those buckets so "Completed: 4/7" keeps counting the way it did.
"""

import frappe

from frappe.utils import (
    add_days,
    cint,
    formatdate,
    getdate,
    strip_html,
    today,
)


# ============================================================
# CONSTANTS
# ============================================================

ONBOARDING_FLOW = "onboarding"

STATUS_PENDING = "Pending"
STATUS_IN_PROGRESS = "In Progress"
STATUS_COMPLETED = "Completed"

# The checklist shows each activity's Task status exactly as ERPNext
# holds it, so a task HR (or the employee) set to "Working" reads back
# as "Working" and not as some translation of it.

TASK_STATUS_ICONS = {
    "Open": "⏳",
    "Template": "⏳",
    "Working": "\U0001f504",
    "Pending Review": "\U0001f440",
    "Overdue": "⚠️",
    "Completed": "✅",
    "Cancelled": "\U0001f6ab",
}

# The status shown when the onboarding is still in Draft, so no Task
# exists yet - the status ERPNext will give the Task when HR submits.

DEFAULT_TASK_STATUS = "Open"

# ERPNext Task statuses -> the three buckets the *progress* rollup
# counts in. Only the counting uses these; the checklist shows the Task
# status itself.
#
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
            # What the employee sees: the Task's own status.
            "status": _resolve_task_status(task),
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

        # What the progress rollup counts: the same status folded into
        # one of the three buckets.

        activity["progress_status"] = TASK_STATUS_MAP.get(
            activity["status"],
            STATUS_PENDING,
        )

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


def _resolve_task_status(task):
    """The Task's own status, reported as ERPNext holds it.

    Tasks only exist once the onboarding is submitted; until then there
    is nothing to read, so the activity shows the status ERPNext will
    open it with.
    """

    if not task:
        return DEFAULT_TASK_STATUS

    return task.get("status") or DEFAULT_TASK_STATUS


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

    if activity.get("progress_status") == STATUS_COMPLETED:
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

        status = activity.get("progress_status")

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

        status = activity.get("status") or DEFAULT_TASK_STATUS

        icon = TASK_STATUS_ICONS.get(status, "⏳")

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

    # Tell the employee they can reply to change a status - otherwise
    # the update syntax is invisible.

    lines.append(UPDATE_HINT)

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
        if activity.get("progress_status") != STATUS_COMPLETED
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
# STATUS UPDATE - CONSTANTS
# ============================================================

# The Task statuses an employee may set from WhatsApp. These are
# ERPNext's own Task statuses written through as-is, so every option HR
# can pick in the Task form can also be set from a message, and the
# confirmation can name the status the employee actually asked for.
#
# "Template" is the one option left out: it marks a task as a template
# rather than describing progress, and ``Task.validate_status`` forces
# it back for non-template tasks anyway.

SETTABLE_TASK_STATUSES = [
    "Open",
    "Working",
    "Pending Review",
    "Overdue",
    "Completed",
    "Cancelled",
]

# Spellings accepted on the right-hand side of "<activity> : <status>",
# resolved to one of SETTABLE_TASK_STATUSES. Every Task status is
# accepted under its own name; the three words this flow *reports* with
# (see TASK_STATUS_MAP) are kept as aliases so "Pending" and
# "In Progress" go on working. Compared after ``normalize_text``.

STATUS_ALIASES = {
    "open": "Open",
    "pending": "Open",
    "not started": "Open",
    "yet to start": "Open",
    "todo": "Open",
    "to do": "Open",

    "working": "Working",
    "in progress": "Working",
    "inprogress": "Working",
    "in-progress": "Working",
    "progress": "Working",
    "started": "Working",
    "ongoing": "Working",
    "wip": "Working",

    "pending review": "Pending Review",
    "review": "Pending Review",
    "in review": "Pending Review",
    "for review": "Pending Review",
    "under review": "Pending Review",

    "overdue": "Overdue",
    "late": "Overdue",
    "delayed": "Overdue",

    "completed": "Completed",
    "complete": "Completed",
    "done": "Completed",
    "finished": "Completed",

    "cancelled": "Cancelled",
    "canceled": "Cancelled",
    "cancel": "Cancelled",
    "dropped": "Cancelled",
    "not required": "Cancelled",
    "not applicable": "Cancelled",
}

# Separators accepted between the activity name and the status. ":" is
# tried first so a hyphen inside an activity name never wins over an
# explicit colon.

STATUS_SEPARATORS = [":", "=", "–", "—", "-"]

# Padding an employee may add around the status ("Laptop : done!"),
# including a status icon echoed back from the checklist.

STATUS_TRIM_CHARS = (
    " .!*_"
    "✅⏳⚠️\U0001f504\U0001f440\U0001f6ab"
)

# Outcomes of a status write.

UPDATE_DONE = "done"
UPDATE_UNCHANGED = "unchanged"
UPDATE_FAILED = "failed"

UPDATE_HINT = "To update a task, reply like *Laptop : Completed*."

UNKNOWN_STATUS_HINT = "Please use one of: {0}.".format(
    ", ".join(SETTABLE_TASK_STATUSES)
)

# A near miss is only worth answering when the status is short - a long
# tail is prose that happens to contain a separator, not a status.

MAX_STATUS_LENGTH = 40

TASK_NOT_CREATED_MESSAGE = (
    "HR has not submitted your onboarding record yet, so this activity "
    "has no task to update."
)

TASK_MISSING_MESSAGE = (
    "The task linked to this activity no longer exists.\n\n"
    "Please contact HR."
)


# ============================================================
# STATUS UPDATE - PARSING
# ============================================================

def _normalize(value):
    """``normalize_text`` from the handler, imported lazily like the
    rest of this module's handler use.
    """

    from whatsapp_hr_bot.whatsapp_handler import normalize_text

    return normalize_text(value)


def match_status(value):
    """Resolve what the employee typed to a settable Task status."""

    normalized = _normalize(value).strip(STATUS_TRIM_CHARS)

    normalized = " ".join(normalized.split())

    return STATUS_ALIASES.get(normalized)


def split_on_separator(text):
    """Split ``text`` once, on the first separator that leaves both
    sides non-empty.

    Only used to spot a near miss - a message shaped like a status
    update whose status is not one we know - so the employee gets the
    list of statuses instead of the generic prompt.
    """

    raw = (text or "").strip()

    for separator in STATUS_SEPARATORS:

        left, found, right = raw.partition(separator)

        if not found:
            continue

        left = left.strip()

        right = right.strip()

        if left and right:
            return left, right

    return None, None


def parse_status_update(text):
    """Split "<activity> : <status>" into ``(activity_name, status)``.

    Returns ``(None, None)`` unless the part after a separator is a
    status we recognise, so an ordinary message that happens to contain
    a colon or a hyphen falls through to the rest of the flow
    untouched.
    """

    raw = (text or "").strip()

    if not raw:
        return None, None

    for separator in STATUS_SEPARATORS:

        parts = raw.split(separator)

        if len(parts) < 2:
            continue

        # Left to right, so a hyphen inside the activity name
        # ("Pre-joining Docs - Completed") or inside the status
        # ("Laptop - in-progress") is not mistaken for the separator.

        for index in range(1, len(parts)):

            activity_name = separator.join(parts[:index]).strip()

            status = match_status(separator.join(parts[index:]))

            if activity_name and status:
                return activity_name, status

    return None, None


# ============================================================
# STATUS UPDATE - ACTIVITY MATCHING
# ============================================================

def find_activity_by_name(activities, activity_name):
    """Match what the employee typed against the activity names on their
    own onboarding record.

    Returns ``(activity, candidates)``. An exact name (ignoring case and
    spacing) wins; otherwise a partial match is used only when it is
    unambiguous. ``candidates`` carries the near misses so the caller
    can ask which one was meant.
    """

    normalized = _normalize(activity_name)

    if not normalized:
        return None, []

    exact = [
        activity
        for activity in (activities or [])
        if _normalize(activity.get("activity_name")) == normalized
    ]

    if exact:
        return exact[0], exact

    partial = [
        activity
        for activity in (activities or [])
        if normalized in _normalize(activity.get("activity_name"))
    ]

    if len(partial) == 1:
        return partial[0], partial

    return None, partial


# ============================================================
# STATUS UPDATE - WRITE
# ============================================================

def update_activity_status(activity, task_status):
    """Write ``task_status`` onto the ERPNext Task behind an activity.

    The Task is saved as a document rather than through
    ``db.set_value`` so ERPNext's and HRMS's own hooks still run - in
    particular HRMS's ``update_task``, which refreshes the Project's
    ``percent_complete`` and through it the parent Employee Onboarding's
    ``boarding_status``.

    Returns ``(outcome, detail)``, outcome being ``UPDATE_DONE``,
    ``UPDATE_UNCHANGED`` or ``UPDATE_FAILED``.
    """

    task_name = (activity or {}).get("task")

    if not task_name:
        return UPDATE_FAILED, TASK_NOT_CREATED_MESSAGE

    if task_status not in SETTABLE_TASK_STATUSES:
        return UPDATE_FAILED, None

    # The webhook runs as *Guest*, which the Task save path and its
    # on_update hooks (project rollup, closing assignments) will not
    # accept. Elevate for the write and restore afterwards, exactly like
    # the leave flow does when it creates a Leave Application.

    original_user = frappe.session.user

    try:

        frappe.set_user("Administrator")

        task = frappe.get_doc("Task", task_name)

        if task.status == task_status:
            return UPDATE_UNCHANGED, None

        task.status = task_status

        task.save(ignore_permissions=True)

        frappe.db.commit()

        return UPDATE_DONE, None

    except frappe.DoesNotExistError:

        return UPDATE_FAILED, TASK_MISSING_MESSAGE

    except frappe.ValidationError as exc:

        # ERPNext rejected the change (most often a dependent task that
        # is still open). Show its actual reason, like the leave flow.

        frappe.log_error(
            frappe.get_traceback(),
            "WhatsApp HR Bot - Onboarding Task Status Error",
        )

        return UPDATE_FAILED, (
            strip_html(str(exc)).strip() or None
        )

    except Exception:

        frappe.log_error(
            frappe.get_traceback(),
            "WhatsApp HR Bot - Onboarding Task Status Error",
        )

        return UPDATE_FAILED, None

    finally:

        frappe.set_user(original_user)


# ============================================================
# STATUS UPDATE - MESSAGE FORMATTING
# ============================================================

def format_status_update_confirmation(activity, task_status, outcome):
    """Name the Task status the employee asked for, so "… : Working"
    is not confirmed back as "In Progress".
    """

    icon = TASK_STATUS_ICONS.get(task_status, "✅")

    name = activity.get("activity_name") or "Activity"

    if outcome == UPDATE_UNCHANGED:
        return f"{icon} {name} is already marked as {task_status}."

    return f"{icon} {name} marked as {task_status}."


def format_activity_not_matched(activity_name, activities, candidates):

    lines = [
        f"❓ I couldn't match \"{activity_name}\" to one of your "
        "onboarding activities.",
        "",
    ]

    if candidates:

        lines.append("Did you mean:")

        listed = candidates

    else:

        lines.append("Your onboarding activities are:")

        listed = activities or []

    for activity in listed:
        lines.append(f"• {activity.get('activity_name') or 'Activity'}")

    lines.append("")

    lines.append(UPDATE_HINT)

    return _truncate("\n".join(lines))


def format_unknown_status(activity, typed_status):

    name = activity.get("activity_name") or "Activity"

    return (
        f"❓ I don't recognise \"{typed_status}\" as a status for "
        f"{name}.\n\n"
        f"{UNKNOWN_STATUS_HINT}"
    )


def format_status_update_failure(activity, task_status, detail):

    name = activity.get("activity_name") or "Activity"

    message = f"❌ I couldn't mark {name} as {task_status}."

    if detail:
        message += f"\n\n{detail}"

    else:
        message += "\n\nPlease try again, or contact HR."

    return _truncate(message)


# ============================================================
# STATUS UPDATE - ENTRY POINT
# ============================================================

def handle_status_update(doc, phone, text, employee=None):
    """Try to read ``text`` as "<activity> : <status>" and apply it.

    Returns True when the message was a status update and has been
    answered, False when it was not one, so the caller carries on with
    its normal handling.
    """

    activity_name, task_status = parse_status_update(text)

    typed_status = None

    if not task_status:

        # Shaped like an update but with a status we don't know. Only
        # worth answering once the left-hand side turns out to be one of
        # this employee's activities, which is checked further down.

        activity_name, typed_status = split_on_separator(text)

        if not typed_status or len(typed_status) > MAX_STATUS_LENGTH:
            return False

    if not activity_name:
        return False

    from whatsapp_hr_bot.whatsapp_handler import send_text

    # --------------------------------------------------------
    # Identify the employee behind this WhatsApp number
    # --------------------------------------------------------

    employee = employee or get_employee_from_whatsapp(phone)

    if not employee:

        # A near miss is too weak a signal to answer with an error, so
        # it is handed back to the caller instead.

        if typed_status:
            return False

        send_text(doc, NO_EMPLOYEE_MESSAGE)

        return True

    # --------------------------------------------------------
    # Their active onboarding record and its activities
    # --------------------------------------------------------

    onboarding = get_active_onboarding(employee)

    if not onboarding:

        if typed_status:
            return False

        send_text(doc, NO_ONBOARDING_MESSAGE)

        return True

    activities = get_onboarding_activities(onboarding)

    activity, candidates = find_activity_by_name(
        activities,
        activity_name
    )

    if not activity:

        if typed_status:
            return False

        send_text(
            doc,
            format_activity_not_matched(
                activity_name,
                activities,
                candidates
            ),
        )

        return True

    # --------------------------------------------------------
    # A real activity, but a status we don't know: list the ones
    # we do instead of letting it fall through to the generic
    # "I couldn't understand that" prompt.
    # --------------------------------------------------------

    if typed_status:

        send_text(doc, format_unknown_status(activity, typed_status))

        return True

    # --------------------------------------------------------
    # Update the Task behind the activity
    # --------------------------------------------------------

    outcome, detail = update_activity_status(activity, task_status)

    if outcome == UPDATE_FAILED:

        send_text(
            doc,
            format_status_update_failure(activity, task_status, detail)
        )

        return True

    send_text(
        doc,
        format_status_update_confirmation(
            activity,
            task_status,
            outcome
        )
    )

    # --------------------------------------------------------
    # A last activity being completed closes the onboarding, so
    # re-read the activities before reporting on the whole record.
    # --------------------------------------------------------

    progress = get_onboarding_progress(
        get_onboarding_activities(onboarding)
    )

    mark_onboarding_complete_if_done(onboarding, progress)

    if progress.get("is_complete"):
        send_text(doc, COMPLETION_MESSAGE)

    return True


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

        # "<activity> : <status>" typed while the onboarding session is
        # open. Only a message that parses as one is taken here; the
        # rest still get the prompt below.

        if handle_status_update(doc, phone, action, employee):

            send_onboarding_menu(doc, phone, employee)

            return

        send_text(
            doc,
            "Please select one of the onboarding options shown above, "
            "or type *Hii* to open the main menu.\n\n"
            f"{UPDATE_HINT}",
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
