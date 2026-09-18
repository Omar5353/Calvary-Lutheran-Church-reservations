"""Quick checks on the reservation logic. Run with: python test_smoke.py"""
import os, tempfile, datetime as dt

os.environ["CALVARY_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
import app  # noqa: E402

app.init_db()

# Start from a clean table. With SQLite each run gets its own temp file, but a
# shared Postgres keeps rows between test files, so clear it explicitly.
with app.Db() as _db:
    _db.execute("DELETE FROM reservations")

def next_weekday(wd, offset_weeks=0):
    d = dt.date.today() + dt.timedelta(days=1)
    while d.weekday() != wd:
        d += dt.timedelta(days=1)
    return d + dt.timedelta(weeks=offset_weeks)

fri, sat, sun = next_weekday(4), next_weekday(5), next_weekday(6)
mon = next_weekday(0)

def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    assert cond, label

# slot lookup
check("Friday has a slot", app.slot_for(fri)["label"] == "6:30-10:30 p.m.")
check("Monday has no slot", app.slot_for(mon) is None)

# create
ok, msg = app.create_request(fri, "Jane Doe", "jane@x.org", "402-555-0101", "Wedding reception", 80, "Needs kitchen")
check("first request accepted", ok)

# duplicate on same date blocked
ok2, msg2 = app.create_request(fri, "Bob", "bob@x.org", "", "Bible study", 10, "")
check("duplicate date blocked", not ok2 and "taken" in msg2)

# non bookable weekday rejected
ok3, msg3 = app.create_request(mon, "Al", "al@x.org", "", "Meeting", 5, "")
check("Monday rejected", not ok3)

# past date rejected
ok4, _ = app.create_request(dt.date.today() - dt.timedelta(days=7), "Old", "o@x.org", "", "x", 1, "")
check("past date rejected", not ok4)

# calendar sees it as pending
sm = app.status_map(fri, fri)
check("pending shows in status map", sm[fri.isoformat()]["status"] == "Pending")

# approve
rid = int(app.all_reservations()["id"].iloc[0])
ok5, _ = app.set_status(rid, app.STATUS_RESERVED)
check("approve works", ok5)
check("now reserved", app.status_map(fri, fri)[fri.isoformat()]["status"] == "Reserved")

# decline frees the date
app.create_request(sat, "Sam", "s@x.org", "", "Concert", 40, "")
sat_id = int(app.all_reservations()[app.all_reservations()["event_date"] == sat.isoformat()]["id"].iloc[0])
app.set_status(sat_id, app.STATUS_DECLINED, "space conflict")
check("declined date is free again", sat.isoformat() not in app.status_map(sat, sat))
ok6, _ = app.create_request(sat, "New Group", "n@x.org", "", "Youth night", 30, "")
check("rebooking a declined date works", ok6)

# calendar html renders
html = app.month_html(fri.year, fri.month, app.status_map(dt.date(fri.year, fri.month, 1), dt.date(fri.year, fri.month, 28)), True)
check("calendar html contains name", "Jane Doe" in html)
html_pub = app.month_html(fri.year, fri.month, app.status_map(dt.date(fri.year, fri.month, 1), dt.date(fri.year, fri.month, 28)), False)
check("public calendar hides name", "Jane Doe" not in html_pub and "Reserved" in html_pub)

# dataframe export
df = app.all_reservations()
check("all_reservations returns rows", len(df) >= 3)
check("filter by status works", set(app.all_reservations((app.STATUS_RESERVED,))["status"]) == {"Reserved"})

# delete
app.delete_reservation(rid)
check("delete works", rid not in app.all_reservations()["id"].tolist())

# ---- email drafting ----
print()
row = app.all_reservations().iloc[0]
subj, body = app._email_parts(row)
check("subject has the name", row["name"] in subj)
check("body has purpose", row["purpose"] in body)
check("formal greeting", body.startswith("Dear Leanna,"))
check("formal signoff", body.rstrip().endswith("Omar Murad"))
check("body has headcount", str(row["num_people"]) in body)
url = app.gmail_compose_url(row)
check("gmail url host", url.startswith("https://mail.google.com/mail/?"))
check("to address encoded", app.office_email().replace("@", "%40") in url)
check("from account pinned", "authuser=muradpic12%40gmail.com" in url)
check("newlines encoded", "%0A" in url)
check("no raw spaces in url", " " not in url)
mt = app.mailto_url(row)
check("mailto target", mt.startswith(f"mailto:{app.office_email()}?"))

from urllib.parse import urlparse, parse_qs
q = parse_qs(urlparse(url).query)
check("subject round trips", q["su"][0] == subj)
check("body round trips", q["body"][0] == body)

# a request with an apostrophe and an ampersand should survive encoding
import datetime as _dt
d2 = next_weekday(6, 3)
app.create_request(d2, "O'Brien & Sons", "x@y.org", "", "Kid's party & potluck", 30, "Tables: 5 & chairs")
r2 = app.all_reservations()[app.all_reservations()["event_date"] == d2.isoformat()].iloc[0]
u2 = app.gmail_compose_url(r2)
q2 = parse_qs(urlparse(u2).query)
check("apostrophe survives", "O'Brien & Sons" in q2["su"][0])
check("ampersand does not split params", "Kid's party & potluck" in q2["body"][0])
check("to field not corrupted", q2["to"][0] == app.office_email())

print("\nAll checks passed.")
