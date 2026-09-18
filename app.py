"""
Calvary Lutheran Church, Event Reservation Scheduler
=====================================================

A Streamlit app with two sides:

* Public side  : anyone can see which dates are open and submit a request.
* Admin side   : password protected, shows every reservation with the
                 requester's name, purpose, contact info and comments,
                 plus approve / decline controls and a CSV export.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import calendar as pycalendar
import io
import os
import hashlib
import hmac
import smtplib
import sqlite3
from contextlib import closing, contextmanager
from email.message import EmailMessage
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode

import pandas as pd
import streamlit as st

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

CHURCH_NAME = "Calvary Lutheran Church"
DB_PATH = os.environ.get("CALVARY_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "reservations.db"))

# Bookable slots keyed by Python weekday (Mon=0 ... Sun=6).
SLOTS = {
    4: {"day": "Friday", "label": "6:30-10:30 p.m.", "start": "18:30", "end": "22:30"},
    5: {"day": "Saturday", "label": "1:00-5:00 p.m.", "start": "13:00", "end": "17:00"},
    6: {"day": "Sunday", "label": "4:00-8:00 p.m.", "start": "16:00", "end": "20:00"},
}

STATUS_PENDING = "Pending"
STATUS_RESERVED = "Reserved"
STATUS_DECLINED = "Declined"
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RESERVED)

# Email notification. The admin view builds a pre-filled Gmail compose link
# so a request can be forwarded to the church office in one click.
EMAIL_FROM = "muradpic12@gmail.com"     # the Gmail account the draft opens in
# The real church office. Do not delete this line; it is what the test banner
# compares against, and what you restore EMAIL_TO to when testing is finished.
REAL_OFFICE_EMAIL = "office@calvarylincoln.org"

# ---------------------------------------------------------------- TEST MODE
# Approve / Decline emails are going to a test inbox, NOT the church office.
# To go live, set this back to REAL_OFFICE_EMAIL.
EMAIL_TO = "muradpic12@gmail.com"
# ---------------------------------------------------------------------------
EMAIL_CC = ""                           # optional, comma separated
EMAIL_GREETING_NAME = "Leanna"          # who the message is addressed to by name
EMAIL_SIGNOFF = "Omar Murad"

# Automatic notification sent the moment a request is submitted.
NOTIFY_EMAIL = "muradpic12@gmail.com"    # default; override with notify_email / NOTIFY_EMAIL
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = "muradpic12@gmail.com"
# Tests point these at a local capture server; SMTP_SSL=0 uses plain SMTP.
SMTP_SSL = os.environ.get("SMTP_SSL", "1") != "0"

# Public URL of the deployed app, used to build the Approve and Decline links
# that go in the office email. Override with the app_base_url secret.
DEFAULT_BASE_URL = "https://calvary-lutheran-church-reservations.streamlit.app"
# The app password comes from st.secrets["gmail_app_password"] or the
# GMAIL_APP_PASSWORD environment variable. Without it, notifications are
# skipped and reservations still work normally.

# At most this many active (pending or reserved) reservations per calendar
# month. Declined requests do not count, so declining frees a slot back up.
MAX_PER_MONTH = 2

# How far ahead the public may request / browse.
MONTHS_AHEAD = 6

COLORS = {
    "available": ("#e8f5e9", "#2e7d32", "Available"),
    "pending": ("#fff4e0", "#b26a00", "Pending"),
    "reserved": ("#fdecec", "#c62828", "Reserved"),
    "full": ("#f2f2f7", "#6b6b8a", "Month full"),
    "closed": ("#f5f5f5", "#9e9e9e", ""),
    "past": ("#fafafa", "#c4c4c4", ""),
}


# --------------------------------------------------------------------------
# Database layer
# --------------------------------------------------------------------------
def _raw_secret(name: str) -> str | None:
    """
    Read a config value from st.secrets, falling back to the environment.

    Defined early because the storage layer needs the connection string
    before anything else runs.
    """
    try:
        val = st.secrets.get(name)
        if val:
            return str(val).strip()
    except Exception:
        pass
    val = os.environ.get(name.upper())
    return val.strip() if val else None


def postgres_url() -> str | None:
    """
    Supabase (or any Postgres) connection string, if one is configured.

    When absent the app falls back to a local SQLite file, which keeps
    development and offline use working with no setup at all.
    """
    url = _raw_secret("postgres_url")
    if not url:
        return None
    # Supabase shows the URI with a placeholder for the password. Refuse it
    # rather than failing later with a confusing authentication error.
    if "[YOUR-PASSWORD]" in url or "[PASSWORD]" in url:
        return None
    return url


def using_postgres() -> bool:
    return postgres_url() is not None


class Db:
    """
    A small connection wrapper so the rest of the app does not care which
    database is behind it.

    SQL is written with `?` placeholders throughout and translated to `%s`
    for Postgres. Rows come back as mappings either way, so `row["name"]`
    works regardless of backend.
    """

    def __init__(self):
        self.pg = using_postgres()
        if self.pg:
            import psycopg2
            import psycopg2.extras

            self._err = psycopg2.IntegrityError
            self.conn = psycopg2.connect(
                postgres_url(),
                connect_timeout=10,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
        else:
            self._err = sqlite3.IntegrityError
            self.conn = sqlite3.connect(DB_PATH, timeout=15)
            self.conn.row_factory = sqlite3.Row
            # Autocommit, so an explicit BEGIN IMMEDIATE in lock_month() is
            # not rejected as a transaction inside a transaction.
            self.conn.isolation_level = None
            self.conn.execute("PRAGMA journal_mode=WAL")

    # -- context manager -------------------------------------------------
    def __enter__(self) -> "Db":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        return False

    @property
    def integrity_error(self):
        return self._err

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.pg else sql

    # -- queries ---------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()):
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def fetchall(self, sql: str, params: tuple = ()) -> list:
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return rows

    def fetchone(self, sql: str, params: tuple = ()):
        cur = self.execute(sql, params)
        row = cur.fetchone()
        cur.close()
        return row

    def scalar(self, sql: str, params: tuple = ()):
        row = self.fetchone(sql, params)
        if row is None:
            return None
        return list(row.values())[0] if isinstance(row, dict) else row[0]

    def insert_returning_id(self, sql: str, params: tuple) -> int:
        """INSERT that yields the new row's id on either backend."""
        if self.pg:
            cur = self.execute(sql + " RETURNING id", params)
            new_id = list(cur.fetchone().values())[0]
            cur.close()
            return int(new_id)
        cur = self.execute(sql, params)
        new_id = cur.lastrowid
        cur.close()
        return int(new_id)

    def lock_month(self, d: date) -> None:
        """
        Serialise submissions that land in the same calendar month, so the
        monthly cap cannot be beaten by two people clicking at once.

        SQLite takes the whole write lock; Postgres takes a per-month
        advisory lock that is released when the transaction ends.
        """
        if self.pg:
            self.execute("SELECT pg_advisory_xact_lock(?)", (d.year * 100 + d.month,))
        else:
            self.execute("BEGIN IMMEDIATE")


def get_conn() -> Db:
    return Db()


