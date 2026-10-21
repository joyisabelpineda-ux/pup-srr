"""Business logic: audit/access logging, room status, reservations, rate limiting."""
import sqlite3
import time
from datetime import date, datetime, timedelta

from flask import current_app, g, request

from .db import ROOM_STATUSES, transaction
from .util import iso, now, parse_dt, fmt_dt, fmt_time, clean

ROOM_SELECT = ("SELECT r.*, c.name AS campus_name, c.is_demo AS campus_is_demo "
               "FROM rooms r JOIN campuses c ON c.id=r.campus_id")


# ------------------------------------------------------------------ logging
def audit(db, action, entity_type, entity_id=None, details="", actor_id=None):
    if actor_id is None and getattr(g, "user", None):
        actor_id = g.user["id"]
    db.execute("INSERT INTO audit_logs(actor_id,action,entity_type,entity_id,occurred_at,details) VALUES(?,?,?,?,?,?)",
               (actor_id, action, entity_type, entity_id, iso(now()), (details or "")[:1000]))


def log_access(db, user, room, reservation, result, event_type, reason="", method="QR code"):
    db.execute("""INSERT INTO access_logs(user_id,room_id,reservation_id,occurred_at,method,result,event_type,reason,
                  campus_name,building,user_name_snapshot,user_role_snapshot) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
               (user["id"] if user else None, room["id"] if room else None,
                reservation["id"] if reservation else None, iso(now()), method, result, event_type, reason[:300],
                room["campus_name"] if room else "", room["building"] if room else "",
                user["full_name"] if user else "Unknown/Unauthenticated", user["role"] if user else "Unknown"))


# ------------------------------------------------------------------ rate limiting (stored in SQLite so it works across workers)
def client_ip():
    return request.remote_addr or "unknown"


def rate_count(db, bucket, key, window):
    return db.execute("SELECT COUNT(*) FROM rate_limit_events WHERE bucket=? AND key=? AND occurred_at>?",
                      (bucket, key, time.time() - window)).fetchone()[0]


def rate_limited(db, bucket, key, limit, window):
    if not current_app.config.get("RATE_LIMIT_ENABLED", True):
        return False
    return rate_count(db, bucket, key, window) >= limit


def rate_hit(db, bucket, key):
    if not current_app.config.get("RATE_LIMIT_ENABLED", True):
        return
    t = time.time()
    db.execute("INSERT INTO rate_limit_events(bucket,key,occurred_at) VALUES(?,?,?)", (bucket, key, t))
    if int(t) % 20 == 0:  # opportunistic cleanup of events older than a day
        db.execute("DELETE FROM rate_limit_events WHERE occurred_at<?", (t - 86400,))


def rate_clear(db, bucket, key):
    db.execute("DELETE FROM rate_limit_events WHERE bucket=? AND key=?", (bucket, key))


# ------------------------------------------------------------------ rooms
def get_room(db, room_id):
    return db.execute(ROOM_SELECT + " WHERE r.id=?", (room_id,)).fetchone()


def set_room_status(db, room_id, new_status, source, note="", actor_id=None, source_mode=None,
                    res_start=None, res_end=None, guard_status=None):
    """Change a room's status and record history + audit. Returns True when a change was made."""
    if new_status not in ROOM_STATUSES:
        raise ValueError("Invalid room status")
    row = db.execute("SELECT status FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not row or row["status"] == new_status:
        return False
    if guard_status and row["status"] != guard_status:
        return False
    stamp = iso(now())
    mode = source_mode or ("manual" if new_status != "Available" else "system")
    db.execute("UPDATE rooms SET status=?,status_source=?,updated_at=?,updated_by=? WHERE id=?",
               (new_status, mode, stamp, actor_id, room_id))
    db.execute("""INSERT INTO room_status_history(room_id,previous_status,new_status,changed_at,reservation_start,
                  reservation_end,source,changed_by,note) VALUES(?,?,?,?,?,?,?,?,?)""",
               (room_id, row["status"], new_status, stamp, res_start, res_end, source, actor_id, (note or "")[:500]))
    audit(db, "room_status_changed", "room", room_id, f"{row['status']} -> {new_status}; source={source}; {note or ''}".strip(),
          actor_id=actor_id)
    return True


_last_sync = {"t": 0.0, "db": None}


def refresh_room_statuses(db, force=False):
    """Keep automatic room statuses consistent with approved reservations.

    Available <-> Reserved follows whether an approved reservation is active right now. Rooms an
    administrator marked Occupied / Under Maintenance / Reserved (manual) are left untouched.
    """
    interval = current_app.config.get("ROOM_SYNC_INTERVAL", 15)
    path = current_app.config["DATABASE"]
    if not force and _last_sync["db"] == path and time.time() - _last_sync["t"] < interval:
        return 0
    stamp = iso(now())
    rows = db.execute("""SELECT r.id, r.status, r.status_source,
        (SELECT b.id FROM reservations b WHERE b.room_id=r.id AND b.status='Approved'
           AND b.start_at<=? AND b.end_at>? LIMIT 1) AS active_id,
        (SELECT b.start_at FROM reservations b WHERE b.id=(SELECT b2.id FROM reservations b2 WHERE b2.room_id=r.id
           AND b2.status='Approved' AND b2.start_at<=? AND b2.end_at>? LIMIT 1)) AS a_start,
        (SELECT b.end_at FROM reservations b WHERE b.id=(SELECT b2.id FROM reservations b2 WHERE b2.room_id=r.id
           AND b2.status='Approved' AND b2.start_at<=? AND b2.end_at>? LIMIT 1)) AS a_end
        FROM rooms r""", (stamp,) * 6).fetchall()
    todo = []
    for r in rows:
        if r["status_source"] != "system":
            continue
        if r["status"] == "Available" and r["active_id"]:
            todo.append((r["id"], "Reserved", f"Reservation #{r['active_id']} started", r["a_start"], r["a_end"], "Available"))
        elif r["status"] == "Reserved" and not r["active_id"]:
            todo.append((r["id"], "Available", "No active approved reservation", None, None, "Reserved"))
    changed = 0
    if todo:
        with transaction(db):
            for rid, new, note, s, e, guard in todo:
                if set_room_status(db, rid, new, "system", note, None, "system", s, e, guard_status=guard):
                    changed += 1
    _last_sync.update(t=time.time(), db=path)
    return changed


def room_next_reservation(db, room_id):
    return db.execute("""SELECT start_at,end_at FROM reservations WHERE room_id=? AND status='Approved' AND end_at>?
                         ORDER BY start_at LIMIT 1""", (room_id, iso(now()))).fetchone()


# ------------------------------------------------------------------ reservations
def max_hours_for(user):
    cfg = current_app.config
    return cfg["MAX_HOURS_STUDENT"] if user["role"] == "Student" else cfg["MAX_HOURS_DEFAULT"]


def has_conflict(db, room_id, start_s, end_s, statuses=("Pending", "Approved"), exclude_id=None):
    q = ",".join("?" * len(statuses))
    sql = (f"SELECT id FROM reservations WHERE room_id=? AND status IN ({q}) AND start_at<? AND end_at>?")
    params = [room_id, *statuses, end_s, start_s]
    if exclude_id:
        sql += " AND id<>?"
        params.append(exclude_id)
    return db.execute(sql + " LIMIT 1", params).fetchone()


def parse_slot(date_s, start_s, end_s):
    d = date.fromisoformat((date_s or "").strip())
    st = datetime.strptime((start_s or "").strip()[:5], "%H:%M").time()
    et = datetime.strptime((end_s or "").strip()[:5], "%H:%M").time()
    return datetime.combine(d, st), datetime.combine(d, et)


def validate_slot(user, start, end):
    """Return a list of human-readable problems (empty when the time slot is acceptable)."""
    cfg = current_app.config
    errs = []
    n = now()
    if end <= start:
        errs.append("End time must be after the start time.")
        return errs
    if start < n:
        errs.append("Reservations cannot be made in the past. Choose a future date and time.")
    if start.date() > (n + timedelta(days=cfg["MAX_ADVANCE_DAYS"])).date():
        errs.append(f"Reservations can be made at most {cfg['MAX_ADVANCE_DAYS']} days ahead.")
    minutes = (end - start).total_seconds() / 60
    if minutes < cfg["MIN_MINUTES"]:
        errs.append(f"The minimum reservation length is {cfg['MIN_MINUTES']} minutes.")
    limit = max_hours_for(user)
    if minutes > limit * 60:
        errs.append(f"The maximum reservation length for your account type is {limit} hours.")
    if start.hour < cfg["OPEN_HOUR"] or (end.hour, end.minute) > (cfg["CLOSE_HOUR"], 0):
        errs.append(f"Rooms can be reserved between {cfg['OPEN_HOUR']:02d}:00 and {cfg['CLOSE_HOUR']:02d}:00 only.")
    return errs


def create_reservation(db, user, room_id, date_s, start_s, end_s, purpose):
    """Validate and store a Pending reservation. Returns (ok, message, reservation_id)."""
    try:
        room_id = int(room_id)
        start, end = parse_slot(date_s, start_s, end_s)
    except (TypeError, ValueError):
        return False, "Select a room and enter a valid date, start time and end time.", None
    purpose = clean(purpose, 300)
    if len(purpose) < 3:
        return False, "Please describe the purpose of the reservation (3–300 characters).", None
    errs = validate_slot(user, start, end)
    if errs:
        return False, " ".join(errs), None
    start_s, end_s = iso(start), iso(end)
    try:
        with transaction(db):
            room = get_room(db, room_id)
            if not room:
                return False, "That room does not exist.", None
            if room["status"] == "Under Maintenance":
                return False, "This room is under maintenance and cannot be reserved right now.", None
            pending = db.execute("SELECT COUNT(*) FROM reservations WHERE user_id=? AND status='Pending'",
                                 (user["id"],)).fetchone()[0]
            if pending >= current_app.config["MAX_PENDING_PER_USER"]:
                return False, "You have too many pending requests. Wait for them to be reviewed or cancel some.", None
            clash = has_conflict(db, room_id, start_s, end_s)
            if clash:
                return False, ("This room is not available at that time: it overlaps an existing approved or pending "
                               "reservation. Please choose another time or room."), None
            cur = db.execute("""INSERT INTO reservations(user_id,room_id,purpose,start_at,end_at,status,created_at)
                                VALUES(?,?,?,?,?,'Pending',?)""", (user["id"], room_id, purpose, start_s, end_s, iso(now())))
            audit(db, "reservation_requested", "reservation", cur.lastrowid,
                  f"room={room_id}; {start_s} to {end_s}", actor_id=user["id"])
            rid = cur.lastrowid
    except sqlite3.IntegrityError:
        return False, "This room is not available at that time. Please choose another slot.", None
    return True, "Reservation request submitted. It is pending until an administrator reviews it.", rid


def review_reservation(db, admin, rid, decision, note=""):
    """Approve or reject a Pending reservation. Availability is re-checked inside the write lock."""
    if decision not in ("Approved", "Rejected"):
        return False, "Invalid decision."
    note = clean(note, 500)
    try:
        with transaction(db):
            res = db.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
            if not res:
                return False, "Reservation not found."
            if res["status"] != "Pending":
                return False, f"Only pending reservations can be reviewed (this one is {res['status'].lower()})."
            if res["user_id"] == admin["id"]:
                return False, "You cannot review your own reservation. Another administrator must do it."
            if decision == "Approved":
                if res["end_at"] <= iso(now()):
                    return False, "This reservation's time has already passed, so it cannot be approved. Reject it instead."
                room = get_room(db, res["room_id"])
                if room["status"] == "Under Maintenance":
                    return False, "The room is under maintenance. Change its status first, or reject the request."
                if has_conflict(db, res["room_id"], res["start_at"], res["end_at"], ("Approved",), exclude_id=rid):
                    return False, "Cannot approve: the room is already approved for an overlapping time."
            db.execute("UPDATE reservations SET status=?,reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
                       (decision, admin["id"], iso(now()), note, rid))
            audit(db, "reservation_" + decision.lower(), "reservation", rid, note or "", actor_id=admin["id"])
    except sqlite3.IntegrityError:
        return False, "Cannot approve: the room is already approved for an overlapping time."
    refresh_room_statuses(db, force=True)
    return True, f"Reservation #{rid} {decision.lower()}."


def can_cancel(res, user, ref=None):
    ref = iso(ref or now())
    if res["status"] not in ("Pending", "Approved"):
        return False
    if user["role"] == "Administrator":
        return res["end_at"] > ref
    return res["user_id"] == user["id"] and res["start_at"] > ref


def cancel_reservation(db, user, rid):
    try:
        with transaction(db):
            res = db.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone()
            if not res or (res["user_id"] != user["id"] and user["role"] != "Administrator"):
                return None, "Reservation not found."
            if not can_cancel(res, user):
                if res["status"] not in ("Pending", "Approved"):
                    return False, f"A {res['status'].lower()} reservation cannot be cancelled."
                return False, "Reservations can only be cancelled before they start."
            db.execute("UPDATE reservations SET status='Cancelled',cancelled_at=?,cancelled_by=? WHERE id=?",
                       (iso(now()), user["id"], rid))
            audit(db, "reservation_cancelled", "reservation", rid, f"was {res['status']}", actor_id=user["id"])
    except sqlite3.Error:
        raise
    refresh_room_statuses(db, force=True)
    return True, "Reservation cancelled."


def display_status(res):
    """Status shown to users; Approved/Pending reservations that already ended are labelled."""
    ref = iso(now())
    s = res["status"]
    if s == "Approved":
        if res["end_at"] <= ref:
            return "Completed"
        if res["start_at"] <= ref:
            return "In progress"
        return "Approved"
    if s == "Pending" and res["end_at"] <= ref:
        return "Expired"
    return s


def room_slots_today(db, room_id, days=7):
    """Upcoming approved/pending slots for the room schedule widget."""
    return db.execute("""SELECT b.start_at,b.end_at,b.status FROM reservations b
        WHERE b.room_id=? AND b.status IN ('Approved','Pending') AND b.end_at>? AND b.start_at<?
        ORDER BY b.start_at""", (room_id, iso(now()), iso(now() + timedelta(days=days)))).fetchall()


# ------------------------------------------------------------------ QR authorization
def authorize_room_access(db, user, room):
    """Decide (simulated) room access for `user`. Returns (granted, reason, reservation)."""
    n = iso(now())
    if room["status"] == "Under Maintenance":
        return False, "This room is under maintenance. Access is not allowed.", None
    active = db.execute("""SELECT * FROM reservations WHERE room_id=? AND user_id=? AND status='Approved'
                           AND start_at<=? AND end_at>? ORDER BY start_at LIMIT 1""",
                        (room["id"], user["id"], n, n)).fetchone()
    if active:
        return True, f"Approved reservation #{active['id']} is active ({fmt_time(active['start_at'])}–{fmt_time(active['end_at'])}).", active
    cutoff = iso(now() - timedelta(hours=24))
    near = db.execute("""SELECT * FROM reservations WHERE room_id=? AND user_id=? AND end_at>? 
                         ORDER BY CASE status WHEN 'Approved' THEN 0 WHEN 'Pending' THEN 1 ELSE 2 END, start_at LIMIT 1""",
                      (room["id"], user["id"], cutoff)).fetchone()
    if not near:
        return False, "You do not have an approved reservation for this room at this time.", None
    if near["status"] == "Approved":
        if near["end_at"] <= n:
            return False, f"Your reservation for this room has expired (it ended {fmt_dt(near['end_at'])}).", near
        return False, f"Your reservation is not active yet. It starts {fmt_dt(near['start_at'])}.", near
    msgs = {"Pending": "Your reservation request is still pending approval.",
            "Rejected": "Your reservation request for this room was rejected.",
            "Cancelled": "Your reservation for this room was cancelled."}
    return False, msgs[near["status"]], near
