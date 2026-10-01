"""Currency of an expense claim raised over WhatsApp.

The amount is stored **exactly as the employee entered it**, in the
currency they picked. USD 100 is stored as 100 with
``custom_expense_currency`` = USD; nothing is converted.

Making ERPNext show it that way takes one more thing, because every
Currency field on Expense Claim ships declared as
``Company:company:default_currency`` - so a USD claim would render as
"₹ 100.00". The Property Setters in fixtures/property_setter.json repoint
those fields' ``options`` at ``custom_expense_currency`` instead, which is
all Frappe needs: ``frappe.model.meta.get_field_currency`` reads a plain
fieldname option off the document (and, for a row in the expenses table,
off its parent). The claim then displays in its own currency - amount,
Total Claimed, Total Sanctioned and Grand Total alike.

Those Property Setters apply to *every* Expense Claim, including ones
created from the desk that know nothing about this app, so
:func:`set_default_currency` runs on every claim's ``validate`` and fills
the field with the company's default currency when it is empty. A
desk-created claim therefore renders exactly as it did before.

One consequence to be aware of, because nothing here can fix it: HRMS
books an approved claim's GL Entries with ``grand_total`` as the amount
and the company-currency payable/expense accounts as the accounts (see
``make_gl_entries`` in hrms/hr/doctype/expense_claim). ERPNext's Expense
Claim has no ``conversion_rate`` and no account-currency handling, so a
claim raised in another currency posts its face value to those accounts.
Accounts has to make the exchange adjustment. The field description on
``custom_expense_currency`` says so, so whoever opens the claim sees it.
"""

import re

import frappe
from frappe.utils import flt

# Button/list-reply id prefix for the currency step, same
# "<name>:<value>" convention as the flow's leave_type: / expense_type:
# ids (see whatsapp_handler.is_valid_button_id).
CURRENCY_BUTTON_PREFIX = "expense_currency:"

# The Custom Field every Currency field on Expense Claim now takes its
# currency from - see the module docstring.
CURRENCY_FIELD = "custom_expense_currency"

# Offered in the WhatsApp currency list, after the company's own
# currency and filtered to the ones enabled on this site. Any other
# Currency the site has is still accepted when the employee types its
# code - the list is a shortcut, not the allowed set.
PREFERRED_CURRENCIES = (
    "INR",
    "USD",
    "EUR",
    "GBP",
    "CAD",
    "AUD",
    "AED",
    "SGD",
    "JPY",
)

# WhatsApp renders more than 3 options as a list message, capped at 10
# rows by Meta (see frappe_whatsapp's WhatsAppMessage.send_outgoing).
MAX_CURRENCY_OPTIONS = 10

# Spellings employees type instead of an ISO code. Only ever used when
# the site actually has that Currency, so this cannot invent one.
CURRENCY_ALIASES = {
    "RS": "INR",
    "RS.": "INR",
    "INR.": "INR",
    "RUPEE": "INR",
    "RUPEES": "INR",
    "DOLLAR": "USD",
    "DOLLARS": "USD",
    "EURO": "EUR",
    "EUROS": "EUR",
    "POUND": "GBP",
    "POUNDS": "GBP",
    "DIRHAM": "AED",
    "DIRHAMS": "AED",
    "YEN": "JPY",
    "YUAN": "CNY",
    "FRANC": "CHF",
}

# Characters that belong to the number itself rather than to a currency
# symbol, stripped before whatever is left is resolved as one.
_NUMERIC_CHARS = ".,+-"

# The sign is part of the match on purpose: without it "-50" would come
# back as 50 and pass the caller's "greater than 0" check.
_AMOUNT_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z.]{1,7}")


# ============================================================
# COMPANY / CURRENCY LOOKUPS
# ============================================================

def get_company_currency(company: str | None) -> str:
    """The company's default currency - what a claim is in unless the
    employee picked something else.
    """

    currency = None

    if company:
        currency = frappe.get_cached_value("Company", company, "default_currency")

    return currency or frappe.db.get_single_value("Global Defaults", "default_currency") or "INR"


def set_default_currency(doc, method=None):
    """Expense Claim ``validate`` - every claim names the currency its
    amounts are in.

    Wired for every Expense Claim, not only the ones this app creates:
    the doctype's Currency fields now read their currency from this field
    (see the module docstring), and a claim that left it empty would fall
    back to the site's default currency, which is usually but not always
    the company's. Filling it in explicitly keeps a desk-created claim
    rendering exactly as it did before.
    """

    if doc.get(CURRENCY_FIELD):
        return

    doc.set(CURRENCY_FIELD, get_company_currency(doc.get("company")))


def get_claim_currency(doc) -> str:
    """Currency of an existing claim, for messages built from it."""

    return doc.get(CURRENCY_FIELD) or get_company_currency(doc.get("company"))


def get_enabled_currencies() -> list[str]:
    return frappe.get_all("Currency", filters={"enabled": 1}, pluck="name", order_by="name asc")


