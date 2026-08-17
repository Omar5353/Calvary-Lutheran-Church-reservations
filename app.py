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
import smtplib
import sqlite3
from contextlib import closing
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
EMAIL_FROM = "5353murad@gmail.com"     # the Gmail account the draft opens in
EMAIL_TO = "office@calvarylincoln.org"  # who the draft is addressed to
EMAIL_CC = ""                           # optional, comma separated
EMAIL_GREETING_NAME = "Leanna"          # who the message is addressed to by name
EMAIL_SIGNOFF = "Omar Murad"

# Automatic notification sent the moment a request is submitted.
NOTIFY_EMAIL = "5353murad@gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_USER = "5353murad@gmail.com"
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
                    notify_status TEXT
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
            if "notify_status" not in cols:
                db.execute("ALTER TABLE reservations ADD COLUMN notify_status TEXT")

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


def month_counts(start: date, end: date, db: "Db | None" = None) -> dict[tuple[int, int], int]:
    """
    Active bookings per calendar month, as {(year, month): count}, in a single
    query.

    This replaces asking the database once per candidate date, which meant
    roughly 80 round trips to build one page and made the request form crawl
    on a remote database.
    """
    sql = """
        SELECT substr(event_date, 1, 7) AS ym, COUNT(*) AS n
        FROM reservations
        WHERE status IN ('Pending', 'Reserved')
          AND event_date BETWEEN ? AND ?
        GROUP BY substr(event_date, 1, 7)
    """
    params = (start.isoformat(), end.isoformat())
    if db is not None:
        rows = db.fetchall(sql, params)
    else:
        with Db() as own:
            rows = own.fetchall(sql, params)

    out: dict[tuple[int, int], int] = {}
    for r in rows:
        y, m = str(r["ym"]).split("-")[:2]
        out[(int(y), int(m))] = int(r["n"])
    return out


def month_bounds(d: date) -> tuple[str, str]:
    """First and last day of d's calendar month, as ISO strings."""
    first = d.replace(day=1)
    last = d.replace(day=pycalendar.monthrange(d.year, d.month)[1])
    return first.isoformat(), last.isoformat()


def active_count_in_month(d: date, db: "Db | None" = None) -> int:
    """How many pending or reserved bookings already sit in d's month."""
    first, last = month_bounds(d)
    sql = (
        "SELECT COUNT(*) FROM reservations "
        "WHERE status IN ('Pending', 'Reserved') AND event_date BETWEEN ? AND ?"
    )
    if db is not None:
        return int(db.scalar(sql, (first, last)))
    with Db() as own:
        return int(own.scalar(sql, (first, last)))


def month_is_full(d: date) -> bool:
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
                    f"{event_date.strftime('%B %Y')} already has {MAX_PER_MONTH} reservations, "
                    "which is the limit for one month. Please choose a date in another month."
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
        notify_new_request(row_id)
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
        "admin_note", "notify_status",
    ]
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([dict(r) for r in rows])


def get_reservation(res_id: int):
    with Db() as db:
        return db.fetchone("SELECT * FROM reservations WHERE id = ?", (res_id,))