def init_db() -> None:
    with Db() as db:
        if db.pg:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS reservations (
                    id            SERIAL PRIMARY KEY,
                    event_date    TEXT    NOT NULL,
                    day_name      TEXT    NOT NULL,
                    slot_label    TEXT    NOT NULL,
                    name          TEXT    NOT NULL,
                    email         TEXT,
                    phone         TEXT,
                    purpose       TEXT    NOT NULL,
                    num_people    INTEGER,
                    comments      TEXT,
                    status        TEXT    NOT NULL DEFAULT 'Pending',
                    submitted_at  TEXT    NOT NULL,
                    admin_note    TEXT,
                    notify_status TEXT,
                    office_status TEXT,
                    decision_emails TEXT,
                    ack_status TEXT
                )
                """
            )
        else:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS reservations (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_date    TEXT    NOT NULL,
                    day_name      TEXT    NOT NULL,
                    slot_label    TEXT    NOT NULL,
                    name          TEXT    NOT NULL,
                    email         TEXT,
                    phone         TEXT,
                    purpose       TEXT    NOT NULL,
                    num_people    INTEGER,
                    comments      TEXT,
                    status        TEXT    NOT NULL DEFAULT 'Pending',
                    submitted_at  TEXT    NOT NULL,
                    admin_note    TEXT
                )
                """
            )
            cols = {r[1] for r in db.execute("PRAGMA table_info(reservations)").fetchall()}
            for col in ("notify_status", "office_status", "decision_emails", "ack_status"):
                if col not in cols:
                    db.execute(f"ALTER TABLE reservations ADD COLUMN {col} TEXT")

        if db.pg:
            for col in ("office_status", "decision_emails", "ack_status"):
                db.execute(f"ALTER TABLE reservations ADD COLUMN IF NOT EXISTS {col} TEXT")

        # One active (pending or reserved) request per date. Declined rows are
        # ignored so a date frees up again if you turn a request down. Both
        # engines support partial unique indexes.
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_date
            ON reservations(event_date)
            WHERE status IN ('Pending', 'Reserved')
            """
        )


def slot_for(d: date) -> dict | None:
    return SLOTS.get(d.weekday())


def bookable_dates(start: date, end: date) -> list[date]:
    out, cur = [], start
    while cur <= end:
        if cur.weekday() in SLOTS:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def status_map(start: date, end: date, db: "Db | None" = None) -> dict[str, dict]:
    """Active reservations between two dates, keyed by ISO date string."""
    sql = """
        SELECT * FROM reservations
        WHERE status IN ('Pending', 'Reserved')
          AND event_date BETWEEN ? AND ?
    """
    params = (start.isoformat(), end.isoformat())
    if db is not None:
        rows = db.fetchall(sql, params)
    else:
        with Db() as own:
            rows = own.fetchall(sql, params)
    return {r["event_date"]: r for r in rows}


def month_bounds(d: date) -> tuple[str, str]:
    """First and last day of d's calendar month, as ISO strings."""
    first = d.replace(day=1)
    last = d.replace(day=pycalendar.monthrange(d.year, d.month)[1])
    return first.isoformat(), last.isoformat()


def month_counts(start: date, end: date, db: "Db | None" = None) -> dict[tuple[int, int], dict]:
    """
    Per calendar month, how many bookings are approved and how many are still
    waiting, as {(year, month): {"reserved": n, "pending": n}}, in one query.

    Only approved bookings count toward the monthly limit. Requests that are
    still pending are shown on the calendar but do not close the month, so a
    request nobody has reviewed yet cannot lock everyone else out.

    One query rather than one per candidate date: that used to be roughly 80
    round trips to build a single page.
    """
    sql = """
        SELECT substr(event_date, 1, 7) AS ym, status, COUNT(*) AS n
        FROM reservations
        WHERE status IN ('Pending', 'Reserved')
          AND event_date BETWEEN ? AND ?
        GROUP BY substr(event_date, 1, 7), status
    """
    params = (start.isoformat(), end.isoformat())
    if db is not None:
        rows = db.fetchall(sql, params)
    else:
        with Db() as own:
            rows = own.fetchall(sql, params)

    out: dict[tuple[int, int], dict] = {}
    for r in rows:
        y, m = str(r["ym"]).split("-")[:2]
        key = (int(y), int(m))
        slot = out.setdefault(key, {"reserved": 0, "pending": 0})
        slot["reserved" if r["status"] == STATUS_RESERVED else "pending"] += int(r["n"])
    return out


def _count_in_month(d: date, status: str, db: "Db | None" = None) -> int:
    first, last = month_bounds(d)
    sql = "SELECT COUNT(*) FROM reservations WHERE status = ? AND event_date BETWEEN ? AND ?"
    if db is not None:
        return int(db.scalar(sql, (status, first, last)))
    with Db() as own:
        return int(own.scalar(sql, (status, first, last)))


def reserved_count_in_month(d: date, db: "Db | None" = None) -> int:
    """Approved bookings in d's month. This is what the monthly limit counts."""
    return _count_in_month(d, STATUS_RESERVED, db)


def pending_count_in_month(d: date, db: "Db | None" = None) -> int:
    """Requests in d's month still waiting on the office."""
    return _count_in_month(d, STATUS_PENDING, db)


def active_count_in_month(d: date, db: "Db | None" = None) -> int:
    """Approved plus pending, for display."""
    return reserved_count_in_month(d, db) + pending_count_in_month(d, db)


def month_is_full(d: date) -> bool:
    """
    A month is full once the limit is reached by ACTIVE bookings, which means
    pending as well as approved.

    A request that is waiting on the office holds its place, so two unreviewed
    requests close the month to a third. Declining one frees the place again
    straight away.
    """
    return active_count_in_month(d) >= MAX_PER_MONTH


