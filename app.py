import os
import json
import secrets
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import (
    Flask,
    Response,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
)
from pymongo import MongoClient
from bson.objectid import ObjectId
from dotenv import load_dotenv

import data_tools as dt

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# Load environment variables from .env file
load_dotenv(ENV_PATH, override=True)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)

# -------------------------------------------------------------------
# SIMPLE ADMIN LOGIN
# The username and password are stored in plain text inside .env so
# they are easy to see and change.
# -------------------------------------------------------------------
def get_admin_credentials():
    """Load latest credentials from .env so edits take effect immediately."""
    load_dotenv(ENV_PATH, override=True)
    username = (os.getenv("ADMIN_USERNAME") or "admin").strip()
    password = (os.getenv("ADMIN_PASSWORD") or "").strip()
    return username, password

ADMIN_USERNAME, ADMIN_PASSWORD = get_admin_credentials()

if not ADMIN_PASSWORD:
    app.logger.warning("ADMIN_PASSWORD is not set in .env!")

# Initialize MongoDB Atlas Connection
MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    raise ValueError("MONGO_URI is missing in your .env file!")

client = MongoClient(MONGO_URI)
# Connects to 'jfj_renthouse' database specified in URI or defaults to it
db = client.get_database("jfj_renthouse")

# Known physical units in JFJ Renthouse
BUILDING_ROOMS = ["2A", "2B", "2C", "2D", "3A", "3B", "3C", "3D"]

# Metadata used by the Activity History page
ACTION_META = {
    "tenant_added":     {"label": "Tenant Added",     "category": "create",  "entity": "Tenant"},
    "tenant_updated":   {"label": "Tenant Updated",   "category": "update",  "entity": "Tenant"},
    "tenant_moved_out": {"label": "Tenant Moved Out", "category": "update",  "entity": "Tenant"},
    "tenant_deleted":   {"label": "Tenant Deleted",   "category": "delete",  "entity": "Tenant"},
    "payment_added":    {"label": "Payment Added",    "category": "create",  "entity": "Payment"},
    "payment_updated":  {"label": "Payment Updated",  "category": "update",  "entity": "Payment"},
    "payment_deleted":  {"label": "Payment Deleted",  "category": "delete",  "entity": "Payment"},
    "expense_added":    {"label": "Expense Added",    "category": "expense", "entity": "Expense"},
    "expense_updated":  {"label": "Expense Updated",  "category": "expense", "entity": "Expense"},
    "expense_deleted":  {"label": "Expense Deleted",  "category": "delete",  "entity": "Expense"},
    "admin_login":      {"label": "Admin Login",    "category": "update", "entity": "System"},
    "admin_logout":     {"label": "Admin Logout",   "category": "update", "entity": "System"},
    "data_imported":    {"label": "Data Imported",  "category": "create", "entity": "System"},
    "data_exported":    {"label": "Data Exported",  "category": "update", "entity": "System"},
}


