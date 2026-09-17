"""
A failed send must never look like a success.

The bug this guards against: decide() marked the reservation approved, threw
away the SMTP error, and returned True, so the page said "everyone has been
emailed" while nothing had been. The date changed and nobody was told.
"""
import json, os, tempfile, datetime as dt

os.environ.setdefault("CALVARY_DB", os.path.join(tempfile.mkdtemp(), "fail.db"))
os.environ.update(SMTP_HOST="127.0.0.1", SMTP_PORT="8025", SMTP_SSL="0",
                  GMAIL_APP_PASSWORD="x", APP_BASE_URL="http://localhost:8501",
                  NOTIFY_EMAIL="organiser@example.org")
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


def a_slot(skip=0):
    d = dt.date.today() + dt.timedelta(days=1); f = 0
    while True:
        if d.weekday() in app.SLOTS:
            if f == skip:
                return d
            f += 1
        d += dt.timedelta(days=1)


# ------------------------------------------------- mail server unreachable
print("--- the mail server is down when the office approves ---")
d1 = a_slot(0)
app.create_request(d1, "Ana Diaz", "ana@example.org", "", "Baptism", 30, "")
rid = int(app.all_reservations()["id"].iloc[0])

app.SMTP_PORT = 9  # discard port: nothing listening
ok, msg = app.decide(rid, True)
app.SMTP_PORT = 8025

check("decide reports FAILURE, not success", not ok)
check("message says the status still changed", "marked reserved" in msg.lower())
check("message names the real reason", "requester" in msg and "organiser" in msg)
print(f"     {msg[:150]}")
check("the date really was approved", app.get_reservation(rid)["status"] == app.STATUS_RESERVED)
rec = app.get_reservation(rid)["decision_emails"]
check("the failure is recorded on the row", "FAILED" in rec)
print(f"     recorded: {rec[:110]}")

# ---------------------------------------------------------------- resending
print("\n--- resending once the mail server is back ---")
json.dump([], open(MAIL, "w"))
ok, msg = app.send_decision_emails(rid, True)
check("resend succeeds", ok)
check("resend says both were emailed", "both been emailed" in msg)

import time
for _ in range(60):
    box = json.load(open(MAIL))
    if len(box) >= 2:
        break
    time.sleep(0.1)
check("two emails actually arrived on resend", len(box) == 2)
check("one to the requester", any("ana@example.org" in m["to"][0] for m in box))
check("one to the organiser", any("organiser@example.org" in m["to"][0] for m in box))
check("row now records success", "FAILED" not in app.get_reservation(rid)["decision_emails"])

# --------------------------------------------- deciding again resends mail
print("\n--- clicking Approve again on an already approved request ---")
json.dump([], open(MAIL, "w"))
ok, msg = app.decide(rid, True)
check("reported as already approved", ok and "already reserved" in msg.lower())
for _ in range(60):
    box = json.load(open(MAIL))
    if len(box) >= 2:
        break
    time.sleep(0.1)
check("the emails are sent again rather than silently skipped", len(box) == 2)

# --------------------------------------------------- no password configured
print("\n--- no app password configured ---")
d2 = a_slot(1)
app.create_request(d2, "Ben Carter", "ben@example.org", "", "Youth night", 20, "")
rid2 = int(app.all_reservations()[app.all_reservations()["name"] == "Ben Carter"]["id"].iloc[0])
saved = os.environ.pop("GMAIL_APP_PASSWORD")
ok, msg = app.decide(rid2, False)
os.environ["GMAIL_APP_PASSWORD"] = saved
check("reports failure when no password is set", not ok)
check("message explains what is missing", "gmail_app_password" in msg)
print(f"     {msg[:120]}")
check("the decline still applied", app.get_reservation(rid2)["status"] == app.STATUS_DECLINED)

# ------------------------------------------------ requester with no address
print("\n--- requester left the email box empty ---")
d3 = a_slot(2)
app.create_request(d3, "Cara Lin", "", "", "Recital", 15, "")
rid3 = int(app.all_reservations()[app.all_reservations()["name"] == "Cara Lin"]["id"].iloc[0])
json.dump([], open(MAIL, "w"))
ok, msg = app.decide(rid3, True)
check("flagged, because the requester cannot be told", not ok)
check("message says why", "no email address" in msg)
for _ in range(40):
    box = json.load(open(MAIL))
    if box:
        break
    time.sleep(0.1)
check("the organiser is still emailed", any("organiser@example.org" in m["to"][0] for m in box))

print("\nAll mail-failure checks passed.")
