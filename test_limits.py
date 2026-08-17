import os, tempfile, datetime as dt, sys
os.environ["CALVARY_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.pop("GMAIL_APP_PASSWORD", None)
import app
app.init_db()

# Start from a clean table. With SQLite each run gets its own temp file, but a
# shared Postgres keeps rows between test files, so clear it explicitly.
with app.Db() as _db:
    _db.execute("DELETE FROM reservations")

def nw(wd, weeks=0):
    d = dt.date.today() + dt.timedelta(days=1)
    while d.weekday() != wd: d += dt.timedelta(days=1)
    return d + dt.timedelta(weeks=weeks)

def check(l, c):
    print(("PASS  " if c else "FAIL  ") + l); assert c, l

# find three bookable dates inside ONE calendar month
base = dt.date.today()
cands = []
probe = base + dt.timedelta(days=1)
target_month = None
for m_off in range(0, 7):
    y = base.year + (base.month - 1 + m_off)//12
    mo = (base.month - 1 + m_off) % 12 + 1
    import calendar
    days = [dt.date(y,mo,dd) for dd in range(1, calendar.monthrange(y,mo)[1]+1)]
    days = [d for d in days if d.weekday() in app.SLOTS and d > base]
    if len(days) >= 4:
        target_month, cands = (y,mo), days[:4]; break
print("Testing month:", target_month, "candidates:", [str(c) for c in cands])

ok1,_ = app.create_request(cands[0], "A", "a@x.org", "", "P1", 10, "")
ok2,_ = app.create_request(cands[1], "B", "b@x.org", "", "P2", 10, "")
check("first two accepted", ok1 and ok2)
check("count is 2", app.active_count_in_month(cands[0]) == 2)
check("month reports full", app.month_is_full(cands[0]))

ok3, msg3 = app.create_request(cands[2], "C", "c@x.org", "", "P3", 10, "")
check("third rejected", not ok3)
check("rejection names the limit", "limit for one month" in msg3)
print("     msg:", msg3)

# decline one -> a slot frees up
rid = int(app.all_reservations()["id"].iloc[0])
app.set_status(rid, app.STATUS_DECLINED, "conflict")
check("count back to 1", app.active_count_in_month(cands[0]) == 1)
check("month no longer full", not app.month_is_full(cands[0]))
ok4,_ = app.create_request(cands[2], "C", "c@x.org", "", "P3", 10, "")
check("third accepted after decline", ok4)
check("count is 2 again", app.active_count_in_month(cands[0]) == 2)

# re-approving the declined one must not exceed the cap
ok5, msg5 = app.set_status(rid, app.STATUS_RESERVED)
check("re-approve blocked at cap", not ok5)
check("re-approve message explains", "already has 2 active" in msg5)
print("     msg:", msg5)

# next month is unaffected
nxt = None
import calendar
y2 = target_month[0] + (target_month[1])//12
m2 = target_month[1] % 12 + 1
for dd in range(1, calendar.monthrange(y2,m2)[1]+1):
    d = dt.date(y2,m2,dd)
    if d.weekday() in app.SLOTS: nxt = d; break
ok6,_ = app.create_request(nxt, "D", "d@x.org", "", "P4", 10, "")
check("next month still open", ok6)

# notification is skipped without a password, and does not break the booking
row = app.get_reservation(int(app.all_reservations()["id"].iloc[-1]))
check("notify recorded as not configured", row["notify_status"] == "not configured")
# ---- month_counts must agree with the per-month count it replaced ----
print()
import calendar as _cal
probe = dt.date.today().replace(day=1)
end = (probe + dt.timedelta(days=31*7))
end = end.replace(day=_cal.monthrange(end.year, end.month)[1])
counts = app.month_counts(probe, end)
agree = True
d = probe
seen = set()
while d <= end:
    ym = (d.year, d.month)
    if ym not in seen:
        seen.add(ym)
        if counts.get(ym, 0) != app.active_count_in_month(d):
            agree = False
            print(f"     mismatch for {ym}: batched={counts.get(ym,0)} per-month={app.active_count_in_month(d)}")
    d += dt.timedelta(days=28)
check("batched month counts match per-month counts", agree)
check("months with no bookings are absent, not zero-filled",
      all(v > 0 for v in counts.values()))

print("\nAll checks passed.")