def parse_money(value):
    """Turns user input like '8,500' into a float safely."""
    try:
        return float(str(value).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def log_activity(action, description, details=None):
    """Write an audit-trail entry so the History page can show every change."""
    meta = ACTION_META.get(action, {})
    try:
        db.activities.insert_one({
            "action": action,
            "entity": meta.get("entity", "System"),
            "category": meta.get("category", "update"),
            "description": description,
            "details": details or {},
            "created_at": datetime.now(),
        })
    except Exception as exc:  # logging should never break the main action
        app.logger.warning("Unable to write activity log: %s", exc)


def format_activity(activity):
    """Convert a raw activity document into display-friendly values."""
    created = activity.get("created_at")
    if isinstance(created, datetime):
        when = created.strftime("%b %d, %Y · %I:%M %p")
        day = created.strftime("%A, %b %d, %Y")
    else:
        when = "Unknown time"
        day = "Unknown date"

    meta = ACTION_META.get(activity.get("action"), {})
    return {
        "id": str(activity.get("_id", "")),
        "action": activity.get("action", ""),
        "label": meta.get("label", (activity.get("action") or "Activity").replace("_", " ").title()),
        "category": activity.get("category") or meta.get("category", "update"),
        "entity": activity.get("entity") or meta.get("entity", "System"),
        "description": activity.get("description", ""),
        "when": when,
        "day": day,
        "details": activity.get("details") or {},
    }


def tenant_form_payload(form):
    """Normalizes tenant form input into a MongoDB document."""
    status = form.get("status") or "Active"
    return {
        "room_no": (form.get("room_no") or "").strip().upper(),
        "tenant_name": (form.get("tenant_name") or "").strip(),
        "contact_number": (form.get("contact_number") or "").strip(),
        "email": (form.get("email") or "").strip(),
        "monthly_rent": parse_money(form.get("monthly_rent")),
        "move_in_date": (form.get("move_in_date") or "").strip(),
        "move_out_date": (form.get("move_out_date") or "").strip(),
        "status": status if status in ("Active", "Moved Out") else "Active",
        "notes": (form.get("notes") or "").strip(),
    }


def tenant_dropdown_options():
    """Lightweight tenant list used by the payment and expense forms."""
    tenants = list(db.tenants.find().sort([("status", 1), ("room_no", 1), ("tenant_name", 1)]))
    return [
        {
            "id": str(t.get("_id")),
            "name": t.get("tenant_name") or "",
            "room_no": t.get("room_no") or "",
            "rent": float(t.get("monthly_rent") or 0.0),
            "status": t.get("status") or "Active",
        }
        for t in tenants
    ]


def resolve_linked_tenant(raw_tenant_id):
    """Returns (tenant_id, tenant_name, room_no) for a selected tenant, if valid."""
    raw_tenant_id = (raw_tenant_id or "").strip()
    if not raw_tenant_id:
        return (None, "", "")
    try:
        tenant_oid = ObjectId(raw_tenant_id)
    except Exception:
        return (None, "", "")

    tenant = db.tenants.find_one({"_id": tenant_oid})
    if not tenant:
        return (None, "", "")
    return (tenant_oid, tenant.get("tenant_name") or "", tenant.get("room_no") or "")


def parse_date_components(date_str):
    """Safely extracts (YYYY, YYYY-MM) from various date formats."""
    if not date_str:
        return ("Unknown", "Unknown")
    s = str(date_str).strip()
    if "-" in s:
        parts = s.split("-")
        if len(parts) >= 2 and len(parts[0]) == 4:
            return (parts[0], f"{parts[0]}-{parts[1].zfill(2)}")
    elif "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            year = parts[2] if len(parts[2]) == 4 else f"20{parts[2]}"
            month = parts[0].zfill(2)
            return (year, f"{year}-{month}")
    return ("Unknown", "Unknown")


# -------------------------------------------------------------------
# SIMPLE ADMIN LOGIN
# -------------------------------------------------------------------
def safe_next_url(target):
    """Allows only same-site relative redirect targets."""
    if not target:
        return None
    target = target.strip()
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


@app.before_request
def require_admin_login():
    """Send visitors to the login page until they sign in."""
    if request.endpoint in ("static", "login"):
        return None
    if not session.get("admin_authenticated"):
        if request.method == "GET":
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return redirect(url_for("login"))
    return None


# -------------------------------------------------------------------
# LOGIN / LOGOUT ROUTES
# -------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_authenticated"):
        return redirect(url_for("dashboard"))

    next_url = safe_next_url(request.args.get("next") or request.form.get("next"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()

        admin_username, admin_password = get_admin_credentials()

        if username == admin_username and password == admin_password:
            session["admin_authenticated"] = True
            session["admin_username"] = admin_username
            log_activity("admin_login", f"Admin '{admin_username}' signed in.")
            flash("Welcome back! You are signed in.", "success")
            return redirect(next_url or url_for("dashboard"))

        flash("Invalid username or password.", "danger")

    return render_template("login.html", next_url=next_url or "")


@app.route("/logout", methods=["POST"])
def logout():
    admin_username, _ = get_admin_credentials()
    username = session.get("admin_username", admin_username or "admin")
    if session.get("admin_authenticated"):
        log_activity("admin_logout", f"Admin '{username}' signed out.")
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))


# -------------------------------------------------------------------
# IMPORT / EXPORT (CSV + GOOGLE SHEETS)
# -------------------------------------------------------------------
def dataset_records(data_type):
    if data_type == "payments":
        return list(db.payments.find().sort("date", -1))
    if data_type == "tenants":
        return list(db.tenants.find().sort([("room_no", 1), ("tenant_name", 1)]))
    if data_type == "expenses":
        return list(db.expenses.find().sort("date", -1))
    return []


def store_imported_rows(data_type, rows):
    """Validates rows, links tenants, and appends them to MongoDB."""
    docs, errors = dt.prepare_rows(data_type, rows)

    if data_type == "tenants":
        existing_rooms = {
            (t.get("room_no") or "").upper()
            for t in db.tenants.find({"status": "Active"})
        }
        accepted = []
        for doc in docs:
            room = (doc.get("room_no") or "").upper()
            if doc.get("status") == "Active" and room in existing_rooms:
                errors.append(f"Room {room} already has an active tenant - skipped.")
                continue
            if doc.get("status") == "Active":
                existing_rooms.add(room)
            accepted.append(doc)
        docs = accepted
        now = datetime.now()
        for doc in docs:
            doc["created_at"] = now
            doc["updated_at"] = now

    if data_type in ("payments", "expenses"):
        active_tenants = {
            (t.get("room_no") or "").upper(): t
            for t in db.tenants.find({"status": "Active"})
        }
        for doc in docs:
            tenant = active_tenants.get((doc.get("room_no") or "").upper())
            if tenant:
                doc["tenant_id"] = tenant["_id"]
                if not doc.get("tenant_name"):
                    doc["tenant_name"] = tenant.get("tenant_name") or ""

    if docs:
        collection = {
            "payments": db.payments,
            "tenants": db.tenants,
            "expenses": db.expenses,
        }[data_type]
        collection.insert_many(docs)

    return docs, errors


def flash_import_result(data_type, rows, docs, errors, source):
    label = dt.DATASETS[data_type]["label"].lower()
    skipped = max(0, len(rows) - len(docs) - len(errors))

    if docs:
        message = f"Imported {len(docs)} row(s) of {label} from {source}."
        if errors or skipped:
            message += f" Skipped {len(errors) + skipped} row(s)."
        flash(message, "success")
    elif rows:
        flash(f"No rows of {label} were imported from {source}.", "warning")
    else:
        flash(f"The {source} did not contain any rows.", "warning")

    for error in errors[:3]:
        flash(error, "warning")
    if len(errors) > 3:
        flash(f"...and {len(errors) - 3} more row issue(s).", "warning")

    log_activity(
        "data_imported",
        f"Imported {len(docs)} row(s) of {label} from {source}.",
        {
            "dataset": data_type,
            "source": source,
            "imported": len(docs),
            "issues": len(errors) + skipped,
        },
    )


def fetch_google_sheet_csv(url):
    """Downloads a shared or published Google Sheet as CSV."""
    csv_url = dt.to_csv_export_url(url)
    if not csv_url:
        raise ValueError(
            "Paste a valid Google Sheets link. For private sheets, publish it with "
            "File > Share > Publish to web (CSV)."
        )

    request_obj = Request(csv_url, headers={"User-Agent": "JFJ-Renthouse/3.2"})
    try:
        with urlopen(request_obj, timeout=25) as response:
            data = response.read(dt.MAX_IMPORT_BYTES + 1)
    except HTTPError as exc:
        raise ValueError(
            f"Google returned HTTP {exc.code}. Check that the sheet is shared for viewing."
        ) from exc
    except URLError as exc:
        raise ValueError(f"Could not reach Google Sheets: {exc.reason}") from exc

    if len(data) > dt.MAX_IMPORT_BYTES:
        raise ValueError("That sheet is too large to import (limit is 10 MB).")
    if b"<html" in data[:2000].lower():
        raise ValueError(
            "That link returned a web page instead of CSV. Publish the sheet with "
            "File > Share > Publish to web, choose CSV, and paste that link."
        )
    return data.decode("utf-8-sig", errors="replace")


def google_sheets_configured():
    if not (os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")):
        return False
    try:
        import gspread  # noqa: F401
    except ImportError:
        return False
    return True


def google_sheets_client():
    """Returns (gspread client, error message)."""
    try:
        import gspread
    except ImportError:
        return None, "gspread is not installed. Run: pip install gspread google-auth"

    raw_json = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    key_file = (os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()

    try:
        if raw_json:
            return gspread.service_account_from_dict(json.loads(raw_json)), None
        if key_file:
            path = Path(key_file)
            if not path.exists():
                return None, f"Google service account file not found: {key_file}"
            return gspread.service_account(filename=str(path)), None
    except Exception as exc:
        return None, f"Could not load Google credentials: {str(exc)[:200]}"

    return None, (
        "Direct Google Sheets sync is not set up. Add GOOGLE_SERVICE_ACCOUNT_FILE "
        "to .env, or use the CSV / published-link import below."
    )


def open_worksheet(client, url, tab_name, create_if_missing=False):
    import gspread

    key = dt.extract_spreadsheet_key(url)
    if not key:
        raise ValueError("Paste a valid Google Sheets URL or spreadsheet ID.")

    spreadsheet = client.open_by_key(key)
    tab_name = (tab_name or "").strip()
    if not tab_name:
        return spreadsheet.sheet1

    try:
        return spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        if create_if_missing:
            return spreadsheet.add_worksheet(title=tab_name, rows=2000, cols=30)
        raise ValueError(f"Tab '{tab_name}' was not found in that spreadsheet.")


@app.route("/data")
def data_tools():
    return render_template(
        "data.html",
        datasets=dt.DATASETS,
        google_ready=google_sheets_configured(),
    )


@app.route("/data/export/<data_type>")
def export_data_csv(data_type):
    dataset = dt.DATASETS.get(data_type)
    if not dataset:
        flash("Unknown data type.", "danger")
        return redirect(url_for("data_tools"))

    records = dataset_records(data_type)
    text = dt.csv_text(records, dataset["fields"])
    filename = f"jfj_{data_type}_{datetime.now().strftime('%Y-%m-%d')}.csv"

    log_activity(
        "data_exported",
        f"Exported {len(records)} {dataset['label'].lower()} record(s) to CSV.",
        {"dataset": data_type, "count": len(records), "format": "csv"},
    )

    return Response(
        text,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/data/import", methods=["POST"])
def import_data_csv():
    data_type = (request.form.get("data_type") or "").strip()
    if data_type not in dt.DATASETS:
        flash("Choose a valid data type.", "danger")
        return redirect(url_for("data_tools"))

    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        flash("Choose a CSV file to import.", "danger")
        return redirect(url_for("data_tools"))

    raw = uploaded.read(dt.MAX_IMPORT_BYTES + 1)
    if len(raw) > dt.MAX_IMPORT_BYTES:
        flash("That CSV is too large (limit is 10 MB).", "danger")
        return redirect(url_for("data_tools"))

    rows = dt.read_csv(raw.decode("utf-8-sig", errors="replace"))
    if len(rows) > dt.MAX_IMPORT_ROWS:
        flash(f"That CSV has {len(rows)} rows; the limit is {dt.MAX_IMPORT_ROWS}.", "danger")
        return redirect(url_for("data_tools"))

    try:
        docs, errors = store_imported_rows(data_type, rows)
    except Exception as exc:
        flash(f"Import failed: {str(exc)[:250]}", "danger")
        return redirect(url_for("data_tools"))

    flash_import_result(data_type, rows, docs, errors, f"CSV '{uploaded.filename}'")
    return redirect(url_for("data_tools"))


@app.route("/data/import-sheet", methods=["POST"])
def import_google_sheet():
    data_type = (request.form.get("data_type") or "").strip()
    url = (request.form.get("sheet_url") or "").strip()
    if data_type not in dt.DATASETS:
        flash("Choose a valid data type.", "danger")
        return redirect(url_for("data_tools"))

    try:
        text = fetch_google_sheet_csv(url)
        rows = dt.read_csv(text)
        if len(rows) > dt.MAX_IMPORT_ROWS:
            raise ValueError(f"That sheet has {len(rows)} rows; the limit is {dt.MAX_IMPORT_ROWS}.")
        docs, errors = store_imported_rows(data_type, rows)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("data_tools"))
    except Exception as exc:
        flash(f"Import failed: {str(exc)[:250]}", "danger")
        return redirect(url_for("data_tools"))

    flash_import_result(data_type, rows, docs, errors, "Google Sheet link")
    return redirect(url_for("data_tools"))


@app.route("/data/google-sync", methods=["POST"])
def google_sheet_sync():
    data_type = (request.form.get("data_type") or "").strip()
    action = (request.form.get("sync_action") or "import").strip()
    url = (request.form.get("sheet_url") or "").strip()
    tab_name = (request.form.get("tab_name") or "").strip()

    if data_type not in dt.DATASETS:
        flash("Choose a valid data type.", "danger")
        return redirect(url_for("data_tools"))

    client, error = google_sheets_client()
    if not client:
        flash(error, "danger")
        return redirect(url_for("data_tools"))

    dataset = dt.DATASETS[data_type]
    try:
        if action == "export":
            worksheet = open_worksheet(client, url, tab_name, create_if_missing=True)
            records = dataset_records(data_type)
            values = dt.table_rows(records, dataset["fields"])
            worksheet.clear()
            worksheet.update(values=values, range_name="A1", value_input_option="USER_ENTERED")
            log_activity(
                "data_exported",
                f"Exported {len(records)} {dataset['label'].lower()} record(s) to Google Sheets.",
                {"dataset": data_type, "count": len(records), "format": "google_sheets"},
            )
            flash(
                f"Exported {len(records)} {dataset['label'].lower()} record(s) to Google Sheets.",
                "success",
            )
        else:
            worksheet = open_worksheet(client, url, tab_name, create_if_missing=False)
            rows = worksheet.get_all_records()
            if len(rows) > dt.MAX_IMPORT_ROWS:
                raise ValueError(f"That sheet has {len(rows)} rows; the limit is {dt.MAX_IMPORT_ROWS}.")
            docs, errors = store_imported_rows(data_type, rows)
            flash_import_result(data_type, rows, docs, errors, "Google Sheets API")
    except ValueError as exc:
        flash(str(exc), "danger")
    except Exception as exc:
        flash(f"Google Sheets error: {str(exc)[:250]}", "danger")

    return redirect(url_for("data_tools"))


# -------------------------------------------------------------------
# DASHBOARD ROUTE (Read & Filter Payments)
# -------------------------------------------------------------------
@app.route("/")
def dashboard():
    # Get room filter from query string (e.g., /?room=2B)
    room_filter = request.args.get("room")
    query = {}
    if room_filter:
        query["room_no"] = room_filter

    # Fetch payments sorted by payment date (newest first)
    payments = list(db.payments.find(query).sort("date", -1))

    # Fetch distinct room numbers from database merged with building rooms
    recorded_rooms = db.payments.distinct("room_no")
    all_rooms = sorted(list(set(BUILDING_ROOMS + recorded_rooms)))

    # Calculate Summary Statistics using MongoDB Aggregation
    paid_pipeline = [{"$group": {"_id": None, "total": {"$sum": "$amount_paid"}}}]
    balance_pipeline = [{"$group": {"_id": None, "total": {"$sum": "$balance"}}}]
    due_pipeline = [{"$group": {"_id": None, "total": {"$sum": "$payment_due"}}}]

    paid_result = list(db.payments.aggregate(paid_pipeline))
    balance_result = list(db.payments.aggregate(balance_pipeline))
    due_result = list(db.payments.aggregate(due_pipeline))

    total_paid = paid_result[0]["total"] if paid_result else 0.0
    total_balance = balance_result[0]["total"] if balance_result else 0.0
    total_due = due_result[0]["total"] if due_result else 0.0

    # Total Operating Expenses
    expense_pipeline = [{"$group": {"_id": None, "total": {"$sum": "$amount"}}}]
    expense_result = list(db.expenses.aggregate(expense_pipeline))
    total_expenses = expense_result[0]["total"] if expense_result else 0.0

    net_profit = total_paid - total_expenses
    collection_rate = (total_paid / total_due * 100.0) if total_due > 0 else 0.0

    return render_template(
        "dashboard.html",
        payments=payments,
        rooms=all_rooms,
        total_paid=total_paid,
        total_balance=total_balance,
        total_due=total_due,
        total_expenses=total_expenses,
        net_profit=net_profit,
        collection_rate=round(collection_rate, 1),
        selected_room=room_filter,
        tenant_options=tenant_dropdown_options(),
    )


# -------------------------------------------------------------------
# ANALYTICS & FINANCIAL REPORTING ROUTE
# -------------------------------------------------------------------
@app.route("/analytics")
def analytics():
    all_payments = list(db.payments.find().sort("date", -1))
    all_expenses = list(db.expenses.find().sort("date", -1))

    total_transactions = len(all_payments)
    total_revenue = sum(p.get("amount_paid", 0.0) for p in all_payments)
    total_due = sum(p.get("payment_due", 0.0) for p in all_payments)
    total_balance = sum(p.get("balance", 0.0) for p in all_payments)
    total_expenses = sum(e.get("amount", 0.0) for e in all_expenses)
    net_profit = total_revenue - total_expenses

    collection_rate = (total_revenue / total_due * 100.0) if total_due > 0 else 0.0
    delinquency_rate = (total_balance / total_due * 100.0) if total_due > 0 else 0.0
    net_profit_margin = (net_profit / total_revenue * 100.0) if total_revenue > 0 else 0.0

    # Tenant-level statistics (Highest, Lowest, Average)
    tenant_paid = defaultdict(float)
    tenant_due = defaultdict(float)
    tenant_count = defaultdict(int)
    tenant_rooms = defaultdict(set)

    for p in all_payments:
        name = (p.get("tenant_name") or "Unknown").strip()
        tenant_paid[name] += p.get("amount_paid", 0.0)
        tenant_due[name] += p.get("payment_due", 0.0)
        tenant_count[name] += 1
        if p.get("room_no"):
            tenant_rooms[name].add(p.get("room_no"))

    tenant_summary_list = []
    for name, paid in tenant_paid.items():
        count = tenant_count[name]
        due = tenant_due[name]
        rooms_str = ", ".join(sorted(tenant_rooms[name]))
        tenant_summary_list.append({
            "name": name,
            "total_paid": paid,
            "total_due": due,
            "balance": max(0.0, due - paid),
            "count": count,
            "avg_payment": paid / count if count > 0 else 0.0,
            "rooms": rooms_str
        })

    # Sort tenants by total amount paid descending
    tenant_summary_list.sort(key=lambda x: x["total_paid"], reverse=True)

    highest_tenant = tenant_summary_list[0] if tenant_summary_list else None
    lowest_tenant = tenant_summary_list[-1] if tenant_summary_list else None
    avg_per_payment = (total_revenue / total_transactions) if total_transactions > 0 else 0.0
    avg_per_tenant = (total_revenue / len(tenant_summary_list)) if tenant_summary_list else 0.0

    # Yearly Profit & Revenue Aggregation (Determine Year with Most Profit)
    yearly_revenue = defaultdict(float)
    yearly_expenses = defaultdict(float)
    yearly_dues = defaultdict(float)

    for p in all_payments:
        year, _ = parse_date_components(p.get("date"))
        if year != "Unknown":
            yearly_revenue[year] += p.get("amount_paid", 0.0)
            yearly_dues[year] += p.get("payment_due", 0.0)

    for e in all_expenses:
        year, _ = parse_date_components(e.get("date"))
        if year != "Unknown":
            yearly_expenses[year] += e.get("amount", 0.0)

    all_years = sorted(set(yearly_revenue.keys()) | set(yearly_expenses.keys()))
    yearly_breakdown = []
    best_year = None
    best_year_profit = float("-inf")
    best_year_revenue = 0.0

    for y in all_years:
        rev = yearly_revenue[y]
        exp = yearly_expenses[y]
        due = yearly_dues[y]
        prof = rev - exp
        rate = (rev / due * 100.0) if due > 0 else 0.0
        yearly_breakdown.append({
            "year": y,
            "revenue": rev,
            "expenses": exp,
            "net_profit": prof,
            "collection_rate": round(rate, 1),
            "margin": round((prof / rev * 100.0), 1) if rev > 0 else 0.0
        })
        if prof > best_year_profit:
            best_year_profit = prof
            best_year = y
            best_year_revenue = rev

    # Sort yearly breakdown newest first
    yearly_breakdown.sort(key=lambda x: x["year"], reverse=True)

    # Monthly Trend Aggregation (Last 12 active months)
    monthly_rev = defaultdict(float)
    monthly_exp = defaultdict(float)

    for p in all_payments:
        _, ym = parse_date_components(p.get("date"))
        if ym != "Unknown":
            monthly_rev[ym] += p.get("amount_paid", 0.0)

    for e in all_expenses:
        _, ym = parse_date_components(e.get("date"))
        if ym != "Unknown":
            monthly_exp[ym] += e.get("amount", 0.0)

    all_months = sorted(set(monthly_rev.keys()) | set(monthly_exp.keys()))
    # Take up to last 12 months
    recent_months = all_months[-12:] if len(all_months) > 12 else all_months
    chart_months = recent_months
    chart_rev_values = [round(monthly_rev[m], 2) for m in recent_months]
    chart_exp_values = [round(monthly_exp[m], 2) for m in recent_months]
    chart_profit_values = [round(monthly_rev[m] - monthly_exp[m], 2) for m in recent_months]

    # Revenue by Room (Bar Chart data)
    room_rev = defaultdict(float)
    for p in all_payments:
        r = p.get("room_no") or "Unassigned"
        room_rev[r] += p.get("amount_paid", 0.0)

    # Sort rooms naturally
    all_building_rooms = sorted(list(set(BUILDING_ROOMS + list(room_rev.keys()))))
    chart_room_labels = all_building_rooms
    chart_room_values = [round(room_rev[r], 2) for r in all_building_rooms]

    # Payment Methods Breakdown (Pie/Doughnut Chart data)
    method_counts = defaultdict(int)
    method_rev = defaultdict(float)
    for p in all_payments:
        m = p.get("payment_method") or "Cash"
        method_counts[m] += 1
        method_rev[m] += p.get("amount_paid", 0.0)

    chart_method_labels = list(method_rev.keys()) if method_rev else ["Cash"]
    chart_method_values = [round(method_rev[m], 2) for m in chart_method_labels] if method_rev else [0.0]

    # Payment Categories Breakdown (Rent vs Utilities vs Maintenance)
    cat_rev = defaultdict(float)
    for p in all_payments:
        c = p.get("category") or "Rent"
        cat_rev[c] += p.get("amount_paid", 0.0)

    # Expense Categories Breakdown
    exp_cat = defaultdict(float)
    for e in all_expenses:
        c = e.get("category") or "General Repairs"
        exp_cat[c] += e.get("amount", 0.0)

    # Room Occupancy & Loss to Vacancy Tracking
    recorded_rooms = set(db.payments.distinct("room_no"))
    total_building_units = max(len(BUILDING_ROOMS), len(recorded_rooms))
    all_units = sorted(list(set(BUILDING_ROOMS + list(recorded_rooms))))

    # Consider occupied if room has at least one recorded payment
    occupied_rooms = sorted(list(recorded_rooms))
    vacant_rooms = sorted([r for r in all_units if r not in occupied_rooms])
    occupied_count = len(occupied_rooms)
    vacant_count = len(vacant_rooms)
    occupancy_rate = (occupied_count / total_building_units * 100.0) if total_building_units > 0 else 0.0

    avg_rent_benchmark = avg_per_payment if avg_per_payment > 0 else 8500.0
    estimated_vacancy_loss = vacant_count * avg_rent_benchmark

    return render_template(
        "analytics.html",
        # Core Financial KPIs
        total_transactions=total_transactions,
        total_revenue=total_revenue,
        total_due=total_due,
        total_balance=total_balance,
        total_expenses=total_expenses,
        net_profit=net_profit,
        collection_rate=round(collection_rate, 1),
        delinquency_rate=round(delinquency_rate, 1),
        net_profit_margin=round(net_profit_margin, 1),
        # Tenant Analytics
        highest_tenant=highest_tenant,
        lowest_tenant=lowest_tenant,
        avg_per_payment=round(avg_per_payment, 2),
        avg_per_tenant=round(avg_per_tenant, 2),
        tenant_summary_list=tenant_summary_list,
        # Yearly Profit Review
        best_year=best_year,
        best_year_profit=best_year_profit if best_year else 0.0,
        best_year_revenue=best_year_revenue,
        yearly_breakdown=yearly_breakdown,
        # Occupancy & Vacancy Tracking
        total_building_units=total_building_units,
        occupied_count=occupied_count,
        vacant_count=vacant_count,
        occupancy_rate=round(occupancy_rate, 1),
        occupied_rooms=occupied_rooms,
        vacant_rooms=vacant_rooms,
        estimated_vacancy_loss=round(estimated_vacancy_loss, 2),
        # Category breakdowns
        payment_categories=dict(cat_rev),
        expense_categories=dict(exp_cat),
        # Chart Data as JSON strings for Chart.js
        chart_months_json=json.dumps(chart_months),
        chart_rev_json=json.dumps(chart_rev_values),
        chart_exp_json=json.dumps(chart_exp_values),
        chart_profit_json=json.dumps(chart_profit_values),
        chart_room_labels_json=json.dumps(chart_room_labels),
        chart_room_values_json=json.dumps(chart_room_values),
        chart_method_labels_json=json.dumps(chart_method_labels),
        chart_method_values_json=json.dumps(chart_method_values),
        chart_cat_labels_json=json.dumps(list(cat_rev.keys())),
        chart_cat_values_json=json.dumps([round(v, 2) for v in cat_rev.values()])
    )


# -------------------------------------------------------------------
# TENANT RECORDS ROUTE (Add / Update / Move Out / Delete)
# -------------------------------------------------------------------
@app.route("/tenants")
def tenants():
    selected_room = (request.args.get("room") or "").strip().upper()

    query = {}
    if selected_room:
        query["room_no"] = selected_room

    tenant_list = list(
        db.tenants.find(query).sort([("status", 1), ("room_no", 1), ("tenant_name", 1)])
    )

    all_tenants = list(db.tenants.find())
    active_tenants = [t for t in all_tenants if (t.get("status") or "Active") == "Active"]
    moved_out_tenants = [t for t in all_tenants if t.get("status") == "Moved Out"]

    monthly_rent_roll = sum(t.get("monthly_rent", 0.0) for t in active_tenants)
    occupied_rooms = sorted({t.get("room_no") for t in active_tenants if t.get("room_no")})
    occupancy_rate = min(100.0, (len(occupied_rooms) / len(BUILDING_ROOMS) * 100.0)) if BUILDING_ROOMS else 0.0

    # Rooms available for the dropdown: known units + any historical room on record
    room_choices = sorted(set(BUILDING_ROOMS + [t.get("room_no") for t in all_tenants if t.get("room_no")]))

    # Attach payment / expense totals to each tenant so the connection is visible
    paid_by_id = defaultdict(float)
    paid_by_key = defaultdict(float)
    expense_by_id = defaultdict(float)
    expense_by_key = defaultdict(float)

    for p in db.payments.find():
        amount = float(p.get("amount_paid") or 0.0)
        if p.get("tenant_id"):
            paid_by_id[str(p["tenant_id"])] += amount
        key = ((p.get("tenant_name") or "").strip().lower(), (p.get("room_no") or "").strip().upper())
        paid_by_key[key] += amount

    for e in db.expenses.find():
        amount = float(e.get("amount") or 0.0)
        if e.get("tenant_id"):
            expense_by_id[str(e["tenant_id"])] += amount
        key = ((e.get("tenant_name") or "").strip().lower(), (e.get("room_no") or "").strip().upper())
        expense_by_key[key] += amount

    for t in tenant_list:
        tid = str(t.get("_id"))
        key = ((t.get("tenant_name") or "").strip().lower(), (t.get("room_no") or "").strip().upper())
        t["ledger_paid"] = paid_by_id.get(tid, paid_by_key.get(key, 0.0))
        t["ledger_expenses"] = expense_by_id.get(tid, expense_by_key.get(key, 0.0))

    return render_template(
        "tenants.html",
        tenants=tenant_list,
        rooms=room_choices,
        active_count=len(active_tenants),
        moved_out_count=len(moved_out_tenants),
        monthly_rent_roll=monthly_rent_roll,
        occupied_rooms=occupied_rooms,
        total_rooms=len(BUILDING_ROOMS),
        occupancy_rate=round(occupancy_rate, 1),
        selected_room=selected_room,
    )


@app.route("/add_tenant", methods=["POST"])
def add_tenant():
    try:
        data = tenant_form_payload(request.form)

        if not data["room_no"] or not data["tenant_name"]:
            flash("Room number and tenant name are required.", "danger")
            return redirect(request.referrer or url_for("tenants"))

        # Only one active tenant can hold a room at a time
        if data["status"] == "Active":
            existing = db.tenants.find_one({"room_no": data["room_no"], "status": "Active"})
            if existing:
                flash(
                    f"Room {data['room_no']} is already occupied by {existing.get('tenant_name')}. "
                    "Move that tenant out before adding a new one.",
                    "danger",
                )
                return redirect(request.referrer or url_for("tenants"))

        data["created_at"] = datetime.now()
        data["updated_at"] = datetime.now()
        db.tenants.insert_one(data)

        log_activity(
            "tenant_added",
            f"Added tenant {data['tenant_name']} to Room {data['room_no']} "
            f"(₱{data['monthly_rent']:,.2f}/month).",
            {"tenant": data["tenant_name"], "room_no": data["room_no"], "rent": data["monthly_rent"]},
        )
        flash(f"Tenant {data['tenant_name']} added to Room {data['room_no']} successfully!", "success")

    except Exception as e:
        flash(f"Error adding tenant: {str(e)}", "danger")

    return redirect(request.referrer or url_for("tenants"))


@app.route("/edit_tenant/<tenant_id>", methods=["POST"])
def edit_tenant(tenant_id):
    try:
        existing = db.tenants.find_one({"_id": ObjectId(tenant_id)})
        if not existing:
            flash("Tenant record not found.", "danger")
            return redirect(request.referrer or url_for("tenants"))

        data = tenant_form_payload(request.form)

        if not data["room_no"] or not data["tenant_name"]:
            flash("Room number and tenant name are required.", "danger")
            return redirect(request.referrer or url_for("tenants"))

        if data["status"] == "Active":
            clash = db.tenants.find_one({
                "room_no": data["room_no"],
                "status": "Active",
                "_id": {"$ne": ObjectId(tenant_id)},
            })
            if clash:
                flash(
                    f"Room {data['room_no']} already has an active tenant ({clash.get('tenant_name')}).",
                    "danger",
                )
                return redirect(request.referrer or url_for("tenants"))

        # If marked as moved out without an end date, stamp today
        if data["status"] == "Moved Out" and not data["move_out_date"]:
            data["move_out_date"] = datetime.now().strftime("%Y-%m-%d")

        data["updated_at"] = datetime.now()
        db.tenants.update_one({"_id": ObjectId(tenant_id)}, {"$set": data})

        moved_out_now = existing.get("status") == "Active" and data["status"] == "Moved Out"
        action = "tenant_moved_out" if moved_out_now else "tenant_updated"
        log_activity(
            action,
            f"Updated tenant {data['tenant_name']} (Room {data['room_no']}) — status: {data['status']}.",
            {"tenant": data["tenant_name"], "room_no": data["room_no"], "status": data["status"]},
        )
        flash(f"Tenant {data['tenant_name']} updated successfully!", "success")

    except Exception as e:
        flash(f"Error updating tenant: {str(e)}", "danger")

    return redirect(request.referrer or url_for("tenants"))


@app.route("/move_out_tenant/<tenant_id>", methods=["POST"])
def move_out_tenant(tenant_id):
    try:
        tenant = db.tenants.find_one({"_id": ObjectId(tenant_id)})
        if not tenant:
            flash("Tenant record not found.", "danger")
            return redirect(request.referrer or url_for("tenants"))

        move_out_date = (request.form.get("move_out_date") or "").strip()
        if not move_out_date:
            move_out_date = datetime.now().strftime("%Y-%m-%d")

        db.tenants.update_one(
            {"_id": ObjectId(tenant_id)},
            {"$set": {
                "status": "Moved Out",
                "move_out_date": move_out_date,
                "updated_at": datetime.now(),
            }},
        )

        log_activity(
            "tenant_moved_out",
            f"Moved out tenant {tenant.get('tenant_name')} from Room {tenant.get('room_no')} "
            f"(end date {move_out_date}).",
            {"tenant": tenant.get("tenant_name"), "room_no": tenant.get("room_no"), "move_out_date": move_out_date},
        )
        flash(
            f"{tenant.get('tenant_name')} has been moved out of Room {tenant.get('room_no')}.",
            "success",
        )

    except Exception as e:
        flash(f"Error moving tenant out: {str(e)}", "danger")

    return redirect(request.referrer or url_for("tenants"))


@app.route("/delete_tenant/<tenant_id>", methods=["POST"])
def delete_tenant(tenant_id):
    try:
        tenant = db.tenants.find_one({"_id": ObjectId(tenant_id)})
        if not tenant:
            flash("Tenant record not found.", "danger")
            return redirect(request.referrer or url_for("tenants"))

        db.tenants.delete_one({"_id": ObjectId(tenant_id)})
        log_activity(
            "tenant_deleted",
            f"Deleted tenant record for {tenant.get('tenant_name')} (Room {tenant.get('room_no')}).",
            {"tenant": tenant.get("tenant_name"), "room_no": tenant.get("room_no")},
        )
        flash(f"Tenant record for {tenant.get('tenant_name')} deleted successfully!", "success")

    except Exception as e:
        flash(f"Error deleting tenant: {str(e)}", "danger")

    return redirect(request.referrer or url_for("tenants"))


# -------------------------------------------------------------------
# ACTIVITY HISTORY ROUTE (Audit Trail)
# -------------------------------------------------------------------
@app.route("/history")
def history():
    action_filter = (request.args.get("action") or "all").strip()
    search = (request.args.get("q") or "").strip()
    room_filter = (request.args.get("room") or "").strip().upper()

    query = {}
    if action_filter == "create":
        query["category"] = "create"
    elif action_filter == "update":
        query["category"] = "update"
    elif action_filter == "delete":
        query["category"] = "delete"
    elif action_filter == "expense":
        query["entity"] = "Expense"
    elif action_filter not in ("", "all"):
        query["action"] = action_filter

    raw_activities = list(db.activities.find(query).sort("created_at", -1).limit(500))

    if search:
        needle = search.lower()
        raw_activities = [
            a for a in raw_activities
            if needle in (a.get("description") or "").lower()
            or needle in (a.get("entity") or "").lower()
            or needle in (a.get("action") or "").lower()
        ]

    if room_filter:
        raw_activities = [
            a for a in raw_activities
            if room_filter in (a.get("description") or "").upper()
        ]

    activities = [format_activity(a) for a in raw_activities]

    # Header statistics computed over the full audit trail
    all_activities = list(db.activities.find())
    today_str = datetime.now().strftime("%Y-%m-%d")
    stats = {
        "total": len(all_activities),
        "today": sum(
            1 for a in all_activities
            if isinstance(a.get("created_at"), datetime)
            and a["created_at"].strftime("%Y-%m-%d") == today_str
        ),
        "additions": sum(1 for a in all_activities if a.get("category") == "create"),
        "deletions": sum(1 for a in all_activities if a.get("category") == "delete"),
        "expenses": sum(1 for a in all_activities if a.get("entity") == "Expense"),
        "tenants": sum(1 for a in all_activities if a.get("entity") == "Tenant"),
    }

    return render_template(
        "history.html",
        activities=activities,
        stats=stats,
        action_filter=action_filter,
        search=search,
        room_filter=room_filter,
    )


# -------------------------------------------------------------------
# OPERATING EXPENSES ROUTE (Read & Manage Expenses)
# -------------------------------------------------------------------
@app.route("/expenses")
def expenses():
    selected_room = (request.args.get("room") or "").strip().upper()
    query = {}
    if selected_room:
        query["room_no"] = selected_room

    expenses_list = list(db.expenses.find(query).sort("date", -1))
    total_expenses = sum(e.get("amount", 0.0) for e in expenses_list)

    all_rooms = sorted(list(set(BUILDING_ROOMS + db.expenses.distinct("room_no") + db.tenants.distinct("room_no"))))

    # Category summaries
    cat_totals = defaultdict(float)
    for e in expenses_list:
        cat = e.get("category") or "General Maintenance"
        cat_totals[cat] += e.get("amount", 0.0)

    sorted_categories = sorted(
        [{"category": c, "total": amt} for c, amt in cat_totals.items()],
        key=lambda x: x["total"],
        reverse=True
    )

    return render_template(
        "expenses.html",
        expenses=expenses_list,
        total_expenses=total_expenses,
        categories=sorted_categories,
        rooms=all_rooms,
        tenant_options=tenant_dropdown_options(),
        selected_room=selected_room,
    )


# -------------------------------------------------------------------
# ADD PAYMENT RECORD ROUTE (Create)
# -------------------------------------------------------------------
@app.route("/add_payment", methods=["POST"])
def add_payment():
    try:
        tenant_id_raw = (request.form.get("tenant_id") or "").strip()
        room_no = (request.form.get("room_no") or "").strip().upper()
        tenant_name = (request.form.get("tenant_name") or "").strip()
        payment_due = float(request.form.get("payment_due") or 0)
        amount_paid = float(request.form.get("amount_paid") or 0)
        date = (request.form.get("date") or "").strip()
        payment_method = request.form.get("payment_method") or "Cash"
        category = request.form.get("category") or "Rent"
        notes = (request.form.get("notes") or "").strip()

        # Resolve linked tenant ID, room, and name
        tenant_oid, resolved_name, resolved_room = resolve_linked_tenant(tenant_id_raw)
        if not tenant_oid and room_no:
            matched = db.tenants.find_one({"room_no": room_no, "status": "Active"})
            if matched:
                tenant_oid = matched["_id"]
                if not tenant_name:
                    tenant_name = matched.get("tenant_name") or ""
        elif tenant_oid:
            if not tenant_name and resolved_name:
                tenant_name = resolved_name
            if not room_no and resolved_room:
                room_no = resolved_room

        # Automatically compute balance and status
        balance = max(0.0, payment_due - amount_paid)
        status = "Paid" if balance == 0 else "Pending"

        new_record = {
            "tenant_id": tenant_oid,
            "room_no": room_no,
            "tenant_name": tenant_name,
            "payment_due": payment_due,
            "amount_paid": amount_paid,
            "balance": balance,
            "date": date,
            "status": status,
            "payment_method": payment_method,
            "category": category,
            "notes": notes
        }

        # Insert record into MongoDB Atlas 'payments' collection
        db.payments.insert_one(new_record)

        log_activity(
            "payment_added",
            f"Recorded ₱{amount_paid:,.2f} payment for {tenant_name} (Room {room_no}) — "
            f"₱{balance:,.2f} balance remaining.",
            {
                "tenant_id": str(tenant_oid) if tenant_oid else None,
                "room_no": room_no,
                "tenant": tenant_name,
                "amount_paid": amount_paid,
                "balance": balance,
                "method": payment_method,
                "category": category,
            },
        )

        flash(f"Payment record for Room {room_no} ({tenant_name}) successfully added!", "success")

    except Exception as e:
        flash(f"Error adding payment: {str(e)}", "danger")

    return redirect(request.referrer or url_for("dashboard"))


# -------------------------------------------------------------------
# EDIT PAYMENT RECORD ROUTE (Update)
# -------------------------------------------------------------------
@app.route("/edit_payment/<payment_id>", methods=["POST"])
def edit_payment(payment_id):
    try:
        tenant_id_raw = (request.form.get("tenant_id") or "").strip()
        room_no = (request.form.get("room_no") or "").strip().upper()
        tenant_name = (request.form.get("tenant_name") or "").strip()
        payment_due = float(request.form.get("payment_due") or 0)
        amount_paid = float(request.form.get("amount_paid") or 0)
        date = (request.form.get("date") or "").strip()
        payment_method = request.form.get("payment_method") or "Cash"
        category = request.form.get("category") or "Rent"
        notes = (request.form.get("notes") or "").strip()

        # Resolve linked tenant ID, room, and name
        tenant_oid, resolved_name, resolved_room = resolve_linked_tenant(tenant_id_raw)
        if not tenant_oid and room_no:
            matched = db.tenants.find_one({"room_no": room_no, "status": "Active"})
            if matched:
                tenant_oid = matched["_id"]
                if not tenant_name:
                    tenant_name = matched.get("tenant_name") or ""
        elif tenant_oid:
            if not tenant_name and resolved_name:
                tenant_name = resolved_name
            if not room_no and resolved_room:
                room_no = resolved_room

        balance = max(0.0, payment_due - amount_paid)
        status = "Paid" if balance == 0 else "Pending"

        updated_fields = {
            "tenant_id": tenant_oid,
            "room_no": room_no,
            "tenant_name": tenant_name,
            "payment_due": payment_due,
            "amount_paid": amount_paid,
            "balance": balance,
            "date": date,
            "status": status,
            "payment_method": payment_method,
            "category": category,
            "notes": notes
        }

        db.payments.update_one({"_id": ObjectId(payment_id)}, {"$set": updated_fields})

        log_activity(
            "payment_updated",
            f"Updated payment for {tenant_name} (Room {room_no}) — "
            f"paid ₱{amount_paid:,.2f}, balance ₱{balance:,.2f}.",
            {
                "tenant_id": str(tenant_oid) if tenant_oid else None,
                "room_no": room_no,
                "tenant": tenant_name,
                "amount_paid": amount_paid,
                "balance": balance,
                "method": payment_method,
                "category": category,
            },
        )

        flash(f"Payment record for Room {room_no} ({tenant_name}) updated successfully!", "success")

    except Exception as e:
        flash(f"Error updating payment: {str(e)}", "danger")

    return redirect(request.referrer or url_for("dashboard"))


# -------------------------------------------------------------------
# DELETE PAYMENT RECORD ROUTE (Delete)
# -------------------------------------------------------------------
@app.route("/delete_payment/<payment_id>", methods=["POST"])
def delete_payment(payment_id):
    try:
        payment = db.payments.find_one({"_id": ObjectId(payment_id)})
        room_info = f"Room {payment.get('room_no')} - {payment.get('tenant_name')}" if payment else "Record"
        db.payments.delete_one({"_id": ObjectId(payment_id)})

        log_activity(
            "payment_deleted",
            f"Deleted payment record for {room_info}.",
            {
                "room_no": payment.get("room_no") if payment else "",
                "tenant": payment.get("tenant_name") if payment else "",
                "amount_paid": payment.get("amount_paid") if payment else 0,
            },
        )

        flash(f"{room_info} successfully deleted!", "success")
    except Exception as e:
        flash(f"Error deleting record: {str(e)}", "danger")

    return redirect(request.referrer or url_for("dashboard"))


# -------------------------------------------------------------------
# ADD OPERATING EXPENSE ROUTE (Create Expense)
# -------------------------------------------------------------------
@app.route("/add_expense", methods=["POST"])
def add_expense():
    try:
        title = (request.form.get("title") or "").strip()
        amount = float(request.form.get("amount") or 0)
        category = request.form.get("category") or "General Maintenance"
        date = (request.form.get("date") or "").strip()
        notes = (request.form.get("notes") or "").strip()

        # Connected room & tenant association
        tenant_target = (request.form.get("tenant_target") or request.form.get("tenant_id") or "").strip()
        room_no = (request.form.get("room_no") or "").strip().upper()
        tenant_name = (request.form.get("tenant_name") or "").strip()
        tenant_oid = None

        if tenant_target:
            if tenant_target.startswith("room:"):
                room_no = tenant_target.split(":", 1)[1].strip().upper()
                matched = db.tenants.find_one({"room_no": room_no, "status": "Active"})
                if matched:
                    tenant_oid = matched["_id"]
                    tenant_name = matched.get("tenant_name") or ""
            else:
                tenant_oid, resolved_name, resolved_room = resolve_linked_tenant(tenant_target)
                if tenant_oid:
                    tenant_name = resolved_name
                    room_no = resolved_room
        elif room_no:
            matched = db.tenants.find_one({"room_no": room_no, "status": "Active"})
            if matched:
                tenant_oid = matched["_id"]
                if not tenant_name:
                    tenant_name = matched.get("tenant_name") or ""

        new_expense = {
            "title": title,
            "amount": amount,
            "category": category,
            "date": date,
            "room_no": room_no,
            "tenant_name": tenant_name,
            "tenant_id": tenant_oid,
            "notes": notes
        }

        db.expenses.insert_one(new_expense)

        unit_str = f" for Room {room_no}" if room_no else " (Building-Wide)"
        if tenant_name:
            unit_str += f" · {tenant_name}"

        log_activity(
            "expense_added",
            f"Recorded expense '{title}' (₱{amount:,.2f}) under {category}{unit_str}.",
            {
                "title": title,
                "amount": amount,
                "category": category,
                "date": date,
                "room_no": room_no,
                "tenant": tenant_name,
                "tenant_id": str(tenant_oid) if tenant_oid else None,
            },
        )

        flash(f"Operating expense '{title}' (₱{amount:,.2f}) successfully recorded!", "success")

    except Exception as e:
        flash(f"Error recording expense: {str(e)}", "danger")

    return redirect(request.referrer or url_for("expenses"))


# -------------------------------------------------------------------
# DELETE OPERATING EXPENSE ROUTE (Delete Expense)
# -------------------------------------------------------------------
@app.route("/delete_expense/<expense_id>", methods=["POST"])
def delete_expense(expense_id):
    try:
        exp = db.expenses.find_one({"_id": ObjectId(expense_id)})
        title_info = exp.get("title", "Expense") if exp else "Expense"
        db.expenses.delete_one({"_id": ObjectId(expense_id)})

        log_activity(
            "expense_deleted",
            f"Deleted expense '{title_info}'"
            + (f" (₱{exp.get('amount', 0):,.2f})." if exp else "."),
            {
                "title": title_info,
                "amount": exp.get("amount") if exp else 0,
                "category": exp.get("category") if exp else "",
            },
        )

        flash(f"Expense '{title_info}' deleted successfully!", "success")
    except Exception as e:
        flash(f"Error deleting expense: {str(e)}", "danger")

    return redirect(request.referrer or url_for("expenses"))


# -------------------------------------------------------------------
# START FLASK SERVER
# -------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    # Never ship the interactive debugger to production: it can execute code.
    app.run(host=os.getenv("HOST", "0.0.0.0"), port=port, debug=debug)