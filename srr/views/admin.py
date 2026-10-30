"""Administrator pages. Every route is protected on the server with @admin_required."""
import csv
import io
import secrets
import sqlite3

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, send_file,
                   url_for)

from .. import services
from ..db import RES_STATUSES, ROLES, ROOM_STATUSES, get_db, transaction
from ..security import admin_required
from ..services import ROOM_SELECT, audit
from ..util import clean, get_page, iso, like, now, paginate, safe_next

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _campuses(db):
    return db.execute("SELECT * FROM campuses ORDER BY name").fetchall()


# ------------------------------------------------------------------ overview
@bp.route("/")
@admin_required
def overview():
    db = get_db()
    n = iso(now())
    one = lambda sql, *p: db.execute(sql, p).fetchone()[0]  # noqa: E731
    stats = {
        "users": one("SELECT COUNT(*) FROM users"),
        "active_users": one("SELECT COUNT(*) FROM users WHERE is_active=1"),
        "rooms": one("SELECT COUNT(*) FROM rooms"),
        "pending": one("SELECT COUNT(*) FROM reservations WHERE status='Pending' AND end_at>?", n),
        "approved_upcoming": one("SELECT COUNT(*) FROM reservations WHERE status='Approved' AND end_at>?", n),
        "approved_total": one("SELECT COUNT(*) FROM reservations WHERE status='Approved'"),
        "today": one("SELECT COUNT(*) FROM reservations WHERE status='Approved' AND date(start_at)=date(?)", n),
        "denied_today": one("SELECT COUNT(*) FROM access_logs WHERE result='Denied' AND date(occurred_at)=date(?)", n),
    }
    room_counts = {s: one("SELECT COUNT(*) FROM rooms WHERE status=?", s) for s in ROOM_STATUSES}
    pending = db.execute("""SELECT b.*, r.room_number, r.name AS room_name, r.building, u.full_name, u.user_id AS uid, u.role
        FROM reservations b JOIN rooms r ON r.id=b.room_id JOIN users u ON u.id=b.user_id
        WHERE b.status='Pending' AND b.end_at>? ORDER BY b.start_at LIMIT 8""", (n,)).fetchall()
    access = db.execute("""SELECT a.*, r.room_number FROM access_logs a LEFT JOIN rooms r ON r.id=a.room_id
        WHERE a.event_type='Access decision' ORDER BY a.id DESC LIMIT 6""").fetchall()
    return render_template("admin/overview.html", stats=stats, room_counts=room_counts, pending=pending, access=access)


# ------------------------------------------------------------------ rooms
@bp.route("/rooms")
@admin_required
def rooms():
    db = get_db()
    f = {k: clean(request.args.get(k), 80) for k in ("q", "campus", "status")}
    sql = ROOM_SELECT + " WHERE 1=1"
    p = []
    if f["q"]:
        sql += " AND (r.room_number LIKE ? ESCAPE '\\' OR r.name LIKE ? ESCAPE '\\' OR r.building LIKE ? ESCAPE '\\')"
        p += [like(f["q"])] * 3
    if f["campus"].isdigit():
        sql += " AND r.campus_id=?"
        p.append(int(f["campus"]))
    if f["status"] in ROOM_STATUSES:
        sql += " AND r.status=?"
        p.append(f["status"])
    rows, pg = paginate(db, sql + " ORDER BY c.name, r.building, r.room_number", p, get_page(request.args), current_app.config["PER_PAGE"])
    return render_template("admin/rooms.html", rooms=rows, pg=pg, f=f, campuses=_campuses(db))


def _room_form_values(form):
    return {
        "room_number": clean(form.get("room_number"), 20), "name": clean(form.get("name"), 80),
        "building": clean(form.get("building"), 80), "campus_id": clean(form.get("campus_id"), 10),
        "new_campus": clean(form.get("new_campus"), 80), "capacity": clean(form.get("capacity"), 6),
        "description": clean(form.get("description"), 300), "features": clean(form.get("features"), 200),
    }