def get_currency_options(company_currency: str) -> list[str]:
    """Currency codes to offer, company currency first.

    Only enabled currencies are offered; ``company_currency`` is always
    included even if somebody disabled it, since a claim in it must
    always be possible.
    """

    enabled = get_enabled_currencies()

    ordered = [company_currency]

    for code in PREFERRED_CURRENCIES:
        if code in enabled and code not in ordered:
            ordered.append(code)

    for code in enabled:
        if code not in ordered:
            ordered.append(code)

    return ordered[:MAX_CURRENCY_OPTIONS]


def get_currency_symbol(code: str) -> str:
    if not code:
        return ""

    return frappe.db.get_value("Currency", code, "symbol") or ""


def _known_currency(code: str | None) -> str | None:
    """``code`` as a real Currency name, or ``None``.

    Matches case-insensitively and through :data:`CURRENCY_ALIASES`, but
    never returns a code the site has no Currency record for.
    """

    if not code:
        return None

    candidate = str(code).strip().upper()

    if not candidate:
        return None

    candidate = CURRENCY_ALIASES.get(candidate, candidate)

    if frappe.db.exists("Currency", candidate):
        return candidate

    return None


def _resolve_symbol(symbol: str) -> tuple[str | None, list[str]]:
    """Resolve a currency symbol to a code.

    Returns ``(code, [])`` when exactly one enabled currency uses the
    symbol, ``(None, candidates)`` when several do - "$" alone is shared
    by USD, CAD, AUD, SGD and two dozen others, so it can never be
    resolved on its own - and ``(None, [])`` when none does.
    """

    symbol = (symbol or "").strip()

    if not symbol:
        return None, []

    candidates = frappe.get_all(
        "Currency",
        filters={"symbol": symbol, "enabled": 1},
        pluck="name",
        order_by="name asc",
    )

    if len(candidates) == 1:
        return candidates[0], []

    return None, candidates


# ============================================================
# PARSING WHAT THE EMPLOYEE TYPED
# ============================================================

def resolve_currency(text: str) -> tuple[str | None, list[str]]:
    """Resolve a currency the employee typed or tapped at the currency step.

    Accepts a list-reply id (``expense_currency:USD``), an ISO code in
    any case, an alias ("rupees"), or a symbol. Same return shape as
    :func:`_resolve_symbol`: a code, or ``None`` plus the ambiguous
    candidates.
    """

    value = " ".join(str(text or "").split())

    if value.startswith(CURRENCY_BUTTON_PREFIX):
        value = value[len(CURRENCY_BUTTON_PREFIX):].strip()

    if not value:
        return None, []

    code = _known_currency(value)

    if code:
        return code, []

    return _resolve_symbol(value)


def parse_amount(
    text: str, default_currency: str | None = None
) -> tuple[str | None, float | None, list[str]]:
    """Split what the employee typed at the amount step into a currency
    and a number.

    Handles "1500", "1,500.50", the symbol forms ("1500" prefixed with a
    currency symbol), "USD 100", "100 cad" and "Rs 1500". A currency
    named in the message wins over ``default_currency`` (the one picked
    at the currency step), so an employee who picked INR and then typed
    "USD 100" gets USD.

    Returns ``(currency, amount, ambiguous_candidates)``:

    - ``(code, number, [])`` on success.
    - ``(None, None, candidates)`` when the message carries an ambiguous
      symbol ("$100") that ``default_currency`` doesn't settle - the
      caller asks which one.
    - ``(None, None, [])`` when nothing usable could be read out of it.
    """

    raw = " ".join(str(text or "").split())

    if not raw:
        return None, None, []

    currency = None
    remainder = raw

    # An explicit code/alias anywhere in the message - "USD 100",
    # "100 cad", "Rs 1500".
    for word in _WORD_RE.findall(raw):
        code = _known_currency(word)

        if code:
            currency = code
            remainder = remainder.replace(word, " ", 1)
            break

    # Otherwise whatever non-numeric characters are left should be a
    # symbol; anything else is not an amount at all.
    if not currency:
        symbol = "".join(
            ch
            for ch in remainder
            if not (ch.isdigit() or ch.isspace() or ch in _NUMERIC_CHARS)
        ).strip()

        if symbol:
            code, candidates = _resolve_symbol(symbol)

            if code:
                currency = code

            elif default_currency and symbol == get_currency_symbol(default_currency):
                # "$100" from an employee who already picked CAD at the
                # currency step means CAD, even though "$" on its own
                # never could. Also covers a currency the site has but
                # has not enabled, which _resolve_symbol never offers.
                currency = default_currency

            else:
                return None, None, candidates

    match = _AMOUNT_RE.search(remainder)

    if not match:
        return None, None, []

    return currency or default_currency, flt(match.group(0).replace(",", "")), []


# ============================================================
# FORMATTING
# ============================================================

def format_money(amount, currency: str | None = None) -> str:
    """``"USD 100.00"`` - the code, not the symbol, so a WhatsApp message
    can never be read as the wrong dollar.
    """

    formatted = f"{flt(amount):,.2f}"

    return f"{currency} {formatted}" if currency else formatted
