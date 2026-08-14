import os, tempfile, datetime as dt, calendar, threading
os.environ["CALVARY_DB"] = os.path.join(tempfile.mkdtemp(), "n.db")
os.environ["GMAIL_APP_PASSWORD"] = "fake-app-password"
import app
app.init_db()

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

msg = [s[1] for s in sent if s[0] == "msg"][0]
check("to the notify address", msg["To"] == "5353murad@gmail.com")
check("reply-to is the requester", msg["Reply-To"] == "maria@example.org")
body = msg.get_content()
print("\n--- SUBJECT:", msg["Subject"]); print(body)
check("body has name", "Maria Lopez" in body)
check("body has purpose", "Quinceanera reception" in body)
check("body has headcount", "120" in body)
check("body has comments", "Kitchen and A/V" in body)
check("body shows month usage", "1 of 2 allowed" in body)

row = app.get_reservation(1)
check("db records the send", row["notify_status"].startswith("sent "))

# a mail failure must not lose the reservation
class BoomSMTP(FakeSMTP):
    def login(self, u, p): raise OSError("connection refused")
app.smtplib.SMTP_SSL = BoomSMTP
d2 = d + dt.timedelta(days=1)
while d2.weekday() not in app.SLOTS: d2 += dt.timedelta(days=1)
ok2, _ = app.create_request(d2, "B", "b@x.org", "", "P", 5, "")
check("booking survives a mail failure", ok2)
check("failure recorded", app.get_reservation(2)["notify_status"].startswith("failed:"))
print("     notify_status:", app.get_reservation(2)["notify_status"])

# ---- concurrency: two people submitting into a month with one slot left ----
os.environ["CALVARY_DB"] = os.path.join(tempfile.mkdtemp(), "c.db")
import importlib; importlib.reload(app)
app.smtplib.SMTP_SSL = FakeSMTP
app.init_db()
y, m = base.year, base.month
days = [dt.date(y,m,x) for x in range(1, calendar.monthrange(y,m)[1]+1)]
days = [x for x in days if x.weekday() in app.SLOTS and x > base]
if len(days) < 3:
    m = m % 12 + 1; y = y + (1 if m == 1 else 0)
    days = [dt.date(y,m,x) for x in range(1, calendar.monthrange(y,m)[1]+1)]
    days = [x for x in days if x.weekday() in app.SLOTS]
app.create_request(days[0], "First", "f@x.org", "", "P", 5, "")
results = []
def submit(day, who):
    results.append(app.create_request(day, who, "x@x.org", "", "P", 5, "")[0])
t1 = threading.Thread(target=submit, args=(days[1], "Racer1"))
t2 = threading.Thread(target=submit, args=(days[2], "Racer2"))
t1.start(); t2.start(); t1.join(); t2.join()
check("exactly one racer won", sum(results) == 1)
check("month holds exactly 2", app.active_count_in_month(days[0]) == 2)
print("\nNotification and concurrency tests passed.")
