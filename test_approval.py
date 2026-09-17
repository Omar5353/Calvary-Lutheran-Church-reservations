"""
End to end test of the approve / decline by email flow.

Every message is delivered to a local capture server, so no real email is
ever sent. Start the catcher first:

    python3 mailcatcher.py 8025 /tmp/mail.json &
    python3 test_approval.py
"""
import json, os, tempfile, datetime as dt

os.environ.setdefault("CALVARY_DB", os.path.join(tempfile.mkdtemp(), "approval.db"))
os.environ["SMTP_HOST"] = "127.0.0.1"
os.environ["SMTP_PORT"] = "8025"
os.environ["SMTP_SSL"] = "0"
os.environ["GMAIL_APP_PASSWORD"] = "not-used-by-the-catcher"
os.environ["APP_BASE_URL"] = "http://localhost:8501"
os.environ["OFFICE_EMAIL"] = "muradpic12@gmail.com"   # the trial inbox, not the real office
os.environ["NOTIFY_EMAIL"] = "5353murad@gmail.com"
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
MAIL = os.environ.get("MAILBOX", "/tmp/mail.json")

import app  # noqa: E402

app.init_db()
with app.Db() as db:
    db.execute("DELETE FROM reservations")

def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    assert cond, label

def mailbox(expect=None, timeout=6.0):
    """Read captured mail, waiting briefly for the catcher to flush to disk."""
    import time
    deadline = time.time() + timeout
    while True:
        try:
            box = json.load(open(MAIL))
        except Exception:
            box = []
        if expect is None or len(box) >= expect or time.time() > deadline:
            return box
        time.sleep(0.1)

def clear_mail():
    json.dump([], open(MAIL, "w"))

REQUESTER = "maria.lopez@example.org"

def next_slot(skip=0):
    d = dt.date.today() + dt.timedelta(days=1)
    found = 0
    while True:
        if d.weekday() in app.SLOTS:
            if found == skip:
                return d
            found += 1
        d += dt.timedelta(days=1)

# ---------------------------------------------------------------- submission
print("\n--- a request comes in ---")
clear_mail()
day = next_slot()
ok, _ = app.create_request(day, "Maria Lopez", REQUESTER, "402-555-0101",
                           "Quinceanera reception", 120, "Kitchen and A/V needed")
check("request accepted", ok)

box = mailbox(expect=2)
check("two emails went out on submission", len(box) == 2)
office = [m for m in box if app.office_email() in m["to"]]
owner = [m for m in box if app.notify_email() in m["to"]]
check("one addressed to the trial office inbox", len(office) == 1)
check("nothing addressed to the real church office",
      not any("office@calvarylincoln.org" in t for m in box for t in m["to"]))
check("one addressed to the organiser", len(owner) == 1)

o = office[0]
print(f"     office subject: {o['subject']}")
check("office mail replies to the requester", o["reply_to"] == REQUESTER)
check("office mail names the requester", "Maria Lopez" in o["text"])
check("office mail has the purpose", "Quinceanera reception" in o["text"])
check("office mail has the comments", "Kitchen and A/V" in o["text"])
check("office mail is addressed to Leanna", o["text"].startswith("Dear Leanna,"))
check("office mail has an HTML part", bool(o["html"]))
check("HTML has an Approve button", "Approve" in o["html"] and "background:#2e7d32" in o["html"])
check("HTML has a Decline button", "Decline" in o["html"] and "background:#c62828" in o["html"])

rid = int(app.all_reservations()["id"].iloc[0])
approve_url = app.decision_url(rid, "approve")
decline_url = app.decision_url(rid, "decline")
check("approve link is in the email", approve_url in o["html"] and approve_url in o["text"])
check("decline link is in the email", decline_url in o["html"])
check("links point at the app", approve_url.startswith("http://localhost:8501/?"))
check("office send was recorded", app.get_reservation(rid)["office_status"] == "sent")

# ------------------------------------------------------------------ security
print("\n--- link signatures ---")
tok = app.action_token(rid, "approve")
check("correct token verifies", app.verify_token(rid, "approve", tok))
check("approve token cannot decline", not app.verify_token(rid, "decline", tok))
check("token cannot be reused on another booking", not app.verify_token(rid + 1, "approve", tok))
check("tampered token rejected", not app.verify_token(rid, "approve", tok[:-1] + ("0" if tok[-1] != "0" else "1")))
check("empty token rejected", not app.verify_token(rid, "approve", ""))
check("guessed token rejected", not app.verify_token(rid, "approve", "a" * 32))

# ------------------------------------------------------------------- approve
print("\n--- the office approves ---")
clear_mail()
ok, msg = app.decide(rid, True, note="approved in test")
check("decision applied", ok)
check("date is now reserved", app.get_reservation(rid)["status"] == app.STATUS_RESERVED)
check("calendar shows it as reserved",
      app.status_map(day, day)[day.isoformat()]["status"] == app.STATUS_RESERVED)

box = mailbox(expect=2)
check("two emails went out on approval", len(box) == 2)
to_req = [m for m in box if REQUESTER in m["to"]][0]
to_own = [m for m in box if app.notify_email() in m["to"]][0]
print(f"     requester subject: {to_req['subject']}")
print(f"     organiser subject: {to_own['subject']}")
check("requester told it was approved", "approved" in to_req["subject"].lower())
check("requester mail greets them by name", to_req["text"].startswith("Dear Maria Lopez,"))
check("requester mail states the date", day.strftime("%A, %B %d, %Y") in to_req["text"])
check("organiser told it was approved", to_own["subject"].startswith("Approved:"))
check("organiser mail names the requester", "Maria Lopez" in to_own["text"])
check("both sends recorded", "requester ok" in app.get_reservation(rid)["decision_emails"])

print("\n--- approving twice does nothing ---")
clear_mail()
ok, msg = app.decide(rid, True)
check("second approval is a no-op", ok and msg == "already reserved")
import time as _t; _t.sleep(1.5)
check("no duplicate emails", len(mailbox()) == 0)

# ------------------------------------------------------------------- decline
print("\n--- the office declines a different request ---")
clear_mail()
day2 = next_slot(skip=1)
app.create_request(day2, "Tom Becker", "tom.becker@example.org", "", "Memorial luncheon", 60, "")
rid2 = int(app.all_reservations()[app.all_reservations()["name"] == "Tom Becker"]["id"].iloc[0])
clear_mail()

ok, _ = app.decide(rid2, False, note="hall already in use")
check("decline applied", ok)
check("status is Declined", app.get_reservation(rid2)["status"] == app.STATUS_DECLINED)
check("date is free again", day2.isoformat() not in app.status_map(day2, day2))

box = mailbox(expect=2)
check("two emails went out on decline", len(box) == 2)
dec_req = [m for m in box if "tom.becker" in m["to"][0]][0]
dec_own = [m for m in box if app.notify_email() in m["to"]][0]
SENTENCE = "Book another day, or please reserve any other day or other weekend."
check("requester gets the exact wording requested", SENTENCE in dec_req["text"])
check("wording is in the HTML part too", SENTENCE in dec_req["html"])
check("requester mail says declined", "declined" in dec_req["subject"].lower())
check("requester gets a link back to the scheduler", "http://localhost:8501" in dec_req["text"])
check("organiser told it was declined", dec_own["subject"].startswith("Declined:"))
check("organiser told the date reopened", "released" in dec_own["text"])

print("\n--- a freed date can be requested again ---")
clear_mail()
ok, _ = app.create_request(day2, "Second Family", "second@example.org", "", "Birthday", 30, "")
check("rebooking the declined date works", ok)

print("\nAll approval checks passed. No real email was sent.")
