app_name = "whatsapp_hr_bot"
app_title = "WhatsApp HR Bot"
app_publisher = "Inanovai Technologies"
app_description = "WhatsApp HR bot for employee leave management"
app_email = "thrisha.shetty@inanovai.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "whatsapp_hr_bot",
# 		"logo": "/assets/whatsapp_hr_bot/logo.png",
# 		"title": "WhatsApp HR Bot",
# 		"route": "/whatsapp_hr_bot",
# 		"has_permission": "whatsapp_hr_bot.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/whatsapp_hr_bot/css/whatsapp_hr_bot.css"
# Shared client-side helper for the generic "Send WhatsApp" buttons
# (notify/config.py + notify/engine.py) - unrelated to the leave bot.
app_include_js = "/assets/whatsapp_hr_bot/js/send_whatsapp.js"

# include js, css files in header of web template
# web_include_css = "/assets/whatsapp_hr_bot/css/whatsapp_hr_bot.css"
# web_include_js = "/assets/whatsapp_hr_bot/js/whatsapp_hr_bot.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "whatsapp_hr_bot/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {
    "Leave Application": "public/js/leave_application.js",
    # Generic "Send WhatsApp" button (notify/config.py) - independent
    # of the leave-bot entry above.
    "Purchase Order": "public/js/purchase_order.js",
    "Sales Order": "public/js/sales_order.js",
    "Expense Claim": "public/js/expense_claim.js",
    "Purchase Receipt": "public/js/purchase_receipt.js",
    "Delivery Note": "public/js/delivery_note.js",
    "Sales Invoice": "public/js/sales_invoice.js",
    "Payment Entry": "public/js/payment_entry.js",
}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "whatsapp_hr_bot/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "whatsapp_hr_bot.utils.jinja_methods",
# 	"filters": "whatsapp_hr_bot.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "whatsapp_hr_bot.install.before_install"
# after_install = "whatsapp_hr_bot.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "whatsapp_hr_bot.uninstall.before_uninstall"
# after_uninstall = "whatsapp_hr_bot.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "whatsapp_hr_bot.utils.before_app_install"
# after_app_install = "whatsapp_hr_bot.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "whatsapp_hr_bot.utils.before_app_uninstall"
# after_app_uninstall = "whatsapp_hr_bot.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "whatsapp_hr_bot.notifications.get_notification_config"

# Awesome Bar
# -----------
# Extra search results: list of dicts with label, description, route, index.
# route: ["List", "ToDo"], "/desk/docs/some/page", or "https://example.com"
# awesomebar_search = ["whatsapp_hr_bot.search.awesomebar_results"]

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"whatsapp_hr_bot.tasks.all"
# 	],
# 	"daily": [
# 		"whatsapp_hr_bot.tasks.daily"
# 	],
# 	"hourly": [
# 		"whatsapp_hr_bot.tasks.hourly"
# 	],
# 	"weekly": [
# 		"whatsapp_hr_bot.tasks.weekly"
# 	],
# 	"monthly": [
# 		"whatsapp_hr_bot.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "whatsapp_hr_bot.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "whatsapp_hr_bot.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "whatsapp_hr_bot.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["whatsapp_hr_bot.utils.before_request"]
# after_request = ["whatsapp_hr_bot.utils.after_request"]

# Job Events
# ----------
# before_job = ["whatsapp_hr_bot.utils.before_job"]
# after_job = ["whatsapp_hr_bot.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"whatsapp_hr_bot.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

# Additive Custom Fields on the existing `WhatsApp Message` doctype
# (frappe_whatsapp) so it can double as a full per-document notification
# log for notify/config.py's rules: recipient, recipient name, which
# notification fired, when it was sent, and the error/response on
# failure. Unrelated to the leave-bot's own use of WhatsApp Message.
fixtures = [
    {"doctype": "Custom Field", "filters": [["dt", "=", "WhatsApp Message"], ["fieldname", "like", "custom_%"]]},
    {"doctype": "Custom Field", "filters": [["dt", "=", "Employee"], ["fieldname", "like", "custom_whatsapp_%"]]},
]

doc_events = {
    "WhatsApp Message": {
        "after_insert": "whatsapp_hr_bot.whatsapp_handler.handle_whatsapp_message"
    },
    # Generic WhatsApp notifications (notify/config.py, notify/engine.py) -
    # every configured DocType/event routes to the same handler; add a
    # new DocType by adding a rule in notify/config.py and a line here.
    "Purchase Order": {
        "on_submit": "whatsapp_hr_bot.notify.engine.on_doc_event",
    },
    "Sales Order": {
        "on_submit": "whatsapp_hr_bot.notify.engine.on_doc_event",
    },
    # Expense Claim is intentionally NOT wired here - it sends only via
    # the manual "Send WhatsApp" button (public/js/expense_claim.js ->
    # api.send_now -> dispatch), never automatically on submit/approval.
    "Purchase Receipt": {
        "on_submit": "whatsapp_hr_bot.notify.engine.on_doc_event",
    },
    "Delivery Note": {
        "on_submit": "whatsapp_hr_bot.notify.engine.on_doc_event",
    },
    "Sales Invoice": {
        "on_submit": "whatsapp_hr_bot.notify.engine.on_doc_event",
    },
    "Payment Entry": {
        "on_submit": "whatsapp_hr_bot.notify.engine.on_doc_event",
    },
}
