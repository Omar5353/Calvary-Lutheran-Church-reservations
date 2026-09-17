"""
The rule Omar specified.

A pending request holds a place. Two pending requests in one month close it
to a third. Only a decline reopens it, whether the thing declined was pending
or already approved.
"""
import json, os, tempfile, calendar as cal, datetime as dt

os.environ.setdefault("CALVARY_DB", os.path.join(tempfile.mkdtemp(), "flow.db"))
os.environ.update(SMTP_HOST="127.0.0.1", SMTP_PORT="8025", SMTP_SSL="0",
                  GMAIL_APP_PASSWORD="x", APP_BASE_URL="http://localhost:8501",
                  OFFICE_EMAIL="office-trial@example.org", NOTIFY_EMAIL="organiser@example.org")
MAIL = os.environ.get("MAILBOX", "/tmp/mail.json")


def _ensure_catcher(host="127.0.0.1", port=8025):
    import socket, subprocess, sys, time
    s = socket.socket(); s.settimeout(1)
    try:
        s.connect((host, port)); s.close(); return None
    except Exception:
        pass
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mailcatcher.py"),
         str(port), MAIL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            s = socket.socket(); s.settimeout(1); s.connect((host, port)); s.close(); return proc
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("no mailcatcher")


_CATCHER = _ensure_catcher()
import atexit
atexit.register(lambda: _CATCHER and _CATCHER.terminate())

import app  # noqa: E402

app.init_db()
with app.Db() as db:
    db.execute("DELETE FROM reservations")


def check(l, c):
    print(("PASS  " if c else "FAIL  ") + l); assert c, l


def mail(expect=None, timeout=6.0):
    import time
    end = time.time() + timeout
    while True:
        try:
            box = json.load(open(MAIL))
        except Exception:
            box = []
        if expect is None or len(box) >= expect or time.time() > end:
            return box
        time.sleep(0.1)


def clear():
    json.dump([], open(MAIL, "w"))


def to(box, addr):
    return [m for m in box if any(addr in t for t in m["to"])]


base = dt.date.today()
y, m = base.year, base.month
def days(y, m):
    return [dt.date(y, m, d) for d in range(1, cal.monthrange(y, m)[1] + 1)
            if dt.date(y, m, d).weekday() in app.SLOTS and dt.date(y, m, d) > base]
D = days(y, m)
while len(D) < 5:
    m = m % 12 + 1
    if m == 1: y += 1
    D = days(y, m)
print(f"working in {D[0].strftime('%B %Y')}, limit is {app.MAX_PER_MONTH} places\n")

ANA, BEN, CARA = "ana@example.org", "ben@example.org", "cara@example.org"

# ------------------------------------------------- two pending close a month
print("--- first requester ---")
clear()
ok, _ = app.create_request(D[0], "Ana Diaz", ANA, "402-555-0001", "Baptism reception", 40, "")
check("Ana's request accepted", ok)
check("one place taken", app.active_count_in_month(D[0]) == 1)
check("month not full yet", not app.month_is_full(D[0]))
check("Ana was acknowledged", len(to(mail(expect=3), ANA)) == 1)

print("\n--- second requester, same month ---")
ok, _ = app.create_request(D[1], "Ben Carter", BEN, "", "Youth night", 25, "")
check("Ben's request accepted", ok)
check("both places taken while both are pending", app.active_count_in_month(D[0]) == 2)
check("nothing approved yet", app.reserved_count_in_month(D[0]) == 0)
check("month IS full on two pending requests", app.month_is_full(D[0]))

print("\n--- third requester, same month ---")
ok3, msg3 = app.create_request(D[2], "Cara Lin", CARA, "", "Recital", 50, "")
check("the third request is REFUSED", not ok3)
check("the refusal explains pending requests count",
      "requested or approved" in msg3 and "limit for one month" in msg3)
