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
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta

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
    6: {"day": "Sunday", "label": "3:00-7:00 p.m.", "start": "15:00", "end": "19:00"},
}

STATUS_PENDING = "Pending"
STATUS_RESERVED = "Reserved"
STATUS_DECLINED = "Declined"
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RESERVED)

# How far ahead the public may request / browse.
MONTHS_AHEAD = 6

COLORS = {
    "available": ("#e8f5e9", "#2e7d32", "Available"),
    "pending": ("#fff4e0", "#b26a00", "Pending"),
    "reserved": ("#fdecec", "#c62828", "Reserved"),
    "closed": ("#f5f5f5", "#9e9e9e", ""),
    "past": ("#fafafa", "#c4c4c4", ""),
}


# --------------------------------------------------------------------------
# Database layer
# --------------------------------------------------------------------------


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with closing(get_conn()) as conn, conn:
        conn.execute(
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
        # One active (pending or reserved) request per date. Declined rows are
        # ignored so a date frees up again if you turn a request down.
        conn.execute(
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


def status_map(start: date, end: date) -> dict[str, sqlite3.Row]:
    """Active reservations between two dates, keyed by ISO date string."""
    with closing(get_conn()) as conn:
        rows = conn.execute(
            """
            SELECT * FROM reservations
            WHERE status IN ('Pending', 'Reserved')
              AND event_date BETWEEN ? AND ?
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return {r["event_date"]: r for r in rows}


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

    try:
        with closing(get_conn()) as conn, conn:
            conn.execute(
                """
                INSERT INTO reservations
                    (event_date, day_name, slot_label, name, email, phone,
                     purpose, num_people, comments, status, submitted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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
    except sqlite3.IntegrityError:
        return False, "Sorry, that date was just taken. Please pick another one."
    return True, "Request submitted."


def all_reservations(statuses: tuple[str, ...] | None = None) -> pd.DataFrame:
    query = "SELECT * FROM reservations"
    params: tuple = ()
    if statuses:
        query += " WHERE status IN (%s)" % ",".join("?" * len(statuses))
        params = statuses
    query += " ORDER BY event_date ASC, id ASC"
    with closing(get_conn()) as conn:
        return pd.read_sql_query(query, conn, params=params)


def set_status(res_id: int, status: str, note: str = "") -> tuple[bool, str]:
    try:
        with closing(get_conn()) as conn, conn:
            conn.execute(
                "UPDATE reservations SET status = ?, admin_note = ? WHERE id = ?",
                (status, note, res_id),
            )
    except sqlite3.IntegrityError:
        return False, "Another active reservation already exists for that date."
    return True, f"Updated to {status}."


def delete_reservation(res_id: int) -> None:
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM reservations WHERE id = ?", (res_id,))


# --------------------------------------------------------------------------
# Calendar rendering
# --------------------------------------------------------------------------


def month_html(year: int, month: int, taken: dict[str, sqlite3.Row], show_details: bool) -> str:
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
                key = "available"
            elif row["status"] == STATUS_RESERVED:
                key = "reserved"
            else:
                key = "pending"

            bg, fg, text = COLORS[key]
            cell = f'<div class="daynum" style="color:{fg}">{d.day}</div>'

            if slot is not None:
                if key in ("available", "pending", "reserved"):
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
            for k in ("available", "pending", "reserved")
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
    """Sidebar-free month selector, returns (year, month)."""
    today = date.today()
    options = []
    y, m = today.year, today.month
    for _ in range(MONTHS_AHEAD + 1):
        options.append((y, m))
        m += 1
        if m == 13:
            m, y = 1, y + 1
    labels = [f"{pycalendar.month_name[m]} {y}" for y, m in options]
    choice = st.selectbox("Month", labels, key=key)
    return options[labels.index(choice)]


def render_calendar(show_details: bool, key: str) -> None:
    year, month = month_picker(key)
    first = date(year, month, 1)
    last = date(year, month, pycalendar.monthrange(year, month)[1])
    st.markdown(month_html(year, month, status_map(first, last), show_details), unsafe_allow_html=True)


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
    taken = status_map(today, horizon)
    open_dates = [d for d in bookable_dates(today, horizon) if d.isoformat() not in taken]

    if not open_dates:
        st.warning("There are no open dates in the next few months. Please check back later.")
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
    else:
        blocker = None
        st.success(
            f"**{chosen.strftime('%A, %B %d, %Y')}**, {picked_slot['label']} is open. "
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
        "and **Sunday 3:00-7:00 p.m.** Names and event details are not shown publicly."
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
             "num_people", "email", "phone", "comments", "status", "submitted_at", "admin_note"]
        ].rename(
            columns={
                "id": "ID", "event_date": "Date", "day_name": "Day", "slot_label": "Time",
                "name": "Name", "purpose": "Purpose", "num_people": "People",
                "email": "Email", "phone": "Phone", "comments": "Comments",
                "status": "Status", "submitted_at": "Submitted", "admin_note": "Note",
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
        st.markdown("**Change a reservation**")
        ids = view["id"].tolist()
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
        "Sunday, 3:00-7:00 p.m."
    )

    if page == "Request a reservation":
        page_request()
    else:
        page_admin()


if __name__ == "__main__":
    main()
