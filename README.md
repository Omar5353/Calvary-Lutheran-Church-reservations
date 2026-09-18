# Calvary Lutheran Church, Event Reservation Scheduler

A small Streamlit app for taking event space requests and tracking them on a calendar.

## What it does

**Public side** (anyone with the link)

- *Request a reservation*: pick a date from a calendar picker, pick one of the three time slots from a radio list, then enter name, email, phone, purpose, number of people, and comments. Selecting a date auto-selects the matching time slot, and a live message says whether that combination is open, already taken, or on a day the building is not available. Submitting marks the date **Pending**.
- *Availability calendar*: sits at the bottom of the same page, a month grid showing each bookable day as Available, Pending, or Reserved. No names or event details are shown here.

**Admin side** (password protected)

- Calendar with the requester's name, purpose, and headcount written into each booked day.
- *Review requests*: approve or decline each pending request, with an optional internal note. Approving flips it to **Reserved**; declining frees the date up again.
- *Email the office*: every request carries a **Draft email to the office** button. It opens a Gmail compose window already addressed and filled in with the date, time, name, purpose, headcount, contact info and comments, so you skim it and hit Send. Nothing is sent automatically. A **Preview the email** expander shows exactly what will be in it, and a second button hands the same draft to whatever mail app the computer uses by default.
- *All reservations*: filterable, searchable table with every field, plus a CSV download and controls to change a status or delete an entry.

## Bookable slots

| Day | Time |
| --- | --- |
| Friday | 6:30-10:30 p.m. |
| Saturday | 1:00-5:00 p.m. |
| Sunday | 4:00-8:00 p.m. |

One event per date. Mon-Thu are shown as not bookable.

## Approve or decline from the email

> **Currently in test mode.** `EMAIL_TO` in `app.py` is set to `muradpic12@gmail.com`, not the church office, so a live trial cannot reach anyone real. The Admin page shows a red banner while this is true. To go live, set `EMAIL_TO = REAL_OFFICE_EMAIL`.


When a request is submitted the app sends three emails immediately:

1. **To the requester**, confirming it arrived and stating clearly that it is a request, not a booking, and the date is not theirs until the office approves.
2. **To you**, a heads-up with the details and where the month stands.
3. **To the church office** (`EMAIL_TO`), the same details plus **Approve** and **Decline** buttons.

Pressing either button opens a small page in the app showing the request, with one confirmation button. Nothing changes until that is pressed. This matters because mail scanners, link previewers and some corporate security products fetch every URL in an incoming message; if the link itself decided the outcome, a booking could be approved before anyone read it.

On confirming:

- **Approve** marks the date Reserved on the calendar, emails the requester that they are confirmed, and emails you. Blocked if the month already holds the limit.
- **Decline** frees the date for others, and emails the requester including the line *"Please book another day."* plus a link back to the scheduler. You are emailed too.

The Approve and Decline buttons on the Admin page do exactly the same thing through the same code path, so the two routes cannot drift apart.

### Doing a live trial safely

Point the emails at your own inbox for one run, without editing any file:

```bash
OFFICE_EMAIL=muradpic12@gmail.com streamlit run app.py
```

While an override is active the Admin page shows a red banner naming the addresses in use, so a redirected app cannot be mistaken for the real one. Stop the app and start it normally to go back to the church office. On Streamlit Cloud the same override is an `office_email` line in the Secrets panel; delete the line to revert.

### When an email does not arrive

Every send is reported honestly. If the requester or organiser cannot be emailed, the decision page and the Admin page both say so, quoting the actual error, and note that the status was still changed. Nothing is ever reported as sent when it was not.

The Admin page, under *All reservations, Email delivery*, shows what happened to each message for a chosen booking:

```
- Acknowledgement to requester: sent
- Request to the office:        sent
- Copy to you:                  sent
- Decision emails:              requester FAILED, organiser FAILED | ConnectionRefusedError ...
```

A **Resend the decision emails** button there sends them again once the problem is fixed. Pressing Approve or Decline a second time also resends, since a repeated click usually means the first email never arrived.

### How the links are secured

