"""
Helpers for importing and exporting JFJ Renthouse data.

Everything here is pure Python (no Flask / MongoDB) so it can be reused by
CSV downloads, CSV uploads, published Google Sheets, and the Google Sheets API.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# (MongoDB field, spreadsheet column header)
PAYMENT_FIELDS = [
    ("room_no", "Room No."),
    ("tenant_name", "Tenant Name"),
    ("payment_due", "Payment Due"),
    ("amount_paid", "Amount Paid"),
    ("balance", "Balance"),
    ("date", "Date"),
    ("status", "Status"),
    ("payment_method", "Payment Method"),
    ("category", "Category"),
    ("notes", "Notes"),
]

TENANT_FIELDS = [
    ("room_no", "Room No."),
    ("tenant_name", "Tenant Name"),
    ("contact_number", "Contact Number"),
    ("email", "Email"),
    ("monthly_rent", "Monthly Rent"),
    ("move_in_date", "Move In Date"),
    ("move_out_date", "Move Out Date"),
    ("status", "Status"),
    ("notes", "Notes"),
]

EXPENSE_FIELDS = [
    ("title", "Title"),
    ("amount", "Amount"),
    ("category", "Category"),
    ("date", "Date"),
    ("room_no", "Room No."),
    ("tenant_name", "Tenant Name"),
    ("notes", "Notes"),
]

DATASETS = {
    "payments": {
        "label": "Payments",
        "description": "Rent and other payments received, including balances and methods.",
        "fields": PAYMENT_FIELDS,
    },
    "tenants": {
        "label": "Tenants",
        "description": "Tenant directory with rooms, contacts, rent, and move-in/out dates.",
        "fields": TENANT_FIELDS,
    },
    "expenses": {
        "label": "Expenses",
        "description": "Operating expenses, optionally linked to a room or tenant.",
        "fields": EXPENSE_FIELDS,
    },
}

_BASE_ALIASES = {
    "room": "room_no",
    "room_number": "room_no",
    "roomno": "room_no",
    "unit": "room_no",
    "tenant": "tenant_name",
    "name": "tenant_name",
    "tenantname": "tenant_name",
    "contact": "contact_number",
    "phone": "contact_number",
    "mobile": "contact_number",
    "contact_no": "contact_number",
    "email_address": "email",
    "rent": "monthly_rent",
    "movein": "move_in_date",
    "movein_date": "move_in_date",
    "move_in": "move_in_date",
    "moveout": "move_out_date",
    "moveout_date": "move_out_date",
    "move_out": "move_out_date",
    "date_paid": "date",
    "payment_date": "date",
    "method": "payment_method",
    "payment_type": "payment_method",
    "note": "notes",
    "remarks": "notes",
}

_ALIASES = {
    "payments": {
        **_BASE_ALIASES,
        "due": "payment_due",
        "amount_due": "payment_due",
        "paymentdue": "payment_due",
        "amount": "amount_paid",
        "paid": "amount_paid",
        "amountpaid": "amount_paid",
        "balance_due": "balance",
    },
    "tenants": {
        **_BASE_ALIASES,
        "monthly": "monthly_rent",
        "monthly_payment": "monthly_rent",
        "status_of_tenant": "status",
    },
    "expenses": {
        **_BASE_ALIASES,
        "description": "title",
        "expense": "title",
        "particulars": "title",
        "expense_title": "title",
        "amount_paid": "amount",
        "cost": "amount",
    },
}

MAX_IMPORT_ROWS = 5000
MAX_IMPORT_BYTES = 10 * 1024 * 1024  # 10 MB


def normalize_header(value):
    """'Room No.' -> 'room_no', 'Amount Paid (PHP)' -> 'amount_paid'."""
    text = str(value or "").strip().lower()
    text = text.replace("₱", " ").replace("php", " ")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def money(value):
    """Turns '₱ 8,500.00' into 8500.0."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def normalize_date(value):
    """Returns YYYY-MM-DD when possible, otherwise the cleaned original text."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def canonical_row(raw_row, data_type):
    """Normalizes spreadsheet headers into the field names used by MongoDB."""
    aliases = _ALIASES.get(data_type, {})
    row = {}
    for key, value in raw_row.items():
        if key is None:
            continue
        normalized = normalize_header(key)
        field = aliases.get(normalized, normalized)
        if field and field not in row:
            row[field] = value
    return row


def read_csv(text):
    """Parses CSV text (including files exported from Google Sheets/Excel)."""
    text = (text or "").lstrip("\ufeff")
    if not text.strip():
        return []

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return [dict(row) for row in reader]


def _prepare_payments(rows):
    docs, errors = [], []
    today = datetime.now().strftime("%Y-%m-%d")

    for index, raw in enumerate(rows, start=2):
        if not any(clean(v) for v in raw.values()):
            continue
        row = canonical_row(raw, "payments")
        room = clean(row.get("room_no")).upper()
        name = clean(row.get("tenant_name"))
        if not room and not name:
            errors.append(f"Row {index}: missing room number and tenant name.")
            continue

        due = money(row.get("payment_due"))
        paid = money(row.get("amount_paid"))
        balance = max(0.0, due - paid)
        docs.append({
            "tenant_id": None,
            "room_no": room,
            "tenant_name": name,
            "payment_due": due,
            "amount_paid": paid,
            "balance": balance,
            "date": normalize_date(row.get("date")) or today,
            "status": "Paid" if balance == 0 else "Pending",
            "payment_method": clean(row.get("payment_method")) or "Cash",
            "category": clean(row.get("category")) or "Rent",
            "notes": clean(row.get("notes")),
        })

    return docs, errors


def _prepare_tenants(rows):
    docs, errors = [], []
    today = datetime.now().strftime("%Y-%m-%d")

    for index, raw in enumerate(rows, start=2):
        if not any(clean(v) for v in raw.values()):
            continue
        row = canonical_row(raw, "tenants")
        room = clean(row.get("room_no")).upper()
        name = clean(row.get("tenant_name"))
        if not room or not name:
            errors.append(f"Row {index}: room number and tenant name are required.")
            continue

        status_raw = clean(row.get("status")).lower()
        if status_raw.startswith("moved"):
            status = "Moved Out"
        elif status_raw in ("", "active", "current", "occupied"):
            status = "Active"
        else:
            status = "Active"

        move_out = normalize_date(row.get("move_out_date"))
        if status == "Moved Out" and not move_out:
            move_out = today

        docs.append({
            "room_no": room,
            "tenant_name": name,
            "contact_number": clean(row.get("contact_number")),
            "email": clean(row.get("email")),
            "monthly_rent": money(row.get("monthly_rent")),
            "move_in_date": normalize_date(row.get("move_in_date")),
            "move_out_date": move_out,
            "status": status,
            "notes": clean(row.get("notes")),
        })

    return docs, errors


def _prepare_expenses(rows):
    docs, errors = [], []
    today = datetime.now().strftime("%Y-%m-%d")

    for index, raw in enumerate(rows, start=2):
        if not any(clean(v) for v in raw.values()):
            continue
        row = canonical_row(raw, "expenses")
        title = clean(row.get("title"))
        if not title:
            errors.append(f"Row {index}: expense title is required.")
            continue

        docs.append({
            "title": title,
            "amount": money(row.get("amount")),
            "category": clean(row.get("category")) or "General Maintenance",
            "date": normalize_date(row.get("date")) or today,
            "room_no": clean(row.get("room_no")).upper(),
            "tenant_name": clean(row.get("tenant_name")),
            "tenant_id": None,
            "notes": clean(row.get("notes")),
        })

    return docs, errors


def prepare_rows(data_type, rows):
    """Validates and converts spreadsheet rows into MongoDB documents."""
    if data_type == "payments":
        return _prepare_payments(rows)
    if data_type == "tenants":
        return _prepare_tenants(rows)
    if data_type == "expenses":
        return _prepare_expenses(rows)
    raise ValueError(f"Unknown data type: {data_type}")


def table_rows(records, fields):
    """Builds a spreadsheet table (header row + values) from Mongo records."""
    table = [[header for _, header in fields]]
    for record in records:
        row = []
        for key, _ in fields:
            value = record.get(key, "")
            if value is None:
                value = ""
            elif isinstance(value, float):
                value = f"{value:.2f}"
            row.append(value)
        table.append(row)
    return table


def csv_text(records, fields):
    """CSV text with a BOM so Excel and Google Sheets open it correctly."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerows(table_rows(records, fields))
    return "\ufeff" + output.getvalue()


