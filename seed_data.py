"""
Utility script to import records from 'JFJ Buiilding Profits - Raw_Data_Sets.csv'
into MongoDB Atlas 'payments' collection, and seed initial operating expenses.
Run: python seed_data.py
"""

import os
import csv
from datetime import datetime
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")

if not MONGO_URI:
    raise ValueError("MONGO_URI is missing in .env")

client = MongoClient(MONGO_URI)
db = client.get_database("jfj_renthouse")

csv_path = "JFJ Buiilding Profits - Raw_Data_Sets.csv"

def clean_float(value):
    if not value or str(value).strip() == "":
        return 0.0
    val_str = str(value).replace(",", "").strip()
    try:
        return float(val_str)
    except ValueError:
        return 0.0

def import_csv():
    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        return

    records = []
    # Payment methods rotation for realistic variety
    methods_cycle = ["Cash", "GCash", "Maya", "Bank Transfer"]

    with open(csv_path, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        idx = 0
        for row in reader:
            room = (row.get("Room No.") or "").strip().upper()
            tenant = (row.get("Tenant Name") or "").strip()
            
            # Skip empty rows
            if not room and not tenant:
                continue

            due = clean_float(row.get("Payment Due"))
            paid = clean_float(row.get("Amount Paid"))
            balance = max(0.0, due - paid)
            status = "Paid" if balance == 0 else "Pending"
            date = (row.get("Date") or "").strip()
            
            # Normalize date if present e.g. 2/14/2026 -> 2026-02-14
            if date:
                for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
                    try:
                        d = datetime.strptime(date, fmt)
                        date = d.strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        pass
            else:
                date = "2026-04-15"

            record = {
                "room_no": room,
                "tenant_name": tenant,
                "payment_due": due,
                "amount_paid": paid,
                "balance": balance,
                "date": date,
                "status": status,
                "payment_method": methods_cycle[idx % len(methods_cycle)],
                "category": "Rent",
                "notes": "Historical ledger import"
            }
            records.append(record)
            idx += 1

    if records:
        # Check existing count
        existing = db.payments.count_documents({})
        if existing == 0:
            result = db.payments.insert_many(records)
            print(f"Successfully imported {len(result.inserted_ids)} payments into MongoDB Atlas!")
        else:
            print(f"Payments collection already has {existing} records. Skipping to prevent duplicate seeding.")

    # Seed sample operating expenses if empty to enable Net Profit analytics
    if db.expenses.count_documents({}) == 0:
        sample_expenses = [
            {"title": "Water Pump Maintenance", "amount": 2500.0, "category": "Maintenance & Repairs", "date": "2026-02-15", "notes": "Plumbing service"},
            {"title": "Common Area Hallway Electricity", "amount": 1850.0, "category": "Utilities", "date": "2026-02-28", "notes": "Meralco bill"},
            {"title": "Deep Cleaning & Disinfection", "amount": 1200.0, "category": "Cleaning & Supplies", "date": "2026-03-10", "notes": "Quarterly cleaning"},
            {"title": "Roof & Gutter Waterproofing", "amount": 4500.0, "category": "Maintenance & Repairs", "date": "2026-04-05", "notes": "Pre-monsoon maintenance"},
            {"title": "Barangay Business Clearance & Permits", "amount": 2200.0, "category": "Taxes & Permits", "date": "2026-01-20", "notes": "Annual permit"},
        ]
        db.expenses.insert_many(sample_expenses)
        print("Seeded 5 sample operating expenses for Net Profit calculation.")

if __name__ == "__main__":
    import_csv()
