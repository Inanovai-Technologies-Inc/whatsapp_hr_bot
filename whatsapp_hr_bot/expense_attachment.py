"""Bill / receipt proof on Expense Claim.

The ``custom_bill_attachment`` Attach field (fixtures/custom_field.json)
lets the employee upload the bill or receipt backing a claim. Everything
about *storing* it is stock Frappe: the upload becomes a ``File`` row
linked to the claim (``attached_to_doctype``/``attached_to_name``/
``attached_to_field``) - by ``frappe.handler.upload_file`` when the claim
already exists, and by ``file/utils.py:attach_files_to_document`` on every
later save, which also covers a ``file_url`` set through the REST API. So
the proof travels with the document: it shows both in the field and in the
form's sidebar attachments, and the Expense Approver can open it even
though it is private, because ``File``'s permission check falls back to
read access on the document it is attached to
(``frappe/core/doctype/file/file.py:has_permission``).

What stock Frappe does *not* do is restrict which formats may be uploaded.
The client restricts the file picker (public/js/expense_claim.js), but the
REST API and the attach dialog's "link" tab bypass that, so the same rule
is enforced here, wired to the Expense Claim ``validate`` /
``before_update_after_submit`` events in hooks.py.

The same field also backs the WhatsApp flow's optional bill step: a file
sent in chat arrives as a ``File`` on the inbound WhatsApp Message, and
``stage_bill_file`` turns it into the claim's own private attachment (see
``whatsapp_handler.handle_inbound_whatsapp_file``).

The field is optional by design - nothing here makes a claim without a
bill invalid.
"""

import os
from urllib.parse import unquote, urlparse

import frappe
from frappe import _

BILL_FIELD = "custom_bill_attachment"

# Kept in sync with ALLOWED_BILL_EXTENSIONS in public/js/expense_claim.js.
ALLOWED_BILL_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png")


def validate_bill_attachment(doc, method=None):
    """Reject a bill/receipt upload that isn't a PDF or an image."""

    file_url = (doc.get(BILL_FIELD) or "").strip()

    if not file_url:
        # Optional field - a claim with no bill attached is valid.
        return

    if _get_extension(file_url) not in ALLOWED_BILL_EXTENSIONS:
        frappe.throw(
            _("{0}: only {1} files can be uploaded as bill/receipt proof.").format(
                _(doc.meta.get_label(BILL_FIELD)),
                ", ".join(ext.lstrip(".").upper() for ext in ALLOWED_BILL_EXTENSIONS),
            ),
            title=_("Invalid File Format"),
        )


def _get_extension(file_url: str) -> str:
    """Lower-cased extension of ``file_url``.

    ``file_url`` is normally ``/files/bill.pdf`` or
    ``/private/files/bill.pdf``, but the attach dialog's "link" tab can
    store a full URL with a query string, hence the parsing.
    """

    path = urlparse(file_url).path or file_url

    return os.path.splitext(unquote(path))[1].lower()


def is_allowed_bill_file(file_name_or_url: str) -> bool:
    """Whether a file may be used as bill/receipt proof, by extension."""

    return _get_extension(file_name_or_url or "") in ALLOWED_BILL_EXTENSIONS


def stage_bill_file(source_file: str) -> str | None:
    """Copy ``source_file`` into a private File ready to be attached to an
    Expense Claim, and return its ``file_url`` (``None`` if the format is
    not allowed).

    Used for a bill sent over WhatsApp. The source File belongs to the
    inbound WhatsApp Message - the chat record - and is public, so it is
    copied rather than re-pointed: the conversation keeps its own
    attachment, and the claim gets a private one whose only readers are
    the people who can read the claim.

    The copy is deliberately left unattached. Inserting the claim with
    this url in ``custom_bill_attachment`` is what links it, via
    ``file/utils.py:attach_files_to_document`` - the same path an upload
    from the desk form takes - so the claim is created in one insert, with
    its proof already on it.

    Returns ``None`` rather than raising if the file is gone or of an
    unexpected type: an attachment is optional, so a claim the employee
    has already confirmed must still be created without it.
    """

    if not frappe.db.exists("File", source_file):
        return None

    source = frappe.get_doc("File", source_file)

    extension = _get_extension(source.file_name or source.file_url)

    if extension not in ALLOWED_BILL_EXTENSIONS:
        return None

    copy = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": f"expense-bill{extension}",
            "content": source.get_content(),
            "is_private": 1,
        }
    ).insert(ignore_permissions=True)

    return copy.file_url