def _validate_room(db, v):
    errs = []
    if not v["room_number"]:
        errs.append("Room number is required.")
    if len(v["name"]) < 2:
        errs.append("Room name is required.")
    if len(v["building"]) < 2:
        errs.append("Building is required.")
    if not v["capacity"].isdigit() or not 0 <= int(v["capacity"]) <= 2000:
        errs.append("Capacity must be a whole number from 0 to 2000.")
    campus_id = None
    if v["new_campus"]:
        row = db.execute("SELECT id FROM campuses WHERE lower(name)=lower(?)", (v["new_campus"],)).fetchone()
        campus_id = row["id"] if row else ("NEW", v["new_campus"])
    elif v["campus_id"].isdigit() and db.execute("SELECT 1 FROM campuses WHERE id=?", (int(v["campus_id"]),)).fetchone():
        campus_id = int(v["campus_id"])
    else:
        errs.append("Choose a campus or enter a new campus name.")
    return errs, campus_id


@bp.route("/rooms/new", methods=["GET", "POST"])
@admin_required
def room_new():
    db = get_db()
    v = _room_form_values(request.form) if request.method == "POST" else {k: "" for k in _room_form_values({})}
    if request.method == "POST":
        errs, campus_id = _validate_room(db, v)
        if not errs:
            try:
                with transaction(db):
                    if isinstance(campus_id, tuple):
                        campus_id = db.execute("INSERT INTO campuses(name,is_demo) VALUES(?,0)", (campus_id[1],)).lastrowid
                    cur = db.execute("""INSERT INTO rooms(room_number,name,building,campus_id,capacity,status,qr_token,is_demo,
                        updated_at,updated_by,description,features) VALUES(?,?,?,?,?,'Available',?,0,?,?,?,?)""",
                        (v["room_number"], v["name"], v["building"], campus_id, int(v["capacity"]), secrets.token_urlsafe(24),
                         iso(now()), g.user["id"], v["description"], v["features"]))
                    audit(db, "room_created", "room", cur.lastrowid, f"{v['building']} {v['room_number']}")
                flash("Room created.", "success")
                return redirect(url_for("admin.rooms"))
            except sqlite3.IntegrityError:
                errs.append("A room with that number already exists in that building and campus.")
        for e in errs:
            flash(e, "error")
    return render_template("admin/room_form.html", v=v, room=None, campuses=_campuses(db)), (400 if request.method == "POST" else 200)


@bp.route("/rooms/<int:rid>/edit", methods=["GET", "POST"])
@admin_required
def room_edit(rid):
    db = get_db()
    room = services.get_room(db, rid)
    if not room:
        abort(404)
    if request.method == "POST":
        v = _room_form_values(request.form)
        errs, campus_id = _validate_room(db, v)
        if not errs:
            try:
                with transaction(db):
                    if isinstance(campus_id, tuple):
                        campus_id = db.execute("INSERT INTO campuses(name,is_demo) VALUES(?,0)", (campus_id[1],)).lastrowid
                    db.execute("""UPDATE rooms SET room_number=?,name=?,building=?,campus_id=?,capacity=?,description=?,features=?,
                                  updated_at=?,updated_by=? WHERE id=?""",
                               (v["room_number"], v["name"], v["building"], campus_id, int(v["capacity"]), v["description"],
                                v["features"], iso(now()), g.user["id"], rid))
                    audit(db, "room_updated", "room", rid, f"{v['building']} {v['room_number']}; capacity={v['capacity']}")
                flash("Room updated.", "success")
                return redirect(url_for("admin.rooms"))
            except sqlite3.IntegrityError:
                errs.append("A room with that number already exists in that building and campus.")
        for e in errs:
            flash(e, "error")
        return render_template("admin/room_form.html", v=v, room=room, campuses=_campuses(db)), 400
    v = {"room_number": room["room_number"], "name": room["name"], "building": room["building"],
         "campus_id": str(room["campus_id"]), "new_campus": "", "capacity": str(room["capacity"]),
         "description": room["description"], "features": room["features"]}
    return render_template("admin/room_form.html", v=v, room=room, campuses=_campuses(db))


