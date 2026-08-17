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

# Fill the month to one slot short of the cap.
for i in range(app.MAX_PER_MONTH - 1):
    ok, _ = app.create_request(days[i], f"Early {i}", "e@x.org", "", "P", 5, "")
    check(f"seed booking {i+1} accepted", ok)

start = threading.Barrier(6)
results, errors = [], []

def racer(day, who):
    try:
        start.wait()                      # all six fire at the same instant
        results.append(app.create_request(day, who, "r@x.org", "", "P", 5, "")[0])
    except Exception as exc:
        errors.append(f"{who}: {type(exc).__name__}: {exc}")

threads = [threading.Thread(target=racer, args=(days[app.MAX_PER_MONTH - 1 + i], f"Racer{i}"))
           for i in range(6)]
for t in threads: t.start()
for t in threads: t.join()

print(f"     six simultaneous submissions -> {sum(results)} accepted, {len(results)-sum(results)} refused")
check("no exceptions leaked to the user", not errors)
check("exactly one racer won the last slot", sum(results) == 1)
check("month holds exactly the cap", app.active_count_in_month(days[0]) == app.MAX_PER_MONTH)
check("no double booking on any date",
      len(app.all_reservations()) == len(set(app.all_reservations()["event_date"])))

# Declining frees exactly one slot, and only one new booking can take it.
first_id = int(app.all_reservations()["id"].iloc[0])
app.set_status(first_id, app.STATUS_DECLINED, "freeing a slot")
check("slot freed by declining", app.active_count_in_month(days[0]) == app.MAX_PER_MONTH - 1)

results.clear()
start = threading.Barrier(4)
threads = [threading.Thread(target=racer, args=(days[app.MAX_PER_MONTH + 1 + i], f"Second{i}"))
           for i in range(4)]
for t in threads: t.start()
for t in threads: t.join()
check("exactly one took the freed slot", sum(results) == 1)
check("back at the cap", app.active_count_in_month(days[0]) == app.MAX_PER_MONTH)

print(f"\nAll concurrency checks passed on {backend}.")
