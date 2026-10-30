"""Authentication helpers, CSRF protection, role checks and security headers."""
import secrets
from functools import wraps

from flask import abort, current_app, flash, g, redirect, request, session, url_for
from markupsafe import Markup, escape

from .db import get_db
from .util import safe_next


def ensure_csrf():
    if not session.get("csrf"):
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_field():
    return Markup('<input type="hidden" name="csrf" value="%s">' % escape(ensure_csrf()))


def check_csrf():
    sent = request.form.get("csrf") or request.headers.get("X-CSRFToken", "")
    expected = session.get("csrf", "")
    if not expected or not sent or not secrets.compare_digest(sent, expected):
        abort(400, description="csrf")


def load_user():
    g.user = None
    uid = session.get("uid")
    if not uid:
        return
    row = get_db().execute(
        """SELECT u.*, c.name AS campus_name FROM users u LEFT JOIN campuses c ON c.id=u.campus_id
           WHERE u.id=? AND u.is_active=1""", (uid,)).fetchone()
    # session_nonce changes on password change/reset, which signs out other sessions
    if row is None or session.get("nonce", "") != (row["session_nonce"] or ""):
        session.clear()
        return
    g.user = row


def home_for(user):
    return url_for("admin.overview") if user and user["role"] == "Administrator" else url_for("main.dashboard")


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.user:
            if request.method == "GET":
                nxt = request.full_path.rstrip("?")
                flash("Please log in to continue.", "info")
                return redirect(url_for("auth.login", next=safe_next(nxt, "")) if safe_next(nxt, "") else url_for("auth.login"))
            flash("Your session has ended. Please log in again.", "info")
            return redirect(url_for("auth.login"))
        return fn(*a, **kw)
    return wrapper


def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapper(*a, **kw):
            if g.user["role"] not in roles:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


admin_required = roles_required("Administrator")


def add_security_headers(resp):
    cfg = current_app.config
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
    if cfg.get("PRODUCTION") or request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if g.get("user") or request.endpoint in ("auth.login", "auth.register"):
        resp.headers["Cache-Control"] = "no-store"
    return resp
