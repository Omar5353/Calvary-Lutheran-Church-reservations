# Calvary Lutheran Church, Event Reservation Scheduler

A small Streamlit app for taking event space requests and tracking them on a calendar.

## What it does

**Public side** (anyone with the link)

- *Request a reservation*: pick a date from a calendar picker, pick one of the three time slots from a radio list, then enter name, email, phone, purpose, number of people, and comments. Selecting a date auto-selects the matching time slot, and a live message says whether that combination is open, already taken, or on a day the building is not available. Submitting marks the date **Pending**.
- *Availability calendar*: sits at the bottom of the same page, a month grid showing each bookable day as Available, Pending, or Reserved. No names or event details are shown here.

**Admin side** (password protected)

- Calendar with the requester's name, purpose, and headcount written into each booked day.
- *Review requests*: approve or decline each pending request, with an optional internal note. Approving flips it to **Reserved**; declining frees the date up again.
- *All reservations*: filterable, searchable table with every field, plus a CSV download and controls to change a status or delete an entry.

## Bookable slots

| Day | Time |
| --- | --- |
| Friday | 6:30-10:30 p.m. |
| Saturday | 1:00-5:00 p.m. |
| Sunday | 3:00-7:00 p.m. |

One event per date. Mon-Thu are shown as not bookable.

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

Everything lives in `reservations.db`, a SQLite file created next to `app.py` on first run. Back it up by copying that one file. To store it elsewhere, set the `CALVARY_DB` environment variable to the path you want.

## Deploying so others can reach it

1. Put this folder in a GitHub repo, but **do not commit `.streamlit/secrets.toml` or `reservations.db`** (a `.gitignore` is included that excludes both).
2. Go to https://share.streamlit.io, connect the repo, and set the main file to `app.py`.
3. In the app's *Settings, Secrets* panel, paste:

   ```toml
   admin_password = "your-real-password"
   ```

4. Share the public URL with the congregation. You reach the admin view through the same URL, under *Admin* in the sidebar.

Note on Streamlit Community Cloud: its disk is not permanent, so the database can be wiped when the app restarts or redeploys. Download the CSV from the admin tab regularly, or move the storage to Google Sheets or Postgres if the app will be in heavy use. Running it on a church computer or a small VPS keeps the SQLite file intact.

## Changing the time slots

Edit the `SLOTS` dictionary near the top of `app.py`. Keys are Python weekday numbers, Monday is 0 and Sunday is 6.

```python
SLOTS = {
    4: {"day": "Friday",   "label": "6:30-10:30 p.m.", "start": "18:30", "end": "22:30"},
    5: {"day": "Saturday", "label": "1:00-5:00 p.m.",  "start": "13:00", "end": "17:00"},
    6: {"day": "Sunday",   "label": "3:00-7:00 p.m.",  "start": "15:00", "end": "19:00"},
}
```

`MONTHS_AHEAD` on the next few lines controls how far into the future people can browse and request.
