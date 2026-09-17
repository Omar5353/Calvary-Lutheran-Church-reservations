import os, tempfile, datetime as dt, calendar, threading
os.environ["CALVARY_DB"] = os.path.join(tempfile.mkdtemp(), "n.db")
os.environ["GMAIL_APP_PASSWORD"] = "fake-app-password"
import app
app.init_db()

# Start from a clean table. With SQLite each run gets its own temp file, but a
# shared Postgres keeps rows between test files, so clear it explicitly.
with app.Db() as _db:
    _db.execute("DELETE FROM reservations")

def check(l, c):
    print(("PASS  " if c else "FAIL  ") + l); assert c, l

sent = []
class FakeSMTP:
    def __init__(self, host, port, timeout=None): sent.append(("connect", host, port))
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, u, p): sent.append(("login", u, p))
    def send_message(self, m): sent.append(("msg", m))
app.smtplib.SMTP_SSL = FakeSMTP

base = dt.date.today()
d = base + dt.timedelta(days=1)
while d.weekday() not in app.SLOTS: d += dt.timedelta(days=1)

ok, _ = app.create_request(d, "Maria Lopez", "maria@example.org", "402-555-0101",
                           "Quinceanera reception", 120, "Kitchen and A/V needed")
check("request accepted", ok)
check("connected to gmail", ("connect", "smtp.gmail.com", 465) in sent)
check("logged in as the right account", ("login", "5353murad@gmail.com", "fake-app-password") in sent)

msgs = [s[1] for s in sent if s[0] == "msg"]
check("three emails leave on submission (requester, organiser, office)", len(msgs) == 3)
check("the requester is acknowledged",
      any(m["To"] == "maria@example.org" for m in msgs))
check("the office is asked to decide",
      any(m["To"] == app.office_email() for m in msgs))

msg = [m for m in msgs if m["To"] == "5353murad@gmail.com"][0]   # the organiser copy
check("to the notify address", msg["To"] == "5353murad@gmail.com")
check("reply-to is the requester", msg["Reply-To"] == "maria@example.org")
body = msg.get_content()
print("\n--- SUBJECT:", msg["Subject"]); print(body)
check("body has name", "Maria Lopez" in body)
check("body has purpose", "Quinceanera reception" in body)
check("body has headcount", "120" in body)
check("body has comments", "Kitchen and A/V" in body)
check("body shows month usage", "0 of 2 approved" in body and "1 awaiting" in body)

row = app.get_reservation(int(app.all_reservations()["id"].iloc[0]))
check("db records the send", row["notify_status"].startswith("sent "))

# a mail failure must not lose the reservation
class BoomSMTP(FakeSMTP):
    def login(self, u, p): raise OSError("connection refused")
app.smtplib.SMTP_SSL = BoomSMTP
d2 = d + dt.timedelta(days=1)
while d2.weekday() not in app.SLOTS: d2 += dt.timedelta(days=1)
ok2, _ = app.create_request(d2, "B", "b@x.org", "", "P", 5, "")
check("booking survives a mail failure", ok2)
check("failure recorded", app.get_reservation(int(app.all_reservations()["id"].iloc[-1]))["notify_status"].startswith("failed:"))
print("     notify_status:", app.get_reservation(int(app.all_reservations()["id"].iloc[-1]))["notify_status"])

print("\nNotification tests passed.")