@bp.route("/rooms/<int:rid>/status", methods=["POST"])
@admin_required
def room_status(rid):
    db = get_db()
    status = request.form.get("status", "")
    note = clean(request.form.get("note"), 500)
    if status not in ROOM_STATUSES:
        flash("Choose a valid room status.", "error")
        return redirect(url_for("admin.rooms"))
    room = services.get_room(db, rid)
    if not room:
        abort(404)
    with transaction(db):
        changed = services.set_room_status(db, rid, status, "administrator", note, g.user["id"])
    if not changed:
        flash(f"Room {room['room_number']} is already {status}.", "info")
    else:
        msg = f"Room {room['room_number']} is now {status}."
        if status == "Under Maintenance":
            k = db.execute("SELECT COUNT(*) FROM reservations WHERE room_id=? AND status IN ('Approved','Pending') AND end_at>?",
                           (rid, iso(now()))).fetchone()[0]
            if k:
                msg += f" {k} upcoming reservation(s) for this room still exist; review them on the Reservations page."
        flash(msg, "success")
    services.refresh_room_statuses(db, force=True)
    return redirect(safe_next(request.form.get("back"), url_for("admin.rooms")))


@bp.route("/rooms/<int:rid>/qr.png")
@admin_required
def room_qr_png(rid):
    from .main import scan_url
    room = services.get_room(get_db(), rid)
    if not room:
        abort(404)
    try:
        import qrcode
    except ImportError:  # pragma: no cover
        abort(501)
    # High error correction helps printed codes remain readable if slightly scuffed.
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H,
                       box_size=10, border=4)
    qr.add_data(scan_url(room["qr_token"]))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    resp = send_file(out, mimetype="image/png", max_age=0)
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@bp.route("/rooms/<int:rid>/qr")
@admin_required
def room_qr(rid):
    from .main import scan_url
    room = services.get_room(get_db(), rid)
    if not room:
        abort(404)
    return render_template("admin/room_qr.html", room=room, url=scan_url(room["qr_token"]))


@bp.route("/rooms/<int:rid>/qr/regenerate", methods=["POST"])
@admin_required
def room_qr_regenerate(rid):
    db = get_db()
    if not services.get_room(db, rid):
        abort(404)
    db.execute("UPDATE rooms SET qr_token=?,updated_at=?,updated_by=? WHERE id=?",
               (secrets.token_urlsafe(24), iso(now()), g.user["id"], rid))
    audit(db, "room_qr_regenerated", "room", rid, "old printed QR codes no longer work")
    flash("A new QR code was generated. Print it and replace the old one — the old code no longer works.", "success")
    return redirect(url_for("admin.room_qr", rid=rid))


# ------------------------------------------------------------------ reservations
def _reservation_query(args):
    f = {k: clean(args.get(k), 80) for k in ("q", "status", "campus", "date_from", "date_to")}
    sql = """SELECT b.*, r.room_number, r.name AS room_name, r.building, c.name AS campus_name,
             u.full_name, u.user_id AS uid, u.role FROM reservations b JOIN rooms r ON r.id=b.room_id
             JOIN campuses c ON c.id=r.campus_id JOIN users u ON u.id=b.user_id WHERE 1=1"""
    p = []
    if f["status"] in RES_STATUSES:
        sql += " AND b.status=?"
        p.append(f["status"])
    if f["campus"].isdigit():
        sql += " AND r.campus_id=?"
        p.append(int(f["campus"]))
    if f["q"]:
        sql += (" AND (u.full_name LIKE ? ESCAPE '\\' OR u.user_id LIKE ? ESCAPE '\\' OR r.room_number LIKE ? ESCAPE '\\' "
                "OR r.name LIKE ? ESCAPE '\\' OR b.purpose LIKE ? ESCAPE '\\')")
        p += [like(f["q"])] * 5
    if f["date_from"]:
        sql += " AND date(b.start_at)>=date(?)"
        p.append(f["date_from"])
    if f["date_to"]:
        sql += " AND date(b.start_at)<=date(?)"
        p.append(f["date_to"])
    return f, sql + " ORDER BY b.start_at DESC, b.id DESC", p


@bp.route("/reservations")
@admin_required
def reservations():
    db = get_db()
    f, sql, p = _reservation_query(request.args)
    rows, pg = paginate(db, sql, p, get_page(request.args), current_app.config["PER_PAGE"])
    return render_template("admin/reservations.html", rows=rows, pg=pg, f=f, campuses=_campuses(db))


