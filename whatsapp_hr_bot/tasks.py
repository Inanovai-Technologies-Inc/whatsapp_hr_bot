"""Scheduled WhatsApp notifications for HR events."""

import frappe
from frappe.utils import add_days, getdate, today


def send_upcoming_holiday_notifications():
    """Notify active employees about holidays on their lists tomorrow."""
    from erpnext.setup.doctype.employee.employee import get_holiday_list_for_employee

    from whatsapp_hr_bot.notify.config import get_rules
    from whatsapp_hr_bot.notify.engine import dispatch_to_recipient

    employees_by_list = {}
    for employee in frappe.get_all("Employee", filters={"status": "Active"}, pluck="name"):
        holiday_list = get_holiday_list_for_employee(employee, raise_exception=False)
        if holiday_list:
            employees_by_list.setdefault(holiday_list, []).append(employee)

    if not employees_by_list:
        return

    tomorrow = add_days(getdate(today()), 1)
    holidays = frappe.get_all(
        "Holiday",
        filters={"parent": ["in", list(employees_by_list)], "holiday_date": tomorrow},
        fields=["name", "parent", "holiday_date", "description"],
    )
    rules = get_rules("Holiday List", "daily")

    for holiday in holidays:
        for employee in employees_by_list[holiday.parent]:
            employee_name = frappe.db.get_value("Employee", employee, "employee_name")
            notification = frappe._dict(
                doctype="Holiday List",
                name=holiday.parent,
                holiday_row=holiday.name,
                holiday_date=holiday.holiday_date,
                description=holiday.description,
                employee=employee,
                employee_name=employee_name,
            )
            for rule in rules:
                dispatch_to_recipient(notification, rule, employee)