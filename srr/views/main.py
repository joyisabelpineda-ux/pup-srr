"""User-facing pages: dashboard, rooms, reservations, profile, QR access."""
import io
import sqlite3
from datetime import timedelta

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file, session,
                   url_for)
from werkzeug.security import check_password_hash

from .. import services
from ..db import RES_STATUSES, ROOM_STATUSES, get_db, hash_password
from ..security import home_for, login_required
from ..services import ROOM_SELECT, audit, log_access
from ..util import clean, get_page, iso, like, now, paginate, password_problems, safe_next, valid_email

bp = Blueprint("main", __name__)


def _campuses(db):
    return db.execute("SELECT * FROM campuses ORDER BY name").fetchall()


def room_filters(args):
    return {k: clean(args.get(k), 80) for k in ("q", "campus", "building", "status", "min_capacity", "date", "start", "end")}


def search_rooms(db, f, user=None):
    """Search/filter rooms. When date/start/end are given, only rooms free in that window are kept."""
    sql = ROOM_SELECT + " WHERE 1=1"
    p = []
    if f["q"]:
        sql += " AND (r.room_number LIKE ? ESCAPE '\\' OR r.name LIKE ? ESCAPE '\\' OR r.building LIKE ? ESCAPE '\\')"
        p += [like(f["q"])] * 3
    if f["campus"].isdigit():
        sql += " AND r.campus_id=?"
        p.append(int(f["campus"]))
    if f["building"]:
        sql += " AND r.building=?"
        p.append(f["building"])
    if f["status"] in ROOM_STATUSES:
        sql += " AND r.status=?"
        p.append(f["status"])
    if f["min_capacity"].isdigit():
        sql += " AND r.capacity>=?"
        p.append(int(f["min_capacity"]))
    window_note = None
    if f["date"] and f["start"] and f["end"]:
        try:
            s, e = services.parse_slot(f["date"], f["start"], f["end"])
            if e > s:
                sql += (" AND r.status!='Under Maintenance' AND NOT EXISTS(SELECT 1 FROM reservations b WHERE b.room_id=r.id "
                        "AND b.status IN ('Pending','Approved') AND b.start_at<? AND b.end_at>?)")
                p += [iso(e), iso(s)]
                window_note = f"Showing rooms free on {s:%b %d, %Y} from {s:%I:%M %p} to {e:%I:%M %p}."
            else:
                flash("End time must be after start time for the availability check.", "error")
        except ValueError:
            flash("Enter a valid date and times for the availability check.", "error")
    return sql + " ORDER BY c.name, r.building, r.room_number", p, window_note


def attach_next(db, rooms):
    out = []
    for r in rooms:
        d = dict(r)
        d["next"] = services.room_next_reservation(db, r["id"])
        out.append(d)
    return out


@bp.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    uid = g.user["id"]
    n = iso(now())
    counts = {s: db.execute("SELECT COUNT(*) FROM reservations WHERE user_id=? AND status=?", (uid, s)).fetchone()[0]
              for s in RES_STATUSES}
    upcoming = db.execute("""SELECT b.*, r.room_number, r.name AS room_name, r.building FROM reservations b
        JOIN rooms r ON r.id=b.room_id WHERE b.user_id=? AND b.status IN ('Approved','Pending') AND b.end_at>?
        ORDER BY b.start_at LIMIT 5""", (uid, n)).fetchall()
    recent = db.execute("""SELECT b.*, r.room_number, r.name AS room_name, r.building FROM reservations b
        JOIN rooms r ON r.id=b.room_id WHERE b.user_id=? ORDER BY b.created_at DESC, b.id DESC LIMIT 5""", (uid,)).fetchall()
    room_counts = {s: db.execute("SELECT COUNT(*) FROM rooms WHERE status=?", (s,)).fetchone()[0] for s in ROOM_STATUSES}
    free_now = db.execute(ROOM_SELECT + " WHERE r.status='Available' ORDER BY r.capacity DESC LIMIT 4").fetchall()
    return render_template("dashboard.html", counts=counts, upcoming=upcoming, recent=recent,
                           room_counts=room_counts, total_rooms=sum(room_counts.values()), free_now=attach_next(db, free_now))


@bp.route("/rooms")
@login_required
def rooms():
    db = get_db()
    f = room_filters(request.args)
    sql, p, note = search_rooms(db, f)
    rows, pg = paginate(db, sql, p, get_page(request.args), 12)
    buildings = [r[0] for r in db.execute("SELECT DISTINCT building FROM rooms ORDER BY building")]
    return render_template("rooms.html", rooms=attach_next(db, rows), pg=pg, f=f, note=note,
                           campuses=_campuses(db), buildings=buildings)