@bp.route("/reservations/<int:rid>/review", methods=["POST"])
@admin_required
def review(rid):
    decision = request.form.get("decision", "")
    ok, msg = services.review_reservation(get_db(), g.user, rid, decision, request.form.get("note", ""))
    flash(msg, "success" if ok else "error")
    return redirect(safe_next(request.form.get("back"), url_for("admin.reservations")))


# ------------------------------------------------------------------ users
@bp.route("/users")
@admin_required
def users():
    db = get_db()
    f = {k: clean(request.args.get(k), 80) for k in ("q", "role", "active")}
    sql = "SELECT u.*, c.name AS campus_name FROM users u LEFT JOIN campuses c ON c.id=u.campus_id WHERE 1=1"
    p = []
    if f["q"]:
        sql += " AND (u.full_name LIKE ? ESCAPE '\\' OR u.user_id LIKE ? ESCAPE '\\' OR u.email LIKE ? ESCAPE '\\')"
        p += [like(f["q"])] * 3
    if f["role"] in ROLES:
        sql += " AND u.role=?"
        p.append(f["role"])
    if f["active"] in ("0", "1"):
        sql += " AND u.is_active=?"
        p.append(int(f["active"]))
    rows, pg = paginate(db, sql + " ORDER BY u.created_at DESC, u.id DESC", p, get_page(request.args), current_app.config["PER_PAGE"])
    return render_template("admin/users.html", rows=rows, pg=pg, f=f, roles=ROLES)


def _active_admins(db):
    return db.execute("SELECT COUNT(*) FROM users WHERE role='Administrator' AND is_active=1").fetchone()[0]


@bp.route("/users/<int:uid>/active", methods=["POST"])
@admin_required
def user_active(uid):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not target:
        abort(404)
    want = 1 if request.form.get("active") == "1" else 0
    if uid == g.user["id"] and not want:
        flash("You cannot deactivate your own account.", "error")
    elif not want and target["role"] == "Administrator" and _active_admins(db) <= 1:
        flash("You cannot deactivate the last active administrator.", "error")
    else:
        db.execute("UPDATE users SET is_active=?, session_nonce=? WHERE id=?", (want, secrets.token_hex(8), uid))
        audit(db, "user_activated" if want else "user_deactivated", "user", uid, target["user_id"])
        flash(f"{target['full_name']} was {'activated' if want else 'deactivated'}.", "success")
    return redirect(safe_next(request.form.get("back"), url_for("admin.users")))


@bp.route("/users/<int:uid>/reset-password", methods=["POST"])
@admin_required
def user_reset_password(uid):
    """Admin-assisted recovery: sets a random temporary password (shown once) and signs the user out everywhere."""
    from ..db import hash_password
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not target:
        abort(404)
    if uid == g.user["id"]:
        flash("Use Profile → Change password for your own account.", "error")
        return redirect(url_for("admin.users"))
    temp = secrets.token_urlsafe(9) + "7a"
    db.execute("UPDATE users SET password_hash=?, session_nonce=? WHERE id=?", (hash_password(temp), secrets.token_hex(8), uid))
    audit(db, "user_password_reset", "user", uid, target["user_id"])
    flash(f"Temporary password for {target['user_id']}: {temp} — give it to the user privately; it is shown only once. "
          "They should change it under Profile after logging in.", "success")
    return redirect(url_for("admin.users"))


@bp.route("/users/<int:uid>/role", methods=["POST"])
@admin_required
def user_role(uid):
    db = get_db()
    target = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    role = request.form.get("role", "")
    if not target:
        abort(404)
    if role not in ROLES:
        flash("Choose a valid role.", "error")
    elif uid == g.user["id"]:
        flash("You cannot change your own role.", "error")
    elif target["role"] == "Administrator" and role != "Administrator" and _active_admins(db) <= 1:
        flash("You cannot remove the last administrator.", "error")
    else:
        db.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        audit(db, "user_role_changed", "user", uid, f"{target['role']} -> {role}")
        flash(f"{target['full_name']} is now {role}.", "success")
    return redirect(url_for("admin.users"))


