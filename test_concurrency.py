"""
Race the monthly cap on whichever backend is configured.

Run against SQLite:    python3 test_concurrency.py
Run against Postgres:  POSTGRES_URL=postgresql://... python3 test_concurrency.py
"""
import os, tempfile, calendar, datetime as dt, threading

if not os.environ.get("POSTGRES_URL"):
    os.environ["CALVARY_DB"] = os.path.join(tempfile.mkdtemp(), "race.db")
import app

def check(l, c):
    print(("PASS  " if c else "FAIL  ") + l); assert c, l

app.init_db()
with app.Db() as db:                      # start from a clean table
    db.execute("DELETE FROM reservations")

backend = "postgres" if app.using_postgres() else "sqlite"
print(f"backend: {backend}, cap: {app.MAX_PER_MONTH} per month\n")

# Collect enough bookable dates inside one calendar month.
base = dt.date.today()
y, m = base.year, base.month
def days_in(y, m):
    return [dt.date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)
            if dt.date(y, m, d).weekday() in app.SLOTS and dt.date(y, m, d) >= base]
days = days_in(y, m)
while len(days) < 7:
    m = m % 12 + 1
    if m == 1: y += 1
    days = days_in(y, m)

# Fill the month to one slot short of the cap. Only APPROVED bookings count
# toward the limit, so the seeds have to be approved, not merely requested.
for i in range(app.MAX_PER_MONTH - 1):
    ok, _ = app.create_request(days[i], f"Early {i}", "e@x.org", "", "P", 5, "")
    check(f"seed booking {i+1} accepted", ok)
    rid = int(app.all_reservations()[app.all_reservations()["name"] == f"Early {i}"]["id"].iloc[0])
    ok, _ = app.set_status(rid, app.STATUS_RESERVED)
    check(f"seed booking {i+1} approved", ok)

start = threading.Barrier(6)
results, errors = [], []

def racer(day, who):
    """Submit a request. With the new rules these should all be accepted."""
    try:
        start.wait()
        results.append(app.create_request(day, who, "r@x.org", "", "P", 5, "")[0])
    except Exception as exc:
        errors.append(f"{who}: {type(exc).__name__}: {exc}")

threads = [threading.Thread(target=racer, args=(days[app.MAX_PER_MONTH - 1 + i], f"Racer{i}"))
           for i in range(6)]
for t in threads: t.start()
for t in threads: t.join()

print(f"     six simultaneous requests -> {sum(results)} accepted, {len(results)-sum(results)} refused")
check("no exceptions leaked to the user", not errors)
check("pending requests do not consume places, so all are accepted", sum(results) == 6)
check("still only the seeded approvals count",
      app.reserved_count_in_month(days[0]) == app.MAX_PER_MONTH - 1)
check("no double booking on any date",
      len(app.all_reservations()) == len(set(app.all_reservations()["event_date"])))

# ---- the real contention now: several people approving at the same instant
print("\n--- six simultaneous APPROVALS for one remaining place ---")
pending = app.all_reservations()
pending = pending[pending["status"] == app.STATUS_PENDING]["id"].tolist()[:6]
approved, approve_errors = [], []
gate = threading.Barrier(len(pending))

def approver(rid):
    try:
        gate.wait()
        approved.append(app.set_status(int(rid), app.STATUS_RESERVED)[0])
    except Exception as exc:
        approve_errors.append(f"{rid}: {type(exc).__name__}: {exc}")

ts = [threading.Thread(target=approver, args=(r,)) for r in pending]
for t in ts: t.start()
for t in ts: t.join()

print(f"     {len(pending)} simultaneous approvals -> {sum(approved)} succeeded, "
      f"{len(approved)-sum(approved)} refused")
check("no exceptions during concurrent approvals", not approve_errors)
check("exactly one approval won the last place", sum(approved) == 1)
check("month sits exactly at the limit, never over",
      app.reserved_count_in_month(days[0]) == app.MAX_PER_MONTH)

# ---- declining an approval frees exactly one place
print("\n--- declining frees one place ---")
first_approved = app.all_reservations()
first_approved = first_approved[first_approved["status"] == app.STATUS_RESERVED]["id"].iloc[0]
app.set_status(int(first_approved), app.STATUS_DECLINED, "freeing a place")
check("one place freed", app.reserved_count_in_month(days[0]) == app.MAX_PER_MONTH - 1)

approved.clear()
left = app.all_reservations()
left = left[left["status"] == app.STATUS_PENDING]["id"].tolist()[:4]
gate = threading.Barrier(len(left))
ts = [threading.Thread(target=approver, args=(r,)) for r in left]
for t in ts: t.start()
for t in ts: t.join()
check("exactly one took the freed place", sum(approved) == 1)
check("back at the limit", app.reserved_count_in_month(days[0]) == app.MAX_PER_MONTH)

print(f"\nAll concurrency checks passed on {backend}.")