@bp.route("/rooms/<int:room_id>")
@login_required
def room_detail(room_id):
    db = get_db()
    room = services.get_room(db, room_id)
    if not room:
        abort(404)
    slots = services.room_slots_today(db, room_id, 14)
    return render_template("room_detail.html", room=room, slots=slots, next=services.room_next_reservation(db, room_id))


@bp.route("/reserve", methods=["GET", "POST"])
@login_required
def reserve():
    db = get_db()
    rooms_ = db.execute(ROOM_SELECT + " WHERE r.status!='Under Maintenance' ORDER BY c.name, r.building, r.room_number").fetchall()
    form = {k: request.values.get(k, "") for k in ("room_id", "date", "start", "end", "purpose")}
    cfg = current_app.config
    limits = {"max_hours": services.max_hours_for(g.user), "min_minutes": cfg["MIN_MINUTES"],
              "open": cfg["OPEN_HOUR"], "close": cfg["CLOSE_HOUR"], "advance": cfg["MAX_ADVANCE_DAYS"],
              "today": now().strftime("%Y-%m-%d"), "max_date": (now() + timedelta(days=cfg["MAX_ADVANCE_DAYS"])).strftime("%Y-%m-%d")}
    if request.method == "POST":
        if services.rate_limited(db, "reserve", str(g.user["id"]), 30, 3600):
            flash("You are submitting requests too quickly. Please wait a while.", "error")
            return render_template("reserve.html", rooms=rooms_, form=form, limits=limits), 429
        services.rate_hit(db, "reserve", str(g.user["id"]))
        ok, msg, rid = services.create_reservation(db, g.user, form["room_id"], form["date"], form["start"], form["end"], form["purpose"])
        if ok:
            flash(msg, "success")
            return redirect(url_for("main.reservation_detail", rid=rid))
        flash(msg, "error")
        return render_template("reserve.html", rooms=rooms_, form=form, limits=limits), 409 if "not available" in msg else 400
    return render_template("reserve.html", rooms=rooms_, form=form, limits=limits)


@bp.route("/reservations")
@login_required
def reservations():
    db = get_db()
    status = request.args.get("status", "")
    q = clean(request.args.get("q"), 80)
    sql = ("SELECT b.*, r.room_number, r.name AS room_name, r.building, c.name AS campus_name FROM reservations b "
           "JOIN rooms r ON r.id=b.room_id JOIN campuses c ON c.id=r.campus_id WHERE b.user_id=?")
    p = [g.user["id"]]
    if status in RES_STATUSES:
        sql += " AND b.status=?"
        p.append(status)
    if q:
        sql += " AND (r.room_number LIKE ? ESCAPE '\\' OR r.name LIKE ? ESCAPE '\\' OR b.purpose LIKE ? ESCAPE '\\')"
        p += [like(q)] * 3
    rows, pg = paginate(db, sql + " ORDER BY b.start_at DESC, b.id DESC", p, get_page(request.args), current_app.config["PER_PAGE"])
    return render_template("reservations.html", rows=rows, pg=pg, status=status, q=q)


@bp.route("/reservations/<int:rid>")
@login_required
def reservation_detail(rid):
    db = get_db()
    res = db.execute("""SELECT b.*, r.room_number, r.name AS room_name, r.building, r.status AS room_status, r.qr_token AS room_token,
           c.name AS campus_name, u.full_name AS requester, u.user_id AS requester_id, u.role AS requester_role,
           rv.full_name AS reviewer FROM reservations b JOIN rooms r ON r.id=b.room_id
           JOIN campuses c ON c.id=r.campus_id JOIN users u ON u.id=b.user_id
           LEFT JOIN users rv ON rv.id=b.reviewed_by WHERE b.id=?""", (rid,)).fetchone()
    # Not the owner and not an admin -> 404 (does not reveal that the record exists)
    if not res or (res["user_id"] != g.user["id"] and g.user["role"] != "Administrator"):
        abort(404)
    return render_template("reservation_detail.html", r=res)