# ------------------------------------------------------------------ logs
@bp.route("/history")
@admin_required
def history():
    db = get_db()
    f = {k: clean(request.args.get(k), 40) for k in ("campus", "room", "status", "date")}
    sql = """SELECT h.*, r.room_number, r.name AS room_name, r.building, c.name AS campus_name, u.full_name AS changed_by_name
             FROM room_status_history h JOIN rooms r ON r.id=h.room_id JOIN campuses c ON c.id=r.campus_id
             LEFT JOIN users u ON u.id=h.changed_by WHERE 1=1"""
    p = []
    if f["campus"].isdigit():
        sql += " AND r.campus_id=?"
        p.append(int(f["campus"]))
    if f["room"]:
        sql += " AND (r.room_number LIKE ? ESCAPE '\\' OR r.name LIKE ? ESCAPE '\\')"
        p += [like(f["room"])] * 2
    if f["status"] in ROOM_STATUSES:
        sql += " AND h.new_status=?"
        p.append(f["status"])
    if f["date"]:
        sql += " AND date(h.changed_at)=date(?)"
        p.append(f["date"])
    rows, pg = paginate(db, sql + " ORDER BY h.changed_at DESC, h.id DESC", p, get_page(request.args), current_app.config["PER_PAGE"])
    return render_template("admin/history.html", rows=rows, pg=pg, f=f, campuses=_campuses(db))


def _access_query(args):
    f = {k: clean(args.get(k), 60) for k in ("q", "result", "event_type", "date")}
    sql = "SELECT a.*, r.room_number, r.name AS room_name FROM access_logs a LEFT JOIN rooms r ON r.id=a.room_id WHERE 1=1"
    p = []
    if f["q"]:
        sql += " AND (a.user_name_snapshot LIKE ? ESCAPE '\\' OR r.room_number LIKE ? ESCAPE '\\' OR a.reason LIKE ? ESCAPE '\\')"
        p += [like(f["q"])] * 3
    if f["result"] in ("Successful", "Denied", "Pending"):
        sql += " AND a.result=?"
        p.append(f["result"])
    if f["event_type"]:
        sql += " AND a.event_type=?"
        p.append(f["event_type"])
    if f["date"]:
        sql += " AND date(a.occurred_at)=date(?)"
        p.append(f["date"])
    return f, sql + " ORDER BY a.occurred_at DESC, a.id DESC", p


@bp.route("/access-logs")
@admin_required
def access_logs():
    db = get_db()
    f, sql, p = _access_query(request.args)
    rows, pg = paginate(db, sql, p, get_page(request.args), current_app.config["PER_PAGE"])
    return render_template("admin/access_logs.html", rows=rows, pg=pg, f=f)


@bp.route("/audit-logs")
@admin_required
def audit_logs():
    db = get_db()
    f = {k: clean(request.args.get(k), 60) for k in ("q", "action", "date")}
    sql = "SELECT a.*, u.full_name AS actor_name, u.user_id AS actor_uid FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_id WHERE 1=1"
    p = []
    if f["q"]:
        sql += " AND (a.details LIKE ? ESCAPE '\\' OR u.full_name LIKE ? ESCAPE '\\' OR a.entity_type LIKE ? ESCAPE '\\')"
        p += [like(f["q"])] * 3
    if f["action"]:
        sql += " AND a.action=?"
        p.append(f["action"])
    if f["date"]:
        sql += " AND date(a.occurred_at)=date(?)"
        p.append(f["date"])
    rows, pg = paginate(db, sql + " ORDER BY a.occurred_at DESC, a.id DESC", p, get_page(request.args), current_app.config["PER_PAGE"])
    actions = [r[0] for r in db.execute("SELECT DISTINCT action FROM audit_logs ORDER BY action")]
    return render_template("admin/audit_logs.html", rows=rows, pg=pg, f=f, actions=actions)


