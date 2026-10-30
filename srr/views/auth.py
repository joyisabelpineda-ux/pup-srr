"""Login, registration and logout."""
import secrets
import sqlite3

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from ..db import SELF_REGISTER_ROLES, get_db, hash_password
from ..security import home_for, login_required
from ..services import (audit, client_ip, rate_clear, rate_count, rate_hit, rate_limited)
from ..util import USER_ID_RE, clean, iso, now, password_problems, safe_next, valid_email

bp = Blueprint("auth", __name__)
# A valid hash used to equalise timing when the account does not exist (prevents user enumeration by timing).
_DUMMY_HASH = generate_password_hash("not-a-real-password", method="pbkdf2:sha256:1000")


@bp.route("/")
def index():
    return redirect(home_for(g.user) if g.user else url_for("auth.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    cfg = current_app.config
    db = get_db()
    nxt = request.values.get("next", "")
    if request.method == "GET" and g.user:
        return redirect(safe_next(nxt, home_for(g.user)))
    status = 200
    if request.method == "POST":
        ident = clean(request.form.get("user_id"), 160)
        pw = request.form.get("password", "")
        ip = client_ip()
        key = ident.lower()
        window = cfg["LOGIN_WINDOW_SECONDS"]
        if rate_limited(db, "login_ident", key, cfg["LOGIN_MAX_FAILS"], window) or \
                rate_limited(db, "login_ip", ip, cfg["LOGIN_IP_MAX_FAILS"], window):
            flash("Too many failed attempts. Please wait a few minutes before trying again.", "error")
            return render_template("login.html", next=nxt), 429
        user = None
        if ident and pw:
            user = db.execute("SELECT * FROM users WHERE lower(user_id)=lower(?) OR email=lower(?)", (ident, ident)).fetchone()
        ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, pw) and user is not None
        if not ok:
            rate_hit(db, "login_ident", key)
            rate_hit(db, "login_ip", ip)
            audit(db, "login_failed", "user", user["id"] if user else None,
                  "wrong password" if user else "unknown account", actor_id=user["id"] if user else None)
            flash("Invalid account ID/email or password.", "error")
            status = 401
        elif not user["is_active"]:
            audit(db, "login_blocked_inactive", "user", user["id"], "", actor_id=user["id"])
            flash("This account has been deactivated. Please contact an administrator.", "error")
            status = 403
        else:
            rate_clear(db, "login_ident", key)
            session.clear()
            session.permanent = True
            session["uid"] = user["id"]
            session["nonce"] = user["session_nonce"] or ""
            session["csrf"] = secrets.token_urlsafe(32)
            db.execute("UPDATE users SET last_login_at=? WHERE id=?", (iso(now()), user["id"]))
            audit(db, "login_success", "user", user["id"], "", actor_id=user["id"])
            flash(f"Welcome back, {user['full_name'].split()[0]}!", "success")
            return redirect(safe_next(nxt, home_for(user)))
    show_demo = cfg.get("SEED_DEMO_USERS") and not cfg.get("PRODUCTION")
    return render_template("login.html", next=nxt, show_demo=show_demo), status


@bp.route("/register", methods=["GET", "POST"])
def register():
    cfg = current_app.config
    db = get_db()
    if g.user:
        return redirect(home_for(g.user))
    if not cfg["ALLOW_REGISTRATION"]:
        return render_template("register_closed.html"), 403
    campuses = db.execute("SELECT * FROM campuses ORDER BY name").fetchall()
    form = {}
    if request.method == "POST":
        form = {k: clean(request.form.get(k), 160) for k in
                ("full_name", "user_id", "email", "role", "campus_id", "department", "program", "year_level")}
        form["email"] = form["email"].lower()
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        ip = client_ip()
        errors = []
        if rate_limited(db, "register", ip, cfg["REGISTER_MAX_PER_HOUR"], 3600):
            flash("Too many registrations from this network. Please try again later.", "error")
            return render_template("register.html", campuses=campuses, form=form, roles=SELF_REGISTER_ROLES), 429
        if len(form["full_name"]) < 2:
            errors.append("Enter your full name.")
        if not USER_ID_RE.match(form["user_id"]):
            errors.append("User ID must be 3–40 characters: letters, numbers, dot, dash or underscore.")
        if not valid_email(form["email"]):
            errors.append("Enter a valid email address.")
        if form["role"] not in SELF_REGISTER_ROLES:
            errors.append("Choose a valid role.")
        campus_id = None
        if campuses:
            if form["campus_id"].isdigit() and any(c["id"] == int(form["campus_id"]) for c in campuses):
                campus_id = int(form["campus_id"])
            else:
                errors.append("Choose your campus.")
        problems = password_problems(pw, form["user_id"], form["email"], cfg["PASSWORD_MIN_LENGTH"])
        if problems:
            errors.append("Password must have " + "; ".join(problems) + ".")
        elif pw != pw2:
            errors.append("The two passwords do not match.")
        if not errors:
            try:
                cur = db.execute("""INSERT INTO users(user_id,full_name,email,password_hash,role,campus_id,department,program,
                                    year_level,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                 (form["user_id"], form["full_name"], form["email"], hash_password(pw), form["role"], campus_id,
                                  form["department"][:120], form["program"][:120], form["year_level"][:20], iso(now())))
                rate_hit(db, "register", ip)
                audit(db, "account_registered", "user", cur.lastrowid, f"role={form['role']}", actor_id=cur.lastrowid)
                flash("Account created. You can now log in.", "success")
                return redirect(url_for("auth.login"))
            except sqlite3.IntegrityError:
                errors.append("That user ID or email is already registered.")
        for e in errors:
            flash(e, "error")
        return render_template("register.html", campuses=campuses, form=form, roles=SELF_REGISTER_ROLES), 400
    return render_template("register.html", campuses=campuses, form=form, roles=SELF_REGISTER_ROLES)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit(get_db(), "logout", "user", g.user["id"], "")
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))