Each link carries an HMAC signature tied to both the reservation id and the action. An Approve link cannot be edited into a Decline link, neither works on a different booking, and a guessed or altered signature is refused. The signing key comes from the `decision_secret` secret, falling back to `admin_password` if that is not set. Changing either one invalidates links already in flight.

Deciding twice is a no-op, so a forwarded email cannot double-send anything.

### Settings

```python
EMAIL_TO = "office@calvarylincoln.org"   # who gets the Approve / Decline email
NOTIFY_EMAIL = "muradpic12@gmail.com"     # who gets the copies
EMAIL_GREETING_NAME = "Leanna"           # how the office email opens
DEFAULT_BASE_URL = "https://calvary-lutheran-church-reservations.streamlit.app"
```

`DEFAULT_BASE_URL` must match the real address of the deployed app, since the buttons point there. Override it without editing code by adding `app_base_url` to secrets. If it is wrong, the emails still send but the buttons lead nowhere.

## Monthly limit

At most **2 reservations per calendar month**, set by `MAX_PER_MONTH` in `app.py`.

A pending request **holds its place**. Two unreviewed requests therefore close the month to a third, who is told to pick another month. A place is taken the moment someone asks for it.

Only a **decline** frees a place, whether the thing declined was pending or already approved. Approving a pending request changes nothing about the count, since that request was already holding its place, so an approval can never push a month over the limit.

When the limit is reached the month's remaining dates grey out as "Month full", drop off the date picker, and new requests are refused. The calendar caption spells out the position: *"2 of 2 places taken in September 2026 (0 approved, 2 awaiting a decision)"*.

Both checks run inside a locked transaction, so six simultaneous requests for one remaining place produce exactly one winner.

## Automatic email notification

Every submitted request is emailed to the address in `NOTIFY_EMAIL` right away, with the requester's email set as Reply-To so you can answer them directly. This is separate from the **Draft email to the office** button, which stays manual.

It needs a Gmail app password, which is not your normal Google password:

1. Turn on 2-Step Verification for the sending account at https://myaccount.google.com/security
2. Go to https://myaccount.google.com/apppasswords and create one named e.g. "Calvary scheduler"
3. Google shows a 16-character code. Put it in `.streamlit/secrets.toml` alongside the admin password:

   ```toml
   admin_password = "your-admin-password"
   gmail_app_password = "abcdefghijklmnop"
   ```

   On Streamlit Community Cloud, put both lines in the app's *Settings, Secrets* panel instead.

4. Open the Admin page and click **Send a test email** to confirm it works.

Without the app password the app runs normally and just skips notifications, and the admin page shows a banner saying so. If a send fails, the reservation is still saved and the *Emailed* column in the All reservations table records what happened.

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

The app opens at http://localhost:8501.

### Set your admin password

Open `.streamlit/secrets.toml` and replace `change-me`:

```toml
admin_password = "your-real-password"
```

Alternatively set the `CALVARY_ADMIN_PASSWORD` environment variable, which takes over if no secrets file is present.

### Data

Two storage backends, chosen automatically:

- **Postgres**, when a `postgres_url` secret is present. Use this for anything deployed. Data survives restarts, sleeps and redeploys.
- **SQLite**, otherwise. A `reservations.db` file next to `app.py`, created on first run. Fine for local use and for trying the app out. Move it elsewhere with the `CALVARY_DB` environment variable.

The Admin page always states which one is live, and warns you if a deployed app is still on SQLite.

## Setting up Supabase Postgres

Streamlit Community Cloud gives each app a temporary disk and puts apps to sleep after 12 hours without traffic. When an app wakes it is rebuilt from the repo, so a SQLite file written at runtime is gone and every reservation with it. Postgres fixes this permanently.