# ------------------------------------------------------------------ reports (CSV)
def _csv_cell(v):
    """Neutralise spreadsheet formula injection (cells starting with = + - @)."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


REPORTS = {
    "reservations": "Reservations",
    "rooms": "Rooms",
    "users": "Users (no passwords)",
    "access": "Access logs (QR authorization)",
    "audit": "Audit logs",
    "history": "Room status history",
}


@bp.route("/reports")
@admin_required
def reports():
    return render_template("admin/reports.html", reports=REPORTS)


@bp.route("/reports/<kind>.csv")
@admin_required
def report_csv(kind):
    if kind not in REPORTS:
        abort(404)
    db = get_db()
    out = io.StringIO()
    w = csv.writer(out)
    if kind == "reservations":
        f, sql, p = _reservation_query(request.args)
        w.writerow(["Reservation ID", "Requester", "User ID", "Role", "Campus", "Building", "Room", "Purpose", "Start", "End",
                    "Status", "Created", "Reviewed by", "Reviewed at", "Review note"])
        for r in db.execute(sql, p):
            rv = db.execute("SELECT full_name FROM users WHERE id=?", (r["reviewed_by"],)).fetchone() if r["reviewed_by"] else None
            w.writerow([_csv_cell(x) for x in (r["id"], r["full_name"], r["uid"], r["role"], r["campus_name"], r["building"],
                        f"{r['room_number']} {r['room_name']}", r["purpose"], r["start_at"], r["end_at"], r["status"],
                        r["created_at"], rv["full_name"] if rv else "", r["reviewed_at"] or "", r["review_note"] or "")])
    elif kind == "rooms":
        w.writerow(["Room ID", "Campus", "Building", "Room number", "Name", "Capacity", "Status", "Features", "Updated"])
        for r in db.execute(ROOM_SELECT + " ORDER BY c.name, r.building, r.room_number"):
            w.writerow([_csv_cell(x) for x in (r["id"], r["campus_name"], r["building"], r["room_number"], r["name"],
                        r["capacity"], r["status"], r["features"], r["updated_at"])])
    elif kind == "users":
        w.writerow(["ID", "User ID", "Full name", "Email", "Role", "Campus", "Department", "Active", "Created", "Last login"])
        for r in db.execute("SELECT u.*, c.name AS campus_name FROM users u LEFT JOIN campuses c ON c.id=u.campus_id ORDER BY u.id"):
            w.writerow([_csv_cell(x) for x in (r["id"], r["user_id"], r["full_name"], r["email"], r["role"], r["campus_name"] or "",
                        r["department"], "yes" if r["is_active"] else "no", r["created_at"], r["last_login_at"] or "")])
    elif kind == "access":
        f, sql, p = _access_query(request.args)
        w.writerow(["Log ID", "Time", "User", "User ID (db)", "Role", "Campus", "Building", "Room", "Method", "Event", "Result",
                    "Reason", "Reservation ID"])
        for r in db.execute(sql, p):
            w.writerow([_csv_cell(x) for x in (r["id"], r["occurred_at"], r["user_name_snapshot"], r["user_id"] or "",
                        r["user_role_snapshot"], r["campus_name"], r["building"], r["room_number"] or "", r["method"],
                        r["event_type"], r["result"], r["reason"], r["reservation_id"] or "")])
    elif kind == "audit":
        w.writerow(["Log ID", "Time", "Actor", "Action", "Entity", "Entity ID", "Details"])
        for r in db.execute("""SELECT a.*, u.full_name AS actor FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_id
                               ORDER BY a.id DESC"""):
            w.writerow([_csv_cell(x) for x in (r["id"], r["occurred_at"], r["actor"] or "system/anonymous", r["action"],
                        r["entity_type"], r["entity_id"] or "", r["details"])])
    else:
        w.writerow(["ID", "Time", "Campus", "Building", "Room", "Previous", "New", "Source", "Changed by", "Note"])
        for r in db.execute("""SELECT h.*, r.room_number, r.building, c.name AS campus_name, u.full_name AS by_name
             FROM room_status_history h JOIN rooms r ON r.id=h.room_id JOIN campuses c ON c.id=r.campus_id
             LEFT JOIN users u ON u.id=h.changed_by ORDER BY h.id DESC"""):
            w.writerow([_csv_cell(x) for x in (r["id"], r["changed_at"], r["campus_name"], r["building"], r["room_number"],
                        r["previous_status"], r["new_status"], r["source"], r["by_name"] or "system", r["note"])])
    audit(db, "report_exported", "report", None, kind)
    stamp = now().strftime("%Y%m%d-%H%M")
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=pup_srr_{kind}_{stamp}.csv"})
