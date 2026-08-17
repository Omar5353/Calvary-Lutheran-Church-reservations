"""
Copy reservations from the local SQLite file into Postgres, once.

Use this if you already have bookings in reservations.db and are moving to
Supabase. Safe to re-run: rows whose date and name already exist in Postgres
are skipped rather than duplicated.

    POSTGRES_URL="postgresql://..." python3 migrate_to_postgres.py
    POSTGRES_URL="postgresql://..." python3 migrate_to_postgres.py --dry-run
"""
import os
import sqlite3
import sys

SQLITE_PATH = os.environ.get(
    "CALVARY_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "reservations.db"),
)
DRY_RUN = "--dry-run" in sys.argv

url = os.environ.get("POSTGRES_URL")
if not url:
    sys.exit("Set POSTGRES_URL first, e.g.\n  POSTGRES_URL='postgresql://...' python3 migrate_to_postgres.py")

if not os.path.exists(SQLITE_PATH):
    sys.exit(f"No SQLite database found at {SQLITE_PATH}. Nothing to migrate.")

import app  # noqa: E402  (imported after POSTGRES_URL is set)

if not app.using_postgres():
    sys.exit("POSTGRES_URL is set but the app did not accept it. Check for a leftover [YOUR-PASSWORD] placeholder.")

app.init_db()  # make sure the Postgres table exists

src = sqlite3.connect(SQLITE_PATH)
src.row_factory = sqlite3.Row
cols = {r[1] for r in src.execute("PRAGMA table_info(reservations)")}
rows = src.execute("SELECT * FROM reservations ORDER BY id").fetchall()
src.close()

print(f"Found {len(rows)} reservation(s) in {SQLITE_PATH}\n")

copied = skipped = 0
with app.Db() as db:
    for r in rows:
        exists = db.scalar(
            "SELECT COUNT(*) FROM reservations WHERE event_date = ? AND name = ?",
            (r["event_date"], r["name"]),
        )
        label = f"{r['event_date']}  {r['name'][:28]:<28} {r['status']}"
        if exists:
            print(f"  skip   {label}  (already in Postgres)")
            skipped += 1
            continue
        if DRY_RUN:
            print(f"  would copy  {label}")
            copied += 1
            continue
        db.execute(
            """
            INSERT INTO reservations
                (event_date, day_name, slot_label, name, email, phone, purpose,
                 num_people, comments, status, submitted_at, admin_note, notify_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["event_date"], r["day_name"], r["slot_label"], r["name"],
                r["email"], r["phone"], r["purpose"], r["num_people"],
                r["comments"], r["status"], r["submitted_at"],
                r["admin_note"] if "admin_note" in cols else None,
                r["notify_status"] if "notify_status" in cols else None,
            ),
        )
        print(f"  copy   {label}")
        copied += 1

verb = "would be copied" if DRY_RUN else "copied"
print(f"\n{copied} {verb}, {skipped} skipped.")
if DRY_RUN:
    print("Dry run, nothing was written. Re-run without --dry-run to apply.")
else:
    print(f"Postgres now holds {len(app.all_reservations())} reservation(s).")