print(f"     {msg3}")
check("no row was created for Cara", len(app.all_reservations()[app.all_reservations()["name"] == "Cara Lin"]) == 0)

sm = app.status_map(D[0], D[4])
check("Ana's date reads Pending", sm[D[0].isoformat()]["status"] == "Pending")
check("Ben's date reads Pending", sm[D[1].isoformat()]["status"] == "Pending")

counts = app.month_counts(D[0].replace(day=1), D[-1])
c = counts[(D[0].year, D[0].month)]
check("month_counts shows two pending, none approved", c == {"reserved": 0, "pending": 2})

# ----------------------------------------- only a decline reopens the month
print("\n--- the office APPROVES one, which does not free anything ---")
rid_ana = int(app.all_reservations()[app.all_reservations()["name"] == "Ana Diaz"]["id"].iloc[0])
clear()
ok, _ = app.decide(rid_ana, True)
check("Ana approved", ok)
check("still two places taken, one approved one pending", app.active_count_in_month(D[0]) == 2)
check("month still full", app.month_is_full(D[0]))
ok, _ = app.create_request(D[2], "Cara Lin", CARA, "", "Recital", 50, "")
check("Cara still cannot get in", not ok)

print("\n--- the office DECLINES the other, which frees a place ---")
rid_ben = int(app.all_reservations()[app.all_reservations()["name"] == "Ben Carter"]["id"].iloc[0])
clear()
ok, _ = app.decide(rid_ben, False)
check("Ben declined", ok)
check("one place free again", app.active_count_in_month(D[0]) == 1)
check("month is open again", not app.month_is_full(D[0]))

box = mail(expect=2)
check("Ben was emailed", len(to(box, BEN)) == 1)
decline_text = to(box, BEN)[0]["text"]
check("the new wording is used", "Please book another day." in decline_text)
check("the old wording is gone",
      "reserve any other day or other weekend" not in decline_text)
check("wording is in the HTML part too", "Please book another day." in to(box, BEN)[0]["html"])

print("\n--- now the third requester can book ---")
ok, _ = app.create_request(D[2], "Cara Lin", CARA, "", "Recital", 50, "")
check("Cara's request is accepted once a place is free", ok)
check("month full again", app.month_is_full(D[0]))
ok, _ = app.create_request(D[3], "Dana Ruiz", "dana@example.org", "", "Anniversary", 20, "")
check("a fourth requester is refused", not ok)

print("\n--- declining an APPROVED booking also frees a place ---")
ok, _ = app.decide(rid_ana, False)
check("Ana's approval withdrawn", ok)
check("one place free", app.active_count_in_month(D[0]) == 1)
ok, _ = app.create_request(D[3], "Dana Ruiz", "dana@example.org", "", "Anniversary", 20, "")
check("Dana can now book", ok)

print("\n--- approving a pending request never pushes a month over ---")
rid_cara = int(app.all_reservations()[app.all_reservations()["name"] == "Cara Lin"]["id"].iloc[0])
rid_dana = int(app.all_reservations()[app.all_reservations()["name"] == "Dana Ruiz"]["id"].iloc[0])
ok, msg = app.decide(rid_cara, True)
check("Cara approved even though the month is at its limit", ok)
ok, msg = app.decide(rid_dana, True)
check("Dana approved too", ok)
check("two approved, month full", app.reserved_count_in_month(D[0]) == 2 and app.month_is_full(D[0]))

print("\n--- next month is unaffected ---")
nxt = None
yy, mm = (D[0].year, D[0].month % 12 + 1)
if mm == 1: yy += 1
for dd in range(1, cal.monthrange(yy, mm)[1] + 1):
    if dt.date(yy, mm, dd).weekday() in app.SLOTS:
        nxt = dt.date(yy, mm, dd); break
ok, _ = app.create_request(nxt, "Eve Stone", "eve@example.org", "", "Meeting", 10, "")
check("a request in the following month is accepted", ok)

print("\nAll pending-flow checks passed.")
