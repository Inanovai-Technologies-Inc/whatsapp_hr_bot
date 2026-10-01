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

``get_bill_delivery_url`` is the other direction: it hands the Expense
Approver's WhatsApp notification the *same* File, so the approver gets
the employee's actual bill alongside the Approve/Reject request (see
notify/config.py's "approval_requested" rule and
``notify.engine._send_bill_attachment``). It never copies the file - the
one attachment on the claim is what gets sent.

The field is optional by design - nothing here makes a claim without a
bill invalid.
"""

import os
from urllib.parse import unquote, urlparse

import frappe
from frappe import _
from frappe.utils import cint

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

    The name carries a random suffix so each bill is a distinct file
    name, not a dozen claims all holding "expense-bill.png". That matters
    beyond tidiness: :func:`get_bill_delivery_url` publishes the file
    under the same basename to send it to the approver, and Frappe
    refuses to move a file onto a public name something else already
    occupies.

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

    # Each claim gets its own File row, but two claims whose bills are
    # byte-identical share the file on disk - Frappe points the second row
    # at the first one's ``file_url`` (``File.save_file``), and there is no
    # flag on the insert path to opt out of that. Publishing one of them to
    # send it to an approver therefore moves the file for all of them,
    # which is why :func:`_publish_file` repoints every claim that holds
    # the old url and :func:`get_bill_file` falls back to the claim's own
    # attachment. The bytes are the same receipt either way, so the shared
    # file is not a problem as long as no claim is left pointing at a url
    # that has moved.
    copy = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": f"expense-bill-{frappe.generate_hash(length=8)}{extension}",
            "content": source.get_content(),
            "is_private": 1,
        }
    ).insert(ignore_permissions=True)

    return copy.file_url


# ============================================================
# SENDING THE BILL OVER WHATSAPP
# ============================================================

# WhatsApp message type to use per bill format. A PDF has to go as a
# "document"; an image sent as a "document" arrives as a file the
# approver must download first, so images go as "image".
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")


def get_bill_file(claim) -> "frappe.model.document.Document | None":
    """The ``File`` holding ``claim``'s bill/receipt, or ``None``.

    ``claim`` may be an Expense Claim document or any mapping with
    ``name`` and ``custom_bill_attachment`` (the notification engine
    passes a ``frappe._dict`` in one path - see
    ``engine.dispatch_to_recipient``).

    Looks the File up by the url stored in the field, preferring the row
    actually attached to this claim so a url that several documents
    happen to share can never resolve to somebody else's attachment.
    """

    file_url = (claim.get(BILL_FIELD) or "").strip()

    if not file_url:
        return None

    attached_to = {
        "attached_to_doctype": "Expense Claim",
        "attached_to_name": claim.get("name"),
    }

    name = frappe.db.get_value("File", dict(attached_to, file_url=file_url), "name")

    if not name:
        # The field's url can drift from the File it names: Frappe shares
        # one file on disk between byte-identical uploads, and moving that
        # file (publishing it, or making it private again) repoints every
        # File row that shares it but only the one document it was called
        # on. Fall back to the file this claim's own Attach field holds -
        # there is only ever one - rather than reporting no bill.
        name = frappe.db.get_value("File", dict(attached_to, attached_to_field=BILL_FIELD), "name")

    if not name:
        return None

    return frappe.get_doc("File", name)


def get_bill_media_type(file_url: str) -> str:
    """``"image"`` or ``"document"`` - the WhatsApp ``content_type`` to
    send this bill as.
    """

    return "image" if _get_extension(file_url) in IMAGE_EXTENSIONS else "document"


def get_bill_delivery_url(claim) -> str | None:
    """Absolute, publicly fetchable URL of ``claim``'s bill, or ``None``.

    Meta's servers fetch a media message's ``link`` themselves, from the
    public internet and without any session cookie, so two things have to
    be true and neither can be faked:

    - ``host_name`` must be set in site_config.json to a reachable URL.
      Without it frappe_whatsapp would build the link off whatever Host
      header a local request used (``127.0.0.1``, ...) and the message
      would silently fail delivery - the same reason the PDF attachments
      in notify/engine.py check for it. ``None`` here means "send the
      notification without the bill", not "send a broken link".
    - the File must not be private. A bill staged from chat
      (:func:`stage_bill_file`) is private, so the *existing* File is
      published in place rather than copied: ``is_private = 0`` moves it
      from ``private/files`` to ``files`` and Frappe's own
      ``File.handle_is_private_changed`` rewrites both ``file_url`` and
      the claim's ``custom_bill_attachment`` to match. One file, one
      attachment, still the claim's own - it just becomes readable by
      url, which is what delivering it to WhatsApp requires.

    Returns ``None`` (never raises) when there is no bill, the File is
    gone, or it cannot be published - an attachment is optional and must
    never hold up the approver's notification.
    """

    host_name = (frappe.conf.get("host_name") or "").rstrip("/")

    if not host_name:
        return None

    file_doc = get_bill_file(claim)

    if not file_doc:
        return None

    if file_doc.is_remote_file:
        # Already an absolute url somebody pasted in - nothing to publish.
        return file_doc.file_url

    if cint(file_doc.is_private) and not _publish_file(file_doc):
        return None

    return f"{host_name}{file_doc.file_url}"


def _publish_file(file_doc) -> bool:
    """Move ``file_doc`` out of ``private/files`` into ``files``, in place.

    Frappe does the work: setting ``is_private`` to 0 makes
    ``File.handle_is_private_changed`` move the file, rewrite
    ``file_url``, and update the field on the document it is attached to.
    Two things it does not handle are handled here.

    It refuses to move a file onto a public name that is already taken
    (``FileExistsError``), which is what a claim whose bill is called
    ``expense-bill.png`` runs into once any other claim's bill of that
    name has been published. Bills staged from chat now carry a random
    suffix (:func:`stage_bill_file`), so this only arises for ones staged
    before that and for desk uploads; either way the file is renamed to a
    free name first rather than left undeliverable.

    And when several File rows share one file on disk - Frappe reuses the
    bytes for byte-identical uploads, see ``File.validate_duplicate_entry``
    - it repoints every one of those rows but only the *one* document it
    was called on, so any other claim carrying the same bill would be left
    pointing at a url that has moved. Those are repointed too.

    Returns whether the file is now public. Never raises.
    """
    from frappe.core.doctype.file.utils import generate_file_name

    old_url = file_doc.file_url

    try:
        current_name = old_url.rsplit("/", 1)[-1]
        public_name = generate_file_name(current_name, is_private=False)

        if public_name != current_name:
            free_name = generate_file_name(public_name, is_private=True)
            source = frappe.get_site_path("private", "files", current_name)

            if not os.path.exists(source):
                return False

            os.rename(source, frappe.get_site_path("private", "files", free_name))

            file_doc.db_set(
                {"file_name": free_name, "file_url": f"/private/files/{free_name}"},
                update_modified=False,
            )
            file_doc.reload()

        file_doc.is_private = 0
        file_doc.save(ignore_permissions=True)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "WhatsApp HR Bot - Could not publish bill attachment",
        )
        return False

    if file_doc.file_url != old_url:
        frappe.db.set_value(
            "Expense Claim",
            {BILL_FIELD: old_url},
            BILL_FIELD,
            file_doc.file_url,
            update_modified=False,
        )

    return True