INSERT_SQL = """
    INSERT INTO reservations
        (event_date, day_name, slot_label, name, email, phone,
         purpose, num_people, comments, status, submitted_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def create_request(
    event_date: date,
    name: str,
    email: str,
    phone: str,
    purpose: str,
    num_people: int,
    comments: str,
) -> tuple[bool, str]:
    slot = slot_for(event_date)
    if slot is None:
        return False, "That date is not one of the available days."
    if event_date < date.today():
        return False, "That date has already passed."

    row_id = None
    try:
        with Db() as db:
            # Lock first, then count, then insert, all in one transaction, so
            # two simultaneous submissions cannot both pass the monthly cap.
            db.lock_month(event_date)
            if active_count_in_month(event_date, db) >= MAX_PER_MONTH:
                return False, (
                    f"{event_date.strftime('%B %Y')} already has {MAX_PER_MONTH} reservations "
                    "requested or approved, which is the limit for one month. Please choose a "
                    "date in another month."
                )
            row_id = db.insert_returning_id(
                INSERT_SQL,
                (
                    event_date.isoformat(),
                    slot["day"],
                    slot["label"],
                    name.strip(),
                    email.strip(),
                    phone.strip(),
                    purpose.strip(),
                    int(num_people),
                    comments.strip(),
                    STATUS_PENDING,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
    except Exception as exc:  # noqa: BLE001
        if _is_integrity_error(exc):
            return False, "Sorry, that date was just taken. Please pick another one."
        return False, f"Could not save the request: {exc}"

    # The reservation is safely stored. Notifying is best effort from here on,
    # so a mail problem can never cost someone their booking.
    if row_id is not None:
        send_requester_ack(row_id)         # "we got it, it is pending"
        notify_new_request(row_id)         # heads-up to the organiser
        send_office_request_email(row_id)  # the one with Approve / Decline buttons
    return True, "Request submitted."


def _is_integrity_error(exc: Exception) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    try:
        import psycopg2

        return isinstance(exc, psycopg2.IntegrityError)
    except Exception:
        return False


def all_reservations(statuses: tuple[str, ...] | None = None) -> pd.DataFrame:
    query = "SELECT * FROM reservations"
    params: tuple = ()
    if statuses:
        query += " WHERE status IN (%s)" % ",".join("?" * len(statuses))
        params = statuses
    query += " ORDER BY event_date ASC, id ASC"
    with Db() as db:
        rows = db.fetchall(query, params)
    cols = [
        "id", "event_date", "day_name", "slot_label", "name", "email", "phone",
        "purpose", "num_people", "comments", "status", "submitted_at",
        "admin_note", "notify_status", "office_status", "decision_emails", "ack_status",
    ]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([dict(r) for r in rows])


def get_reservation(res_id: int):
    with Db() as db:
        return db.fetchone("SELECT * FROM reservations WHERE id = ?", (res_id,))


def set_status(res_id: int, status: str, note: str = "") -> tuple[bool, str]:
    """
    Change one reservation's status.

    Approving is the contended operation now that only approvals count toward
    the monthly limit: several pending requests can exist for a month with one
    place left, and two people approving at the same moment must not both get
    in. So the limit is re-checked inside a locked transaction, the same way a
    new request is.
    """
    row = get_reservation(res_id)
    if row is None:
        return False, "That reservation no longer exists."

    d = datetime.fromisoformat(row["event_date"]).date()
    # A pending request already occupies its place, so approving it never adds
    # to the month's total. Only bringing a declined request back to life does.
    approving = (
        status == STATUS_RESERVED and row["status"] not in ACTIVE_STATUSES
    )

    try:
        with Db() as db:
            if approving:
                db.lock_month(d)
                if active_count_in_month(d, db) >= MAX_PER_MONTH:
                    return False, (
                        f"{d.strftime('%B %Y')} already has {MAX_PER_MONTH} reservations "
                        "requested or approved, which is the limit. Decline one of those "
                        "before approving this."
                    )
            db.execute(
                "UPDATE reservations SET status = ?, admin_note = ? WHERE id = ?",
                (status, note, res_id),
            )
    except Exception as exc:  # noqa: BLE001
        if _is_integrity_error(exc):
            return False, "Another active reservation already exists for that date."
        return False, f"Could not update: {exc}"
    return True, f"Updated to {status}."


def delete_reservation(res_id: int) -> None:
    with Db() as db:
        db.execute("DELETE FROM reservations WHERE id = ?", (res_id,))

# --------------------------------------------------------------------------
# Calendar rendering
# --------------------------------------------------------------------------


def month_html(
    year: int,
    month: int,
    taken: dict[str, sqlite3.Row],
    show_details: bool,
    month_full: bool = False,
) -> str:
    today = date.today()
    cal = pycalendar.Calendar(firstweekday=6)  # weeks start on Sunday

    css = """
    <style>
      .cal-wrap { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; }
      table.cal { width: 100%; border-collapse: separate; border-spacing: 4px; table-layout: fixed; }
      table.cal th { font-size: 0.75rem; text-transform: uppercase; letter-spacing: .04em;
                     color: #666; font-weight: 600; padding-bottom: 4px; }
      table.cal td { vertical-align: top; height: 78px; border-radius: 8px; padding: 6px 7px;
                     border: 1px solid rgba(0,0,0,.06); }
      .daynum { font-size: 0.85rem; font-weight: 700; line-height: 1; }
      .badge  { display: inline-block; margin-top: 5px; font-size: 0.68rem; font-weight: 700;
                letter-spacing: .02em; }
      .slot   { font-size: 0.62rem; color: #555; margin-top: 3px; line-height: 1.25; }
      .detail { font-size: 0.64rem; margin-top: 3px; line-height: 1.3; color: #333;
                overflow-wrap: anywhere; }
      .legend { margin-top: 10px; font-size: 0.78rem; }
      .legend span { display: inline-block; margin-right: 14px; }
      .dot { display: inline-block; width: 11px; height: 11px; border-radius: 3px;
             margin-right: 5px; vertical-align: -1px; border: 1px solid rgba(0,0,0,.1); }
    </style>
    """

    head = "".join(f"<th>{d}</th>" for d in ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])
    body = ""

    for week in cal.monthdatescalendar(year, month):
        body += "<tr>"
        for d in week:
            if d.month != month:
                body += '<td style="background:#fff;border:none;"></td>'
                continue

            slot = slot_for(d)
            row = taken.get(d.isoformat())

            if slot is None:
                key = "closed"
            elif d < today:
                key = "past"
            elif row is None:
                key = "full" if month_full else "available"
            elif row["status"] == STATUS_RESERVED:
                key = "reserved"
            else:
                key = "pending"

            bg, fg, text = COLORS[key]
            cell = f'<div class="daynum" style="color:{fg}">{d.day}</div>'

            if slot is not None:
                if key in ("available", "pending", "reserved", "full"):
                    cell += f'<div class="badge" style="color:{fg}">{text}</div>'
                    cell += f'<div class="slot">{slot["label"]}</div>'
                else:  # past
                    cell += f'<div class="slot" style="color:#c4c4c4">{slot["label"]}</div>'

                if show_details and row is not None:
                    cell += (
                        f'<div class="detail"><b>{row["name"]}</b><br>{row["purpose"]}'
                        f'<br>{row["num_people"]} people</div>'
                    )

            body += f'<td style="background:{bg}">{cell}</td>'
        body += "</tr>"

    legend = (
        '<div class="legend">'
        + "".join(
            f'<span><i class="dot" style="background:{COLORS[k][0]};'
            f'border-color:{COLORS[k][1]}"></i>{COLORS[k][2]}</span>'
            for k in ("available", "pending", "reserved", "full")
        )
        + '<span><i class="dot" style="background:#f5f5f5"></i>Not a bookable day</span>'
        + "</div>"
    )

    title = f"{pycalendar.month_name[month]} {year}"
    return (
        f'{css}<div class="cal-wrap"><h4 style="margin:0 0 8px 0">{title}</h4>'
        f'<table class="cal"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'
        f"{legend}</div>"
    )


def month_picker(key: str) -> tuple[int, int]:
    """
    Month selector with Previous and Next buttons either side of a dropdown.
    Returns (year, month). The buttons stop at the ends of the browsable range
    rather than wrapping around.
    """
    today = date.today()
    options = []
    y, m = today.year, today.month
    for _ in range(MONTHS_AHEAD + 1):
        options.append((y, m))
        m += 1
        if m == 13:
            m, y = 1, y + 1
    labels = [f"{pycalendar.month_name[m]} {y}" for y, m in options]

    sel_key = f"{key}_sel"
    if sel_key not in st.session_state:
        st.session_state[sel_key] = labels[0]
    idx = labels.index(st.session_state[sel_key])

    st.markdown("**Month**")
    c_prev, c_sel, c_next = st.columns([1.2, 4, 1.2], vertical_alignment="center")

    if c_prev.button(
        "◀ Previous", key=f"{key}_prev", disabled=idx == 0, use_container_width=True
    ):
        st.session_state[sel_key] = labels[idx - 1]
        st.rerun()

    if c_next.button(
        "Next ▶", key=f"{key}_next", disabled=idx >= len(labels) - 1, use_container_width=True
    ):
        st.session_state[sel_key] = labels[idx + 1]
        st.rerun()

    choice = c_sel.selectbox(
        "Month", labels, key=sel_key, label_visibility="collapsed"
    )
    return options[labels.index(choice)]


def render_calendar(show_details: bool, key: str) -> None:
    year, month = month_picker(key)
    first = date(year, month, 1)
    last = date(year, month, pycalendar.monthrange(year, month)[1])

    # One connection, two queries, for the whole grid.
    with Db() as db:
        taken = status_map(first, last, db)
        c = month_counts(first, last, db).get((year, month), {"reserved": 0, "pending": 0})
    used, waiting = c["reserved"], c["pending"]

    taken_places = used + waiting
    label = (
        f"{taken_places} of {MAX_PER_MONTH} places taken in "
        f"{pycalendar.month_name[month]} {year}"
    )
    if waiting:
        label += f" ({used} approved, {waiting} awaiting a decision)"
    st.caption(label)
    st.markdown(
        month_html(year, month, taken, show_details, month_full=taken_places >= MAX_PER_MONTH),
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------
# Email drafting
# --------------------------------------------------------------------------


def _email_parts(row) -> tuple[str, str]:
    """Build the subject and body for one reservation."""
    d = datetime.fromisoformat(row["event_date"]).date()
    pretty = d.strftime("%A, %B %d, %Y")

    subject = f"Building reservation request, {row['name']}, {pretty}"

    greeting = f"Dear {EMAIL_GREETING_NAME}," if EMAIL_GREETING_NAME else "Good morning,"

    lines = [
        greeting,
        "",
        "I hope this message finds you well. A request to reserve the church building "
        "came in through the online reservation form, and I am passing along the details "
        "for the office calendar.",
        "",
        f"Date:              {pretty}",
        f"Time:              {row['slot_label']}",
        f"Requested by:      {row['name']}",
        f"Purpose:           {row['purpose']}",
        f"Number attending:  {row['num_people']}",
        f"Email:             {row['email'] or 'not provided'}",
        f"Phone:             {row['phone'] or 'not provided'}",
    ]
    if row["comments"]:
        lines.append(f"Additional notes:  {row['comments']}")
    lines += [
        f"Submitted:         {str(row['submitted_at']).replace('T', ' ')}",
        "",
        f"The request is currently marked as {row['status'].lower()}, and the requester has "
        "been told that it is not confirmed until the church approves it.",
        "",
        "Could you let me know whether the building is available at that time, and whether "
        "anything needs to be arranged on your end before I confirm with them? I am glad to "
        "follow up with the requester directly once I hear back from you.",
        "",
        "Thank you for your help.",
        "",
        "Kind regards,",
        EMAIL_SIGNOFF,
    ]
    return subject, "\n".join(lines)


def _secret(name: str) -> str | None:
    """Read a value from st.secrets, falling back to the environment."""
    try:
        val = st.secrets.get(name)
        if val:
            return str(val)
    except Exception:
        pass
    return os.environ.get(name.upper())


def office_email() -> str:
    """
    Where the Approve / Decline email goes.

    Overridable so a live trial can be pointed at your own inbox for one run,
    without editing code that might then get committed by accident:

        OFFICE_EMAIL=you@example.com streamlit run app.py
    """
    return _secret("office_email") or EMAIL_TO


def notify_email() -> str:
    """Where the organiser's copies go."""
    return _secret("notify_email") or NOTIFY_EMAIL


def is_test_routing() -> bool:
    """True whenever the office email is going anywhere but the real office."""
    return office_email() != REAL_OFFICE_EMAIL


def app_password() -> str | None:
    """
    The Gmail app password, with every space removed.

    Google displays the 16-character code in four groups, and copying it
    brings the separators along. Some of those are non-breaking spaces
    (U+00A0), which smtplib cannot encode as ASCII, so a pasted-as-shown
    code fails with a UnicodeEncodeError before it ever reaches Gmail.
    str.split() treats U+00A0 as whitespace, so this handles both kinds.
    """
    raw = _secret("gmail_app_password")
    if not raw:
        return None
    cleaned = "".join(raw.split())
    try:
        cleaned.encode("ascii")
    except UnicodeEncodeError:
        return None
    return cleaned or None


def notify_new_request(res_id: int) -> tuple[bool, str]:
    """
    Email the organiser the moment a request is submitted.

    Never raises. If no app password is configured, or the mail server is
    unreachable, the reservation still stands and the outcome is recorded on
    the row so the admin page can show it.
    """
    row = get_reservation(res_id)
    if row is None:
        return False, "Reservation not found."

    if not app_password():
        with closing_note(res_id, "notify_status", "not configured"):
            pass
        return False, "No app password configured, notification skipped."

    d = datetime.fromisoformat(row["event_date"]).date()
    lines = [
        "A new reservation request was submitted through the scheduler.",
        "",
        f"Date:              {d.strftime('%A, %B %d, %Y')}",
        f"Time:              {row['slot_label']}",
        f"Requested by:      {row['name']}",
        f"Purpose:           {row['purpose']}",
        f"Number attending:  {row['num_people']}",
        f"Email:             {row['email'] or 'not provided'}",
        f"Phone:             {row['phone'] or 'not provided'}",
    ]
    if row["comments"]:
        lines.append(f"Comments:          {row['comments']}")
    lines += [
        f"Submitted:         {str(row['submitted_at']).replace('T', ' ')}",
        "",
        f"{d.strftime('%B %Y')}: {active_count_in_month(d)} of {MAX_PER_MONTH} places taken "
        f"({reserved_count_in_month(d)} approved, {pending_count_in_month(d)} awaiting a decision).",
        "",
        f"The office has been emailed with Approve and Decline buttons. You will get "
        f"another message once {EMAIL_GREETING_NAME or 'the office'} decides.",
    ]

    subject = f"New request: {row['name']}, {d.strftime('%a %b %d, %Y')}, {row['slot_label']}"
    ok, msg = send_email(notify_email(), subject, "\n".join(lines), reply_to=row["email"] or None)
    state = f"sent {datetime.now().strftime('%Y-%m-%d %H:%M')}" if ok else f"failed: {msg[:60]}"
    with closing_note(res_id, "notify_status", state):
        pass
    return ok, msg


def send_test_email() -> tuple[bool, str]:
    """Prove the mail settings work without needing a real request."""
    return send_email(
        notify_email(),
        "Test from the Calvary Lutheran Church scheduler",
        "This is a test message from the reservation scheduler.\n\n"
        "If you are reading it, automatic notifications are working. You will get "
        "one of these each time somebody submits a request, and the church office "
        "will get one with Approve and Decline buttons.",
    )


def gmail_compose_url(row) -> str:
    """A Gmail compose window, pre-filled and ready to review and send."""
    subject, body = _email_parts(row)
    params = {"view": "cm", "fs": "1", "to": office_email(), "su": subject, "body": body}
    if EMAIL_CC:
        params["cc"] = EMAIL_CC
    if EMAIL_FROM:
        params["authuser"] = EMAIL_FROM
    return "https://mail.google.com/mail/?" + urlencode(params, quote_via=quote)


def mailto_url(row) -> str:
    """Fallback for whatever mail app the computer uses by default."""
    subject, body = _email_parts(row)
    params = {"subject": subject, "body": body}
    if EMAIL_CC:
        params["cc"] = EMAIL_CC
    return f"mailto:{office_email()}?" + urlencode(params, quote_via=quote)


def draft_email_controls(row, key: str) -> None:
    """Draft button plus a preview expander for one reservation."""
    subject, body = _email_parts(row)
    a, b = st.columns([1, 1])
    a.link_button("Draft email to the office", gmail_compose_url(row), type="secondary")
    b.link_button("Use my default mail app", mailto_url(row))
    with st.expander("Preview the email"):
        st.caption(f"From {EMAIL_FROM} to {office_email()}")
        st.text_input("Subject", subject, disabled=True, key=f"subj_{key}")
        st.code(body, language=None)




# --------------------------------------------------------------------------
# Approve / decline by email
# --------------------------------------------------------------------------


def base_url() -> str:
    return (_secret("app_base_url") or DEFAULT_BASE_URL).rstrip("/")


def _signing_key() -> bytes:
    """
    Key for signing the Approve and Decline links.

    Uses the decision_secret if set, otherwise falls back to the admin
    password so the feature works without extra configuration. Either way the
    key never appears in a link; only a signature derived from it does.
    """
    raw = _secret("decision_secret") or _secret("admin_password") or "calvary-fallback-key"
    return hashlib.sha256(raw.encode("utf-8")).digest()


def action_token(res_id: int, action: str) -> str:
    """
    Signature proving a link came from us.

    Tied to the reservation id AND the action, so an Approve link cannot be
    edited into a Decline link, and neither works for a different booking.
    """
    msg = f"{res_id}:{action}".encode("utf-8")
    return hmac.new(_signing_key(), msg, hashlib.sha256).hexdigest()[:32]


def verify_token(res_id: int, action: str, token: str) -> bool:
    if not token:
        return False
    return hmac.compare_digest(action_token(res_id, action), token)


def decision_url(res_id: int, action: str) -> str:
    params = urlencode({"r": res_id, "a": action, "t": action_token(res_id, action)})
    return f"{base_url()}/?{params}"


def send_email(
    to: str,
    subject: str,
    body: str,
    html: str | None = None,
    reply_to: str | None = None,
    cc: str | None = None,
) -> tuple[bool, str]:
    """
    Send one message. Never raises, so a mail problem cannot break a booking.
    """
    password = app_password()
    if not password:
        return False, "No app password configured."
    if not to:
        return False, "No recipient."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        if SMTP_SSL:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.login(SMTP_USER, password)
                s.send_message(msg)
        else:  # local capture server in tests
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
                s.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return True, "sent"


def _button(url: str, label: str, colour: str) -> str:
    return (
        f'<a href="{url}" style="background:{colour};color:#ffffff;text-decoration:none;'
        f'padding:13px 30px;border-radius:6px;font-weight:600;font-size:15px;'
        f'display:inline-block;font-family:-apple-system,Segoe UI,Roboto,sans-serif">'
        f"{label}</a>"
    )


def office_email_parts(row) -> tuple[str, str, str]:
    """Subject, plain text and HTML for the message that goes to the office."""
    d = datetime.fromisoformat(row["event_date"]).date()
    pretty = d.strftime("%A, %B %d, %Y")
    rid = int(row["id"])
    approve, decline = decision_url(rid, "approve"), decision_url(rid, "decline")

    subject = f"Building reservation request, {row['name']}, {pretty}"

    rows = [
        ("Date", pretty),
        ("Time", row["slot_label"]),
        ("Requested by", row["name"]),
        ("Purpose", row["purpose"]),
        ("Number attending", str(row["num_people"])),
        ("Email", row["email"] or "not provided"),
        ("Phone", row["phone"] or "not provided"),
    ]
    if row["comments"]:
        rows.append(("Comments", row["comments"]))

    greeting = f"Dear {EMAIL_GREETING_NAME}," if EMAIL_GREETING_NAME else "Good morning,"
    widest = max(len(k) for k, _ in rows) + 2
    detail_text = "\n".join(f"{k + ':':<{widest}}{v}" for k, v in rows)

    text = f"""{greeting}

A request to reserve the church building came in through the online reservation form.

{detail_text}

Please approve or decline using one of these links:

  Approve:  {approve}

  Decline:  {decline}

Either link opens a short confirmation page, so an accidental click cannot
book or cancel anything on its own. Once you confirm, {EMAIL_SIGNOFF} and the
person who asked are both notified automatically.

Thank you,
{EMAIL_SIGNOFF}
"""

    detail_html = "".join(
        f'<tr><td style="padding:5px 18px 5px 0;color:#666;white-space:nowrap;'
        f'vertical-align:top">{k}</td>'
        f'<td style="padding:5px 0;color:#111"><b>{v}</b></td></tr>'
        for k, v in rows
    )

    html = f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;
color:#111;line-height:1.55;max-width:600px">
  <p>{greeting}</p>
  <p>A request to reserve the church building came in through the online reservation form.</p>
  <table style="border-collapse:collapse;font-size:15px;margin:18px 0">{detail_html}</table>
  <p style="margin:26px 0 10px 0"><b>Is the building available?</b></p>
  <p>{_button(approve, "&#10003;&nbsp; Approve", "#2e7d32")}
     &nbsp;&nbsp;&nbsp;
     {_button(decline, "&#10007;&nbsp; Decline", "#c62828")}</p>
  <p style="color:#666;font-size:13px;margin-top:22px">
    Either button opens a short confirmation page, so an accidental click cannot book
    or cancel anything on its own. Once you confirm, {EMAIL_SIGNOFF} and the person who
    asked are both notified automatically.
  </p>
  <p style="color:#666;font-size:13px">If the buttons do not work, copy this link:<br>
    <span style="word-break:break-all">{approve}</span></p>
  <p>Thank you,<br>{EMAIL_SIGNOFF}</p>
</body></html>"""
    return subject, text, html


def send_requester_ack(res_id: int) -> tuple[bool, str]:
    """
    Tell the requester we have their request.

    Sent the moment they submit, so nobody is left wondering whether the form
    worked. It is careful to promise nothing: the date is not theirs until the
    office approves it.
    """
    row = get_reservation(res_id)
    if row is None:
        return False, "Reservation not found."
    if not row["email"]:
        with closing_note(res_id, "ack_status", "no email given"):
            pass
        return False, "No requester email."

    d = datetime.fromisoformat(row["event_date"]).date()
    pretty = d.strftime("%A, %B %d, %Y")

    text = f"""Dear {row['name']},

Thank you. We have received your request to use {CHURCH_NAME}, and it is now
waiting for the church office to review.

Date:              {pretty}
Time:              {row['slot_label']}
Purpose:           {row['purpose']}
Number attending:  {row['num_people']}

Please note this is a request, not a confirmed booking. The date is held as
pending and is not yours until the office approves it. You will get another
email either way, usually within a few days.

If you need to change or withdraw the request, simply reply to this message.

Kind regards,
{EMAIL_SIGNOFF}
{CHURCH_NAME}
"""

    rows_html = "".join(
        f'<tr><td style="padding:4px 16px 4px 0;color:#666">{k}</td>'
        f'<td style="padding:4px 0"><b>{v}</b></td></tr>'
        for k, v in [
            ("Date", pretty), ("Time", row["slot_label"]),
            ("Purpose", row["purpose"]), ("Number attending", row["num_people"]),
        ]
    )
    html = f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;
color:#111;line-height:1.55;max-width:600px">
<p>Dear {row['name']},</p>
<p>Thank you. We have received your request to use {CHURCH_NAME}, and it is now
waiting for the church office to review.</p>
<table style="border-collapse:collapse;font-size:15px;margin:16px 0">{rows_html}</table>
<p style="background:#fff4e0;border-left:4px solid #b26a00;padding:11px 14px;margin:18px 0">
<b>This is a request, not a confirmed booking.</b> The date is held as pending and is
not yours until the office approves it. You will get another email either way,
usually within a few days.</p>
<p>If you need to change or withdraw the request, simply reply to this message.</p>
<p>Kind regards,<br>{EMAIL_SIGNOFF}<br>{CHURCH_NAME}</p>
</body></html>"""

    ok, msg = send_email(
        row["email"],
        f"We received your request for {pretty}, {CHURCH_NAME}",
        text, html=html, reply_to=notify_email(),
    )
    with closing_note(res_id, "ack_status", "sent" if ok else f"failed: {msg[:60]}"):
        pass
    return ok, msg


def send_office_request_email(res_id: int) -> tuple[bool, str]:
    row = get_reservation(res_id)
    if row is None:
        return False, "Reservation not found."
    subject, text, html = office_email_parts(row)
    ok, msg = send_email(
        office_email(), subject, text, html=html, reply_to=row["email"] or None,
        cc=EMAIL_CC or None
    )
    with closing_note(res_id, "office_status", "sent" if ok else f"failed: {msg[:60]}"):
        pass
    return ok, msg


@contextmanager
def closing_note(res_id: int, column: str, value: str):
    """Record a per-reservation status column without ever raising."""
    try:
        with Db() as db:
            db.execute(f"UPDATE reservations SET {column} = ? WHERE id = ?", (value, res_id))
    except Exception:
        pass
    yield


def decision_email_parts(row, approved: bool, for_requester: bool) -> tuple[str, str, str]:
    d = datetime.fromisoformat(row["event_date"]).date()
    pretty = d.strftime("%A, %B %d, %Y")
    when = f"{pretty}, {row['slot_label']}"
    word = "approved" if approved else "declined"

    if for_requester:
        subject = f"Reservation {word}: {d.strftime('%b %d, %Y')}, {CHURCH_NAME}"
        if approved:
            text = f"""Dear {row['name']},

Good news. Your request to use {CHURCH_NAME} has been approved.

Date:     {pretty}
Time:     {row['slot_label']}
Purpose:  {row['purpose']}
Expected: {row['num_people']} people

The date is now reserved for you on the church calendar. If anything about
your plans changes, please reply to this message so we can update it.

We look forward to hosting you.

Kind regards,
{EMAIL_SIGNOFF}
{CHURCH_NAME}
"""
        else:
            text = f"""Dear {row['name']},

Thank you for your interest in using {CHURCH_NAME}. Unfortunately your
request for {when} could not be approved.

Please book another day.

You can see what is still open and submit a new request here:
{base_url()}

We are sorry for the inconvenience and hope to host you another time.

Kind regards,
{EMAIL_SIGNOFF}
{CHURCH_NAME}
"""
    else:
        subject = f"{word.capitalize()}: {row['name']}, {pretty}"
        text = f"""The office has {word} a reservation request.

Date:         {pretty}
Time:         {row['slot_label']}
Requested by: {row['name']}
Purpose:      {row['purpose']}
Attending:    {row['num_people']}
Email:        {row['email'] or 'not provided'}
Phone:        {row['phone'] or 'not provided'}

{"The date is now marked Reserved on the calendar." if approved
 else "The date has been released and is open for other requests again."}
{row['name']} has been emailed about this decision.
"""

    colour = "#2e7d32" if approved else "#c62828"
    body_html = text.replace("\n\n", "</p><p>").replace("\n", "<br>")
    html = f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;
color:#111;line-height:1.55;max-width:600px">
<p style="font-size:17px;font-weight:700;color:{colour}">Request {word}</p>
<p>{body_html}</p></body></html>"""
    return subject, text, html


def send_decision_emails(res_id: int, approved: bool) -> tuple[bool, str]:
    """
    Send the outcome emails for one decision and report honestly.

    Returns (everything_sent, human readable detail). The detail is kept on
    the row and shown in the app, because a silent mail failure is worse than
    a loud one: the date changes either way and nobody finds out for days.
    """
    row = get_reservation(res_id)
    if row is None:
        return False, "That reservation no longer exists."

    if not app_password():
        detail = "no gmail_app_password configured, so nothing could be sent"
        with closing_note(res_id, "decision_emails", detail):
            pass
        return False, detail

    problems, notes = [], []

    if row["email"]:
        s_, t_, h_ = decision_email_parts(row, approved, for_requester=True)
        ok_r, why_r = send_email(row["email"], s_, t_, html=h_)
        notes.append(f"requester {'ok' if ok_r else 'FAILED'}")
        if not ok_r:
            problems.append(f"requester ({row['email']}): {why_r}")
    else:
        notes.append("requester skipped, no address given")
        problems.append("the requester gave no email address")

    s_, t_, h_ = decision_email_parts(row, approved, for_requester=False)
    ok_o, why_o = send_email(notify_email(), s_, t_, html=h_, reply_to=row["email"] or None)
    notes.append(f"organiser {'ok' if ok_o else 'FAILED'}")
    if not ok_o:
        problems.append(f"organiser ({notify_email()}): {why_o}")

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    detail = f"{stamp}: " + ", ".join(notes)
    if problems:
        detail += " | " + "; ".join(problems)
    with closing_note(res_id, "decision_emails", detail[:400]):
        pass

    if problems:
        return False, "; ".join(problems)
    return True, "Requester and organiser have both been emailed."


def decide(res_id: int, approved: bool, note: str = "", notify: bool = True) -> tuple[bool, str]:
    """
    Apply an approve or decline decision and tell everyone who needs to know.

    Used by both the buttons in the office email and the admin page, so the
    two routes can never drift apart.

    The returned flag covers the WHOLE job, the status change and the emails.
    An earlier version returned success as soon as the status changed and
    threw the mail result away, so a failed send looked like a clean approval.
    """
    row = get_reservation(res_id)
    if row is None:
        return False, "That reservation no longer exists."

    target = STATUS_RESERVED if approved else STATUS_DECLINED

    if row["status"] == target:
        # Already in the right state. Do not change anything, but the emails
        # may well be why we are here, so offer to send them again.
        if notify:
            ok, detail = send_decision_emails(res_id, approved)
            return ok, (
                f"Already {target.lower()}. {detail}" if ok
                else f"Already {target.lower()}, but the emails could not be sent: {detail}"
            )
        return True, f"already {target.lower()}"

    ok, msg = set_status(res_id, target, note)
    if not ok:
        return False, msg

    if not notify:
        return True, f"{target.lower()}"

    sent_ok, detail = send_decision_emails(res_id, approved)
    if sent_ok:
        return True, f"{target.capitalize()}. {detail}"
    return False, (
        f"The reservation was marked {target.lower()}, but the emails could not be "
        f"sent: {detail}"
    )


def page_decision() -> bool:
    """
    Landing page for the Approve and Decline links in the office email.

    Returns True when it handled the request, so main() can skip the normal
    pages. Nothing is changed until the confirmation button is pressed, which
    keeps mail scanners and link previewers from deciding on their own.
    """
    qp = st.query_params
    rid_raw, action, token = qp.get("r"), qp.get("a"), qp.get("t")
    if not rid_raw or action not in ("approve", "decline"):
        return False

    st.subheader("Reservation decision")

    try:
        rid = int(rid_raw)
    except (TypeError, ValueError):
        st.error("That link is not valid.")
        return True

    if not verify_token(rid, action, token or ""):
        st.error(
            "That link is not valid or has expired. Please use the buttons in the most "
            "recent email, or ask the office to resend it."
        )
        return True

    row = get_reservation(rid)
    if row is None:
        st.error("That reservation no longer exists.")
        return True

    d = datetime.fromisoformat(row["event_date"]).date()
    approved = action == "approve"
    verb = "Approve" if approved else "Decline"

    with st.container(border=True):
        st.markdown(
            f"**{d.strftime('%A, %B %d, %Y')}**, {row['slot_label']}  \n"
            f"**{row['name']}**, {row['purpose']}, {row['num_people']} people  \n"
            f"{row['email'] or 'no email'}  {('| ' + row['phone']) if row['phone'] else ''}"
        )
        if row["comments"]:
            st.caption(f"Comments: {row['comments']}")

    done_key = f"decided_{rid}_{action}"
    if st.session_state.get(done_key):
        outcome = st.session_state.get(done_key)
        if outcome is True:
            st.success(
                f"Done. This request is now **{row['status']}**, and the requester and "
                f"{EMAIL_SIGNOFF} have both been emailed."
            )
        else:
            st.warning(
                f"This request is now **{row['status']}**, but the notification emails "
                f"could not be sent:\n\n{outcome}"
            )
            if st.button("Try sending the emails again"):
                ok2, why2 = send_decision_emails(rid, approved)
                st.session_state[done_key] = True if ok2 else why2
                st.rerun()
        st.link_button("Open the scheduler", base_url())
        return True

    if row["status"] != STATUS_PENDING:
        st.info(
            f"This request is already marked **{row['status']}**, so there is nothing to do. "
            "If that is wrong, sign in to the Admin page to change it."
        )
        st.link_button("Open the scheduler", base_url())
        return True

    st.write(f"Confirm that you want to **{verb.lower()}** this request.")
    c1, c2 = st.columns([1, 3])
    if c1.button(f"{verb} this request", type="primary"):
        with st.spinner("Saving and sending emails..."):
            ok, msg = decide(rid, approved, note=f"{verb}d from the office email")
        # ok covers the emails as well as the status, so a mail failure is
        # recorded rather than being shown as a clean success.
        st.session_state[done_key] = True if ok else msg
        st.rerun()
    c2.caption("Nothing has been changed yet. Nobody is emailed until you press the button.")
    return True


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


def page_request() -> None:
    st.subheader("Request a reservation")
    st.write(
        "Choose a date, choose the time slot for that day, fill out the details, "
        "and we will follow up to confirm. Dates already showing as **Pending** or "
        "**Reserved** on the calendar below cannot be requested."
    )

    today = date.today()
    horizon = today + timedelta(days=31 * MONTHS_AHEAD)

    # Two queries on one connection for the whole page: which dates are taken,
    # and how many bookings each month already holds.
    # Count whole calendar months, so a booking late in the final month is not
    # missed just because the browsing horizon lands mid-month.
    horizon_end = horizon.replace(day=pycalendar.monthrange(horizon.year, horizon.month)[1])

    with Db() as db:
        taken = status_map(today, horizon, db)
        counts = month_counts(today.replace(day=1), horizon_end, db)

    full_months = {
        ym for ym, c in counts.items()
        if c["reserved"] + c["pending"] >= MAX_PER_MONTH
    }

    open_dates = [
        d
        for d in bookable_dates(today, horizon)
        if d.isoformat() not in taken and (d.year, d.month) not in full_months
    ]

    if not open_dates:
        st.warning(
            "There are no open dates in the next few months. Every month is either fully "
            f"booked or already at the limit of {MAX_PER_MONTH} reservations. "
            "Please check back later."
        )
        availability_section()
        return

    # ---- Date and time slot pickers (outside the form so the availability
    #      message updates the moment something is changed) ----
    if "req_date" not in st.session_state:
        st.session_state["req_date"] = open_dates[0]
    if "req_slot" not in st.session_state:
        st.session_state["req_slot"] = open_dates[0].weekday()

    def sync_slot_to_date() -> None:
        d = st.session_state.get("req_date")
        if isinstance(d, date) and d.weekday() in SLOTS:
            st.session_state["req_slot"] = d.weekday()

    pick_l, pick_r = st.columns([1, 1])

    with pick_l:
        st.markdown("**1. Choose a date**")
        chosen = st.date_input(
            "Date",
            key="req_date",
            min_value=today,
            max_value=horizon,
            format="MM/DD/YYYY",
            on_change=sync_slot_to_date,
            label_visibility="collapsed",
        )

    with pick_r:
        st.markdown("**2. Choose a time**")
        slot_weekday = st.radio(
            "Time",
            options=list(SLOTS.keys()),
            key="req_slot",
            format_func=lambda w: f"{SLOTS[w]['day']}, {SLOTS[w]['label']}",
            label_visibility="collapsed",
        )

    # Live status for the current date + time combination.
    picked_slot = slot_for(chosen)
    if picked_slot is None:
        blocker = (
            f"**{chosen.strftime('%A, %B %d, %Y')}** is a {chosen.strftime('%A')}. "
            "The building is only available on Fridays, Saturdays and Sundays."
        )
        st.error(blocker)
    elif chosen.weekday() != slot_weekday:
        blocker = (
            f"You picked a {chosen.strftime('%A')} date but the "
            f"**{SLOTS[slot_weekday]['day']}** time slot. Pick a {SLOTS[slot_weekday]['day']} date, "
            f"or switch the time to {picked_slot['day']}, {picked_slot['label']}"
        )
        st.warning(blocker)
    elif chosen.isoformat() in taken:
        blocker = (
            f"**{chosen.strftime('%A, %B %d, %Y')}** is already "
            f"{taken[chosen.isoformat()]['status'].lower()}. Please choose another date."
        )
        st.error(blocker)
    elif (chosen.year, chosen.month) in full_months:
        blocker = (
            f"**{chosen.strftime('%B %Y')}** already has {MAX_PER_MONTH} reservations requested "
            "or approved, which is the limit for one month. Please pick a date in a "
            "different month."
        )
        st.error(blocker)
    else:
        blocker = None
        c = counts.get((chosen.year, chosen.month), {"reserved": 0, "pending": 0})
        left = MAX_PER_MONTH - (c["reserved"] + c["pending"])
        note = f"{left} of {MAX_PER_MONTH} places left in {chosen.strftime('%B')}"
        if c["pending"]:
            note += (
                f", since {c['pending']} other request"
                f"{'s are' if c['pending'] > 1 else ' is'} already holding a place "
                "while the office reviews"
            )
        st.success(
            f"**{chosen.strftime('%A, %B %d, %Y')}**, {picked_slot['label']} is open. "
            f"{note}. Fill out the details below."
        )

    with st.form("request_form", clear_on_submit=False):
        c1, c2 = st.columns(2)
        name = c1.text_input("Your name *")
        num_people = c2.number_input("Number of people *", min_value=1, max_value=1000, value=25, step=1)

        c3, c4 = st.columns(2)
        email = c3.text_input("Email *")
        phone = c4.text_input("Phone")

        purpose = st.text_input("Purpose of the event *", placeholder="e.g. birthday party, bible study, memorial reception")
        comments = st.text_area("Comments", placeholder="Anything else we should know: setup needs, kitchen use, A/V, arrival time, etc.")

        agree = st.checkbox("I understand this is a request and is not confirmed until the church approves it.")
        submitted = st.form_submit_button("Submit request", type="primary")

    if submitted:
        if blocker:
            st.error("Please fix the date and time selection above before submitting.")
            availability_section()
            return

        missing = []
        if not name.strip():
            missing.append("name")
        if not email.strip() or "@" not in email:
            missing.append("a valid email")
        if not purpose.strip():
            missing.append("purpose")

        if missing:
            st.error("Please provide " + ", ".join(missing) + ".")
        elif not agree:
            st.error("Please check the acknowledgement box before submitting.")
        else:
            ok, msg = create_request(chosen, name, email, phone, purpose, int(num_people), comments)
            if ok:
                st.success(
                    f"Thank you, {name.strip()}. Your request for "
                    f"{chosen.strftime('%A, %B %d, %Y')} ({slot_for(chosen)['label']}) has been received "
                    "and is now marked **Pending** on the calendar below. "
                    "A confirmation has been emailed to you."
                )
                st.balloons()
            else:
                st.error(msg)

    availability_section()


def availability_section() -> None:
    st.divider()
    st.subheader("Availability calendar")
    st.write(
        "The building is available on **Friday 6:30-10:30 p.m.**, **Saturday 1:00-5:00 p.m.** "
        "and **Sunday 4:00-8:00 p.m.** Names and event details are not shown publicly."
    )
    render_calendar(show_details=False, key="public_month")


def check_admin_password() -> bool:
    """Password gate. Reads ADMIN_PASSWORD from st.secrets or the environment."""
    expected = None
    try:
        expected = st.secrets.get("admin_password")
    except Exception:
        pass
    if not expected:
        expected = os.environ.get("CALVARY_ADMIN_PASSWORD")

    if not expected:
        st.error(
            "No admin password is configured. Add `admin_password = \"...\"` to "
            "`.streamlit/secrets.toml`, or set the CALVARY_ADMIN_PASSWORD environment variable."
        )
        return False

    if st.session_state.get("admin_ok"):
        return True

    with st.form("login"):
        pw = st.text_input("Admin password", type="password")
        if st.form_submit_button("Sign in"):
            if pw == expected:
                st.session_state["admin_ok"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    return False


def page_admin() -> None:
    st.subheader("Admin")
    if not check_admin_password():
        return

    top = st.columns([1, 1, 1, 2])
    df_all = all_reservations()
    pending_n = int((df_all["status"] == STATUS_PENDING).sum()) if not df_all.empty else 0
    reserved_n = int((df_all["status"] == STATUS_RESERVED).sum()) if not df_all.empty else 0
    upcoming_n = 0
    if not df_all.empty:
        upcoming_n = int(
            ((df_all["event_date"] >= date.today().isoformat()) & df_all["status"].isin(ACTIVE_STATUSES)).sum()
        )
    top[0].metric("Pending", pending_n)
    top[1].metric("Reserved", reserved_n)
    top[2].metric("Upcoming", upcoming_n)
    if top[3].button("Sign out"):
        st.session_state["admin_ok"] = False
        st.rerun()

    if is_test_routing():
        st.error(
            f"**Test routing is active.** Approve / Decline emails are going to "
            f"**{office_email()}** and copies to **{notify_email()}**, not to the church "
            f"office ({REAL_OFFICE_EMAIL}). Set EMAIL_TO back to REAL_OFFICE_EMAIL in "
            f"app.py, or clear the office_email override, before real use."
        )

    if using_postgres():
        st.caption("Storage: Supabase Postgres. Reservations survive restarts and redeploys.")
    else:
        st.warning(
            "Storage: a local SQLite file. On Streamlit Community Cloud this disk is temporary, "
            "so reservations are lost whenever the app sleeps or redeploys. Add `postgres_url` "
            "to your secrets to store them permanently."
        )

    if not app_password():
        st.warning(
            "Automatic email notifications are off. Add `gmail_app_password` to your secrets "
            "to have new requests emailed to " + notify_email() + " as they arrive."
        )
    else:
        c_test, c_msg = st.columns([1, 3])
        if c_test.button("Send a test email"):
            ok, msg = send_test_email()
            if ok:
                c_msg.success(f"Test email sent to {notify_email()}.")
            else:
                c_msg.error(f"Could not send: {msg}")

    tab_cal, tab_review, tab_table = st.tabs(["Calendar", "Review requests", "All reservations"])

    with tab_cal:
        render_calendar(show_details=True, key="admin_month")

    with tab_review:
        last = st.session_state.pop("last_decision", None)
        if last:
            ok, msg = last
            (st.success if ok else st.error)(msg)
            if not ok:
                st.caption(
                    "The status was still changed. Use Resend below once the mail "
                    "problem is sorted, so the requester is not left guessing."
                )

        pending = all_reservations((STATUS_PENDING,))
        if pending.empty:
            st.info("No pending requests right now.")
        for _, r in pending.iterrows():
            d = datetime.fromisoformat(r["event_date"]).date()
            with st.container(border=True):
                st.markdown(
                    f"**{d.strftime('%A, %B %d, %Y')}**, {r['slot_label']}  \n"
                    f"**{r['name']}**, {r['purpose']}, {r['num_people']} people  \n"
                    f"{r['email']}  {('| ' + r['phone']) if r['phone'] else ''}"
                )
                if r["comments"]:
                    st.caption(f"Comments: {r['comments']}")
                st.caption(f"Submitted {r['submitted_at'].replace('T', ' ')}")

                draft_email_controls(r, key=f"pending_{r['id']}")

                note = st.text_input("Internal note (optional)", key=f"note_{r['id']}")
                b1, b2, _ = st.columns([1, 1, 4])
                if b1.button("Approve", key=f"ok_{r['id']}", type="primary"):
                    with st.spinner("Approving and sending emails..."):
                        ok, msg = decide(int(r["id"]), True, note)
                    st.session_state["last_decision"] = (ok, msg)
                    st.rerun()
                if b2.button("Decline", key=f"no_{r['id']}"):
                    with st.spinner("Declining and sending emails..."):
                        ok, msg = decide(int(r["id"]), False, note)
                    st.session_state["last_decision"] = (ok, msg)
                    st.rerun()

    with tab_table:
        if df_all.empty:
            st.info("No reservations yet.")
            return

        f1, f2 = st.columns([2, 3])
        status_filter = f1.multiselect(
            "Status", [STATUS_PENDING, STATUS_RESERVED, STATUS_DECLINED],
            default=[STATUS_PENDING, STATUS_RESERVED],
        )
        search = f2.text_input("Search name or purpose")

        view = df_all.copy()
        if status_filter:
            view = view[view["status"].isin(status_filter)]
        if search.strip():
            s = search.strip().lower()
            view = view[
                view["name"].str.lower().str.contains(s, na=False)
                | view["purpose"].str.lower().str.contains(s, na=False)
            ]

        show = view[
            ["id", "event_date", "day_name", "slot_label", "name", "purpose",
             "num_people", "email", "phone", "comments", "status", "submitted_at",
             "notify_status", "office_status", "decision_emails", "ack_status", "admin_note"]
        ].rename(
            columns={
                "id": "ID", "event_date": "Date", "day_name": "Day", "slot_label": "Time",
                "name": "Name", "purpose": "Purpose", "num_people": "People",
                "email": "Email", "phone": "Phone", "comments": "Comments",
                "status": "Status", "submitted_at": "Submitted",
                "notify_status": "Emailed you", "office_status": "Emailed office",
                "decision_emails": "Decision sent", "ack_status": "Requester ack",
                "admin_note": "Note",
            }
        )
        st.dataframe(show, hide_index=True, use_container_width=True)

        buf = io.StringIO()
        show.to_csv(buf, index=False)
        st.download_button(
            "Download CSV", buf.getvalue(),
            file_name=f"calvary_reservations_{date.today().isoformat()}.csv",
            mime="text/csv",
        )

        st.divider()
        st.markdown("**Email delivery**")
        st.caption(
            "What happened to each message for a reservation. Anything reading FAILED "
            "never reached its recipient."
        )
        ids_all = view["id"].tolist()
        if ids_all:
            def _lbl(i: int) -> str:
                rr = view[view["id"] == i].iloc[0]
                return f"#{i}, {rr['event_date']}, {rr['name']} ({rr['status']})"

            pick_mail = st.selectbox("Reservation", ids_all, format_func=_lbl, key="mailstat")
            rr = view[view["id"] == pick_mail].iloc[0]
            st.write(
                f"- Acknowledgement to requester: `{rr.get('ack_status') or 'not recorded'}`\n"
                f"- Request to the office: `{rr.get('office_status') or 'not recorded'}`\n"
                f"- Copy to you: `{rr.get('notify_status') or 'not recorded'}`\n"
                f"- Decision emails: `{rr.get('decision_emails') or 'not sent yet'}`"
            )
            if rr["status"] in (STATUS_RESERVED, STATUS_DECLINED):
                if st.button("Resend the decision emails", key=f"resend_{pick_mail}"):
                    with st.spinner("Sending..."):
                        ok_s, why_s = send_decision_emails(
                            int(pick_mail), rr["status"] == STATUS_RESERVED
                        )
                    (st.success if ok_s else st.error)(why_s)
                    st.rerun()
            else:
                st.caption("No decision has been made yet, so there is nothing to resend.")

        st.divider()
        st.markdown("**Email the office about one of these**")
        ids = view["id"].tolist()
        if ids:
            def label_for(i: int) -> str:
                r = view[view["id"] == i].iloc[0]
                return f"#{i}, {r['event_date']}, {r['name']}"

            mail_id = st.selectbox("Reservation", ids, format_func=label_for, key="mail_pick")
            draft_email_controls(view[view["id"] == mail_id].iloc[0], key=f"table_{mail_id}")

        st.divider()
        st.markdown("**Change a reservation**")
        if ids:
            c1, c2, c3 = st.columns([1, 2, 1])
            pick = c1.selectbox("ID", ids)
            new_status = c2.selectbox("Set status to", [STATUS_PENDING, STATUS_RESERVED, STATUS_DECLINED])
            if c3.button("Apply"):
                ok, msg = set_status(int(pick), new_status)
                st.toast(msg) if ok else st.error(msg)
                st.rerun()
            with st.expander("Delete a reservation permanently"):
                del_id = st.selectbox("Reservation ID to delete", ids, key="del_id")
                if st.checkbox("Yes, I am sure", key="del_ok") and st.button("Delete"):
                    delete_reservation(int(del_id))
                    st.rerun()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title=f"{CHURCH_NAME}, Event Scheduler", page_icon="⛪", layout="wide")
    init_db()

    st.title(f"{CHURCH_NAME}")
    st.caption("Event space reservation scheduler")

    # Approve / Decline links from the office email land here.
    if page_decision():
        return

    page = st.sidebar.radio("Menu", ["Request a reservation", "Admin"])
    st.sidebar.divider()
    st.sidebar.markdown(
        "**Available times**  \n"
        "Friday, 6:30-10:30 p.m.  \n"
        "Saturday, 1:00-5:00 p.m.  \n"
        "Sunday, 4:00-8:00 p.m."
    )

    if page == "Request a reservation":
        page_request()
    else:
        page_admin()


if __name__ == "__main__":
    main()
