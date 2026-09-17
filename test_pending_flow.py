"""
The scenario Omar described: two people request dates in the same month.

While both sit unreviewed the month must stay open and show them as pending.
Declining reopens a date. Approving is what fills the month, and once the
limit is reached by approvals the month reads as full.

Also checks the requester is emailed at every stage: on submission, and again
when the office approves or declines.
"""
import json, os, tempfile, calendar as cal, datetime as dt

os.environ.setdefault("CALVARY_DB", os.path.join(tempfile.mkdtemp(), "flow.db"))
os.environ.update(SMTP_HOST="127.0.0.1", SMTP_PORT="8025", SMTP_SSL="0",
                  GMAIL_APP_PASSWORD="x", APP_BASE_URL="http://localhost:8501",
                  OFFICE_EMAIL="office-trial@example.org", NOTIFY_EMAIL="organiser@example.org")
MAIL = os.environ.get("MAILBOX", "/tmp/mail.json")

def _ensure_catcher(host="127.0.0.1", port=8025):
    """Start the capture server if it is not already listening."""
    import socket, subprocess, sys, time, os
    s = socket.socket(); s.settimeout(1)
    try:
        s.connect((host, port)); s.close(); return None
    except Exception:
        pass
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mailcatcher.py"),
         str(port), os.environ.get("MAILBOX", "/tmp/mail.json")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            s = socket.socket(); s.settimeout(1); s.connect((host, port)); s.close()
            return proc
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("could not start mailcatcher on port %d" % port)


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

# four bookable dates inside one calendar month
base = dt.date.today()
y, m = base.year, base.month
def days(y, m):
    return [dt.date(y, m, d) for d in range(1, cal.monthrange(y, m)[1] + 1)
            if dt.date(y, m, d).weekday() in app.SLOTS and dt.date(y, m, d) > base]
D = days(y, m)
while len(D) < 4:
    m = m % 12 + 1
    if m == 1: y += 1
    D = days(y, m)
month_name = D[0].strftime("%B %Y")
print(f"working in {month_name}, limit is {app.MAX_PER_MONTH} approved\n")

ANA, BEN = "ana@example.org", "ben@example.org"

# ---------------------------------------------- two requests, nobody reviewed
print("--- Ana and Ben both request a date, nothing approved yet ---")
clear()
ok1, _ = app.create_request(D[0], "Ana Diaz", ANA, "402-555-0001", "Baptism reception", 40, "")
box = mail(expect=3)
check("Ana's request accepted", ok1)
check("Ana got an acknowledgement", len(to(box, ANA)) == 1)
ack = to(box, ANA)[0]
print(f"     ack subject: {(ack['subject'] or '').replace(chr(10),' ').strip()}")
check("ack says it is only a request", "not a confirmed booking" in ack["html"])
check("ack does not promise the date", "pending" in ack["text"].lower())
check("office was emailed too", len(to(box, "office-trial@example.org")) == 1)
check("organiser was emailed too", len(to(box, "organiser@example.org")) == 1)

clear()
ok2, _ = app.create_request(D[1], "Ben Carter", BEN, "", "Youth night", 25, "")
mail(expect=3)
check("Ben's request accepted in the same month", ok2)

check("two pending in the month", app.pending_count_in_month(D[0]) == 2)
check("nothing approved yet", app.reserved_count_in_month(D[0]) == 0)
check("month is NOT full while both are pending", not app.month_is_full(D[0]))

sm = app.status_map(D[0], D[3])
check("Ana's date shows as Pending", sm[D[0].isoformat()]["status"] == "Pending")
check("Ben's date shows as Pending", sm[D[1].isoformat()]["status"] == "Pending")

counts = app.month_counts(D[0].replace(day=1), D[-1])
c = counts[(D[0].year, D[0].month)]
check("month_counts separates approved from pending", c == {"reserved": 0, "pending": 2})

# a third person can still ask, because nothing is approved
ok3, msg3 = app.create_request(D[2], "Cara Lin", "cara@example.org", "", "Recital", 50, "")
check("a third request is still allowed while none are approved", ok3)
check("three pending now", app.pending_count_in_month(D[0]) == 3)
check("month still not full", not app.month_is_full(D[0]))

# ------------------------------------------------------------ decline reopens
print("\n--- the office declines Ben ---")
clear()
rid_ben = int(app.all_reservations()[app.all_reservations()["name"] == "Ben Carter"]["id"].iloc[0])
ok, _ = app.decide(rid_ben, False)
box = mail(expect=2)
check("decline applied", ok)
check("Ben's date is open again", D[1].isoformat() not in app.status_map(D[1], D[1]))
check("Ben was emailed", len(to(box, BEN)) == 1)
check("Ben told to book another day",
      "Book another day, or please reserve any other day or other weekend." in to(box, BEN)[0]["text"])
ok, _ = app.create_request(D[1], "Dana Ruiz", "dana@example.org", "", "Anniversary", 20, "")
check("the freed date can be requested again", ok)

# --------------------------------------------------------- approvals fill it
print("\n--- approvals are what fill the month ---")
clear()
rid_ana = int(app.all_reservations()[app.all_reservations()["name"] == "Ana Diaz"]["id"].iloc[0])
ok, _ = app.decide(rid_ana, True)
box = mail(expect=2)
check("Ana approved", ok)
check("Ana was emailed the approval", len(to(box, ANA)) == 1)
check("approval email says approved", "approved" in to(box, ANA)[0]["subject"].lower())
check("one approved so far", app.reserved_count_in_month(D[0]) == 1)
check("month still not full at 1 of 2", not app.month_is_full(D[0]))

rid_cara = int(app.all_reservations()[app.all_reservations()["name"] == "Cara Lin"]["id"].iloc[0])
ok, _ = app.decide(rid_cara, True)
check("Cara approved", ok)
check("two approved", app.reserved_count_in_month(D[0]) == 2)
check("NOW the month is full", app.month_is_full(D[0]))

# ------------------------------------------------- full month blocks the rest
print("\n--- with the month full ---")
ok, msg = app.create_request(D[3], "Eve Stone", "eve@example.org", "", "Meeting", 10, "")
check("new requests are refused", not ok)
check("refusal explains it is approved bookings", "approved reservations" in msg)
print(f"     {msg}")

rid_dana = int(app.all_reservations()[app.all_reservations()["name"] == "Dana Ruiz"]["id"].iloc[0])
ok, msg = app.decide(rid_dana, True)
check("a leftover pending request cannot be approved past the limit", not ok)
check("message tells the office what to do", "Decline one of those" in msg)
print(f"     {msg}")

ok, _ = app.decide(rid_dana, False)
check("but it can still be declined", ok)

print("\n--- declining an approved booking reopens the month ---")
ok, _ = app.decide(rid_cara, False)
check("Cara's approval withdrawn", ok)
check("back to one approved", app.reserved_count_in_month(D[0]) == 1)
check("month is open again", not app.month_is_full(D[0]))
ok, _ = app.create_request(D[3], "Eve Stone", "eve@example.org", "", "Meeting", 10, "")
check("a new request is accepted again", ok)

print("\nAll pending-flow checks passed.")
