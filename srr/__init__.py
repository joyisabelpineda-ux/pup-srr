"""PUP Smart Room Reservation System (PUP SRR) — Flask application factory."""
import logging
import sqlite3

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from . import db as dbm
from . import security, services, util
from .config import load_config

ROOM_STATUS_CLASS = {"Available": "G", "Occupied": "R", "Reserved": "Y", "Under Maintenance": "N"}
RES_STATUS_CLASS = {"Pending": "Y", "Approved": "G", "In progress": "G", "Completed": "N", "Rejected": "R",
                    "Cancelled": "N", "Expired": "N", "Successful": "G", "Denied": "R"}


def create_app(overrides=None):
    app = Flask(__name__, instance_relative_config=False,
                template_folder="../templates", static_folder="../static")
    app.config.update(load_config(overrides))
    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    if not app.debug:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    dbm.init_db(app)
    app.teardown_appcontext(dbm.close_db)

    # ---- template helpers
    app.jinja_env.filters.update(dt=util.fmt_dt, dfmt=util.fmt_date, tfmt=util.fmt_time)
    app.jinja_env.globals.update(csrf_field=security.csrf_field, room_class=ROOM_STATUS_CLASS,
                                 status_class=RES_STATUS_CLASS, display_status=services.display_status,
                                 can_cancel=services.can_cancel)

    @app.context_processor
    def inject():
        return {"current_user": g.get("user"), "is_admin": bool(g.get("user") and g.user["role"] == "Administrator"),
                "room_statuses": dbm.ROOM_STATUSES, "res_statuses": dbm.RES_STATUSES,
                "now_str": util.iso(util.now())}

    # ---- request pipeline
    @app.before_request
    def before():
        if request.endpoint == "static":
            return None
        security.load_user()
        security.ensure_csrf()
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            security.check_csrf()
        if g.user and request.method == "GET":
            services.refresh_room_statuses(dbm.get_db())
        return None

    app.after_request(security.add_security_headers)

    # ---- blueprints
    from .views import admin, auth, main
    app.register_blueprint(auth.bp)
    app.register_blueprint(main.bp)
    app.register_blueprint(admin.bp)

    @app.route("/healthz")
    def healthz():
        """Liveness/readiness probe for hosting platforms (touches the database, reveals nothing)."""
        dbm.get_db().execute("SELECT 1").fetchone()
        return {"status": "ok"}, 200

    # ---- error handling (no stack traces or internals are ever shown to users)
    def render_error(code, title, message):
        return render_template("error.html", code=code, title=title, message=message), code

    @app.errorhandler(400)
    def bad_request(e):
        if getattr(e, "description", "") == "csrf":
            flash("Your form expired or was invalid. Please try again.", "error")
            return render_error(400, "Security check failed",
                                "Your session or form token was missing or expired. Go back, reload the page and try again.")
        return render_error(400, "Bad request", "The request could not be understood.")

    @app.errorhandler(403)
    def forbidden(e):
        return render_error(403, "Access denied", "You do not have permission to view this page.")

    @app.errorhandler(404)
    def missing(e):
        return render_error(404, "Not found", "The page or record you requested does not exist.")

    @app.errorhandler(405)
    def method_not_allowed(e):
        return render_error(405, "Method not allowed", "That action is not supported for this page.")

    @app.errorhandler(413)
    def too_large(e):
        return render_error(413, "Request too large", "The submitted data was too large.")

    @app.errorhandler(429)
    def too_many(e):
        return render_error(429, "Too many requests", "Please wait a few minutes and try again.")

    @app.errorhandler(sqlite3.Error)
    def db_error(e):
        app.logger.exception("Database error")
        return render_error(500, "Database error", "Something went wrong while accessing the database. Please try again.")

    @app.errorhandler(500)
    def server_error(e):
        return render_error(500, "Something went wrong", "An unexpected error occurred. It has been logged. Please try again.")

    @app.cli.command("init-db")
    def cli_init():
        """Create or upgrade the database."""
        dbm.init_db(app)
        print("Database ready at", app.config["DATABASE"])

    @app.cli.command("backup-db")
    def cli_backup():
        """Write a consistent copy of the database next to it (safe while the app is running)."""
        import sqlite3
        from datetime import datetime
        src = dbm.connect(app.config["DATABASE"])
        dest_path = f"{app.config['DATABASE']}.backup-{datetime.now():%Y%m%d-%H%M%S}"
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        print("Backup written to", dest_path)

    @app.cli.command("create-admin")
    def cli_admin():
        """Interactively create an administrator account."""
        import getpass
        uid = input("Admin user ID: ").strip()
        email = input("Email: ").strip().lower()
        name = input("Full name: ").strip() or "Administrator"
        pw = getpass.getpass("Password (min 10 chars): ")
        problems = util.password_problems(pw, uid, email, app.config["PASSWORD_MIN_LENGTH"])
        if not util.USER_ID_RE.match(uid) or not util.valid_email(email) or problems:
            raise SystemExit("Invalid input: " + ("; ".join(problems) or "check user ID and email"))
        conn = dbm.connect(app.config["DATABASE"])
        try:
            ok = dbm.bootstrap_admin(conn, uid, email, pw, name)
        finally:
            conn.close()
        print("Administrator created." if ok else "An account with that ID or email already exists.")

    return app
