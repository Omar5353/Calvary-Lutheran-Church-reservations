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

## Monthly limit

At most **2 active reservations per calendar month**, set by `MAX_PER_MONTH` in `app.py`. Pending counts toward the limit, the same as Reserved, so two unreviewed requests close the month until you act on one. Declining frees the slot immediately and the month reopens.

When a month is full, its remaining dates are dropped from the date picker, the calendar greys them out and labels them "Month full", and the request form refuses to submit if someone types the date in by hand. The check also runs inside the database transaction, so two people submitting at the same instant cannot both slip through. Re-approving a previously declined request is blocked too if the month has since filled up.

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

Covers the booking rules, the monthly cap, email drafting and encoding, app password sanitising, and six simultaneous submissions racing for one remaining slot.

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
EMAIL_FROM = "5353murad@gmail.com"      # the Gmail account the draft opens in
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