def set_status(res_id: int, status: str, note: str = "") -> tuple[bool, str]:
    row = get_reservation(res_id)
    if row is None:
        return False, "That reservation no longer exists."

    d = datetime.fromisoformat(row["event_date"]).date()

    # Bringing a declined request back to life must respect the monthly cap.
    if status in ACTIVE_STATUSES and row["status"] not in ACTIVE_STATUSES:
        if active_count_in_month(d) >= MAX_PER_MONTH:
            return False, (
                f"{d.strftime('%B %Y')} already has {MAX_PER_MONTH} active reservations. "
                "Decline one of those first."
            )

    try:
        with Db() as db:
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
        used = month_counts(first, last, db).get((year, month), 0)

    st.caption(f"{used} of {MAX_PER_MONTH} reservations used in {pycalendar.month_name[month]} {year}")
    st.markdown(
        month_html(year, month, taken, show_details, month_full=used >= MAX_PER_MONTH),
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
    Email the request details the moment it is submitted.

    Never raises. If no app password is configured, or the mail server is
    unreachable, the reservation still stands and the failure is recorded in
    the session so the admin page can show it.
    """
    def record(state: str) -> None:
        try:
            with closing(get_conn()) as conn, conn:
                conn.execute(
                    "UPDATE reservations SET notify_status = ? WHERE id = ?", (state, res_id)
                )
        except Exception:
            pass

    password = app_password()
    if not password:
        record("not configured")
        return False, "No app password configured, notification skipped."

    row = get_reservation(res_id)
    if row is None:
        return False, "Reservation not found."

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
        f"This month now holds {active_count_in_month(d)} of {MAX_PER_MONTH} allowed reservations.",
        "",
        "Open the Admin page to approve or decline it.",
    ]

    msg = EmailMessage()
    msg["Subject"] = f"New request: {row['name']}, {d.strftime('%a %b %d, %Y')}, {row['slot_label']}"
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_EMAIL
    if row["email"]:
        msg["Reply-To"] = row["email"]
    msg.set_content("\n".join(lines))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.login(SMTP_USER, password)
            s.send_message(msg)
    except Exception as exc:  # noqa: BLE001 - never block a booking on mail
        record(f"failed: {type(exc).__name__}")
        return False, str(exc)

    record(f"sent {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    return True, "Notification sent."


def send_test_email() -> tuple[bool, str]:
    """Prove the SMTP settings work without needing a real request."""
    password = app_password()
    if not password:
        return False, "No app password configured."

    msg = EmailMessage()
    msg["Subject"] = "Test from the Calvary Lutheran Church scheduler"
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_EMAIL
    msg.set_content(
        "This is a test message from the reservation scheduler.\n\n"
        "If you are reading it, automatic notifications are working and you will "
        "get one of these each time somebody submits a request."
    )
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.login(SMTP_USER, password)
            s.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return True, "Sent."


def gmail_compose_url(row) -> str:
    """A Gmail compose window, pre-filled and ready to review and send."""
    subject, body = _email_parts(row)
    params = {"view": "cm", "fs": "1", "to": EMAIL_TO, "su": subject, "body": body}
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
    return f"mailto:{EMAIL_TO}?" + urlencode(params, quote_via=quote)


def draft_email_controls(row, key: str) -> None:
    """Draft button plus a preview expander for one reservation."""
    subject, body = _email_parts(row)
    a, b = st.columns([1, 1])
    a.link_button("Draft email to the office", gmail_compose_url(row), type="secondary")
    b.link_button("Use my default mail app", mailto_url(row))
    with st.expander("Preview the email"):
        st.caption(f"From {EMAIL_FROM} to {EMAIL_TO}")
        st.text_input("Subject", subject, disabled=True, key=f"subj_{key}")
        st.code(body, language=None)


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

    full_months = {ym for ym, n in counts.items() if n >= MAX_PER_MONTH}

    open_dates = [
        d
        for d in bookable_dates(today, horizon)
        if d.isoformat() not in taken and (d.year, d.month) not in full_months
    ]

    if not open_dates:
        st.warning(
            "There are no open dates in the next few months. Every month is either fully "
            f"booked or already at the limit of {MAX_PER_MONTH} reservations. Please check back later."
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
            f"**{chosen.strftime('%B %Y')}** already has {MAX_PER_MONTH} reservations, which is "
            "the limit for one month. Please pick a date in a different month."
        )
        st.error(blocker)
    else:
        blocker = None
        left = MAX_PER_MONTH - counts.get((chosen.year, chosen.month), 0)
        st.success(
            f"**{chosen.strftime('%A, %B %d, %Y')}**, {picked_slot['label']} is open. "
            f"{left} of {MAX_PER_MONTH} reservations remaining in {chosen.strftime('%B')}. "
            "Fill out the details below."
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
                    "and is now marked **Pending** on the calendar below."
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
            "to have new requests emailed to " + NOTIFY_EMAIL + " as they arrive."
        )
    else:
        c_test, c_msg = st.columns([1, 3])
        if c_test.button("Send a test email"):
            ok, msg = send_test_email()
            if ok:
                c_msg.success(f"Test email sent to {NOTIFY_EMAIL}.")
            else:
                c_msg.error(f"Could not send: {msg}")

    tab_cal, tab_review, tab_table = st.tabs(["Calendar", "Review requests", "All reservations"])

    with tab_cal:
        render_calendar(show_details=True, key="admin_month")

    with tab_review:
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
                    ok, msg = set_status(int(r["id"]), STATUS_RESERVED, note)
                    st.toast(msg)
                    st.rerun()
                if b2.button("Decline", key=f"no_{r['id']}"):
                    ok, msg = set_status(int(r["id"]), STATUS_DECLINED, note)
                    st.toast(msg)
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
             "notify_status", "admin_note"]
        ].rename(
            columns={
                "id": "ID", "event_date": "Date", "day_name": "Day", "slot_label": "Time",
                "name": "Name", "purpose": "Purpose", "num_people": "People",
                "email": "Email", "phone": "Phone", "comments": "Comments",
                "status": "Status", "submitted_at": "Submitted",
                "notify_status": "Emailed", "admin_note": "Note",
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