1. Sign up at [supabase.com](https://supabase.com) and create a project. The free tier is enough for this by a wide margin. Save the database password it asks you to set.
2. In the project, open **Connect** (top of the dashboard).
3. Choose the **Session pooler** connection string, not "Direct connection". Direct connections are IPv6-only on new Supabase projects and Streamlit Cloud cannot reach them. The pooler URI works over IPv4.
4. It looks like this, with a placeholder where your password goes:

   ```
   postgresql://postgres.abcdefgh:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
   ```

   Replace `[YOUR-PASSWORD]` with the database password from step 1. The app refuses a string that still contains the placeholder rather than failing later with a confusing authentication error.

5. Add it to your secrets, locally in `.streamlit/secrets.toml` and on Streamlit Cloud in *Settings, Secrets*:

   ```toml
   admin_password = "your-admin-password"
   gmail_app_password = "your-gmail-app-password"
   postgres_url = "postgresql://postgres.abcdefgh:realpassword@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
   ```

6. Restart the app. The table is created automatically on first run, and the Admin page will report "Storage: Supabase Postgres".

### Moving existing reservations across

If you already have bookings in `reservations.db`:

```bash
POSTGRES_URL="postgresql://..." python3 migrate_to_postgres.py --dry-run   # preview
POSTGRES_URL="postgresql://..." python3 migrate_to_postgres.py            # apply
```

It skips rows already present, so running it twice is harmless.

### About sleeping

Postgres does not stop the app from hibernating after 12 hours of no traffic; it stops hibernation from destroying data. A sleeping app shows a wake button that **anyone** viewing can click, and it is back in about 30 seconds with every reservation intact. If the wake screen itself is a problem, a scheduled job that visits the URL every few hours keeps it awake, or a host without hibernation removes the issue entirely.

## Running the tests

```bash
./run_tests.sh                                          # against SQLite
POSTGRES_URL="postgresql://..." ./run_tests.sh          # against Postgres
```

Covers the booking rules, the monthly cap, email drafting and encoding, app password sanitising, six simultaneous submissions racing for one remaining slot, and the whole approve/decline flow end to end.

Email tests deliver to `mailcatcher.py`, a local capture server the runner starts for you, so **the tests never send real email**. To inspect what was captured, run it yourself and read the JSON:

```bash
python3 mailcatcher.py 8025 /tmp/mail.json &
python3 test_approval.py
python3 -m json.tool /tmp/mail.json | less
```

## Deploying so others can reach it

1. Put this folder in a GitHub repo, but **do not commit `.streamlit/secrets.toml` or `reservations.db`** (a `.gitignore` is included that excludes both).
2. Go to https://share.streamlit.io, connect the repo, and set the main file to `app.py`.
3. In the app's *Settings, Secrets* panel, paste:

   ```toml
   admin_password = "your-real-password"
   ```

4. Share the public URL with the congregation. You reach the admin view through the same URL, under *Admin* in the sidebar.

Note on Streamlit Community Cloud: its disk is not permanent, so the database can be wiped when the app restarts or redeploys. Download the CSV from the admin tab regularly, or move the storage to Google Sheets or Postgres if the app will be in heavy use. Running it on a church computer or a small VPS keeps the SQLite file intact.

## Changing the email addresses

Near the top of `app.py`:

```python
EMAIL_FROM = "muradpic12@gmail.com"      # the Gmail account the draft opens in
EMAIL_TO = "office@calvarylincoln.org"  # who the draft is addressed to
EMAIL_CC = ""                           # optional, comma separated
EMAIL_SIGNOFF = "Omar"
```

`EMAIL_FROM` is passed to Gmail as the `authuser` hint. If you are signed into several Google accounts in the same browser, Gmail opens the compose window in that one. If you are signed into only one account, it opens there regardless. The wording of the message itself is in `_email_parts()` a little further down.

## Changing the time slots

Edit the `SLOTS` dictionary near the top of `app.py`. Keys are Python weekday numbers, Monday is 0 and Sunday is 6.

```python
SLOTS = {
    4: {"day": "Friday",   "label": "6:30-10:30 p.m.", "start": "18:30", "end": "22:30"},
    5: {"day": "Saturday", "label": "1:00-5:00 p.m.",  "start": "13:00", "end": "17:00"},
    6: {"day": "Sunday",   "label": "4:00-8:00 p.m.",  "start": "16:00", "end": "20:00"},
}
```

`MONTHS_AHEAD` on the next few lines controls how far into the future people can browse and request.