@bp.route("/reservations/<int:rid>/cancel", methods=["POST"])
@login_required
def cancel(rid):
    ok, msg = services.cancel_reservation(get_db(), g.user, rid)
    if ok is None:
        abort(404)
    flash(msg, "success" if ok else "error")
    back = request.form.get("back", "")
    if back == "detail":
        return redirect(url_for("main.reservation_detail", rid=rid))
    return redirect(safe_next(back, url_for("main.reservations")))


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    db = get_db()
    if request.method == "POST":
        name = clean(request.form.get("full_name"), 120)
        email = clean(request.form.get("email"), 160).lower()
        if len(name) < 2 or not valid_email(email):
            flash("Enter a valid name and email address.", "error")
        else:
            try:
                db.execute("UPDATE users SET full_name=?,email=?,department=?,program=?,year_level=? WHERE id=?",
                           (name, email, clean(request.form.get("department"), 120), clean(request.form.get("program"), 120),
                            clean(request.form.get("year_level"), 20), g.user["id"]))
                audit(db, "profile_updated", "user", g.user["id"], "profile fields updated")
                flash("Profile updated.", "success")
                return redirect(url_for("main.profile"))
            except sqlite3.IntegrityError:
                flash("That email already belongs to another account.", "error")
    user = db.execute("SELECT u.*, c.name AS campus_name FROM users u LEFT JOIN campuses c ON c.id=u.campus_id WHERE u.id=?",
                      (g.user["id"],)).fetchone()
    return render_template("profile.html", user=user, form=request.form if request.method == "POST" else None)


@bp.route("/profile/password", methods=["POST"])
@login_required
def change_password():
    db = get_db()
    cur, new, new2 = (request.form.get(k, "") for k in ("current_password", "new_password", "new_password2"))
    row = db.execute("SELECT * FROM users WHERE id=?", (g.user["id"],)).fetchone()
    problems = password_problems(new, row["user_id"], row["email"], current_app.config["PASSWORD_MIN_LENGTH"])
    if not check_password_hash(row["password_hash"], cur):
        flash("Your current password is incorrect.", "error")
    elif problems:
        flash("New password must have " + "; ".join(problems) + ".", "error")
    elif new != new2:
        flash("The new passwords do not match.", "error")
    else:
        import secrets
        nonce = secrets.token_hex(8)
        db.execute("UPDATE users SET password_hash=?, session_nonce=? WHERE id=?", (hash_password(new), nonce, g.user["id"]))
        audit(db, "password_changed", "user", g.user["id"], "other sessions signed out")
        session["nonce"] = nonce  # keep this session, end all others
        flash("Password changed. Other devices have been signed out.", "success")
    return redirect(url_for("main.profile"))


# ----------------------------------------------------------------- QR access
def _public_base():
    return current_app.config["PUBLIC_BASE_URL"]


def scan_url(token):
    path = url_for("main.scan_room", token=token)
    return (_public_base() + path) if _public_base() else url_for("main.scan_room", token=token, _external=True)


def room_by_token(db, token):
    return db.execute(ROOM_SELECT + " WHERE r.qr_token=?", (token,)).fetchone()


@bp.route("/scan/<token>")
def scan_room(token):
    """Landing page for a room's QR code. Authentication is required before anything is authorised."""
    db = get_db()
    if len(token) > 100 or services.rate_limited(db, "scan_bad", services.client_ip(), 30, 600):
        abort(404)
    room = room_by_token(db, token)
    if not room:
        services.rate_hit(db, "scan_bad", services.client_ip())
        log_access(db, g.user, None, None, "Denied", "QR scan attempt", "Unknown or invalid room QR code")
        abort(404)
    if not g.user:
        flash("Log in to check your reservation for this room.", "info")
        return redirect(url_for("auth.login", next=url_for("main.scan_room", token=token)))
    log_access(db, g.user, room, None, "Pending", "QR scan attempt", "Room page opened; not proof of entry")
    return render_template("scan.html", room=room, result=None)


@bp.route("/scan/<token>", methods=["POST"])
@login_required
def scan_authorize(token):
    db = get_db()
    room = room_by_token(db, token)
    if not room:
        abort(404)
    if services.rate_limited(db, "scan_auth", str(g.user["id"]), 30, 600):
        flash("Too many access checks. Please wait a few minutes.", "error")
        return render_template("scan.html", room=room, result=None), 429
    services.rate_hit(db, "scan_auth", str(g.user["id"]))
    granted, reason, res = services.authorize_room_access(db, g.user, room)
    log_access(db, g.user, room, res if granted or res else None, "Successful" if granted else "Denied", "Access decision",
               ("Reservation validated. " if granted else "") + reason + (" [simulated: no door hardware]" if granted else ""))
    audit(db, "room_access_authorized" if granted else "room_access_denied", "room", room["id"], reason)
    return render_template("scan.html", room=room, result={"granted": granted, "reason": reason, "res": res})