def extract_spreadsheet_key(value):
    """Accepts a full Google Sheets URL or a raw spreadsheet ID."""
    text = (value or "").strip()
    if not text:
        return None
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", text)
    if match:
        key = match.group(1)
        return key if len(key) >= 20 else None
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", text):
        return text
    return None


def _gid_from_url(parsed):
    identifiers = parse_qs(parsed.query).get("gid")
    if identifiers:
        return identifiers[0]
    match = re.search(r"gid=(\d+)", parsed.fragment or "")
    return match.group(1) if match else "0"


def to_csv_export_url(value):
    """
    Converts a Google Sheets link into a CSV download link.

    Supports:
      - 'Anyone with the link' sheet URLs  -> /export?format=csv
      - Published sheet URLs               -> .../pub?output=csv
    Returns None for anything that is not a Google Sheets URL.
    """
    parsed = urlparse((value or "").strip())
    if parsed.scheme != "https" or parsed.hostname not in ("docs.google.com", "drive.google.com"):
        return None

    # Already a published CSV/HTML link.
    if "/spreadsheets/d/e/" in parsed.path:
        query = parse_qs(parsed.query)
        if query.get("output", [""])[0].lower() != "csv":
            query["output"] = ["csv"]
        return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

    key = extract_spreadsheet_key(value)
    if not key:
        return None
    gid = _gid_from_url(parsed)
    return f"https://docs.google.com/spreadsheets/d/{key}/export?format=csv&gid={gid}"
