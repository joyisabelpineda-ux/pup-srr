"""SQLite access, versioned migrations and idempotent seeding."""
import os
import secrets
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from flask import current_app, g
from werkzeug.security import generate_password_hash

from .util import iso, now

ROLES = ("Student", "Professor", "Faculty", "Staff", "Administrator")
SELF_REGISTER_ROLES = ("Student", "Professor", "Faculty", "Staff")
ROOM_STATUSES = ("Available", "Reserved", "Occupied", "Under Maintenance")
RES_STATUSES = ("Pending", "Approved", "Rejected", "Cancelled")


def connect(path, timeout=15):
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def transaction(db):
    """BEGIN IMMEDIATE takes the write lock up front so check-then-write sequences are serialized."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        db.execute("COMMIT")


# --------------------------------------------------------------------------- migrations
def _m1_baseline(c):
    """Original PUP SRR schema (kept so databases created by the earlier prototype upgrade cleanly)."""
    stmts = [
        "CREATE TABLE IF NOT EXISTS campuses(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, is_demo INTEGER NOT NULL DEFAULT 1)",
        """CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, user_id TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('Student','Professor','Faculty','Staff','Administrator')),
            campus_id INTEGER REFERENCES campuses(id), department TEXT DEFAULT '', program TEXT DEFAULT '',
            year_level TEXT DEFAULT '', profile_picture TEXT DEFAULT '', created_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1)""",
        """CREATE TABLE IF NOT EXISTS rooms(id INTEGER PRIMARY KEY, room_number TEXT NOT NULL, name TEXT NOT NULL,
            building TEXT NOT NULL, campus_id INTEGER NOT NULL REFERENCES campuses(id),
            capacity INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Available' CHECK(status IN ('Available','Reserved','Occupied','Under Maintenance')),
            qr_token TEXT NOT NULL UNIQUE, is_demo INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
            updated_by INTEGER REFERENCES users(id), UNIQUE(campus_id, building, room_number))""",
        """CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            room_id INTEGER NOT NULL REFERENCES rooms(id), purpose TEXT NOT NULL, start_at TEXT NOT NULL,
            end_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('Pending','Approved','Rejected','Cancelled')),
            created_at TEXT NOT NULL, reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT,
            review_note TEXT DEFAULT '', qr_token TEXT UNIQUE, CHECK(end_at > start_at))""",
        """CREATE TABLE IF NOT EXISTS room_status_history(id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id),
            previous_status TEXT NOT NULL, new_status TEXT NOT NULL, changed_at TEXT NOT NULL,
            reservation_start TEXT, reservation_end TEXT, source TEXT NOT NULL,
            changed_by INTEGER REFERENCES users(id), note TEXT DEFAULT '')""",
        """CREATE TABLE IF NOT EXISTS access_logs(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id),
            room_id INTEGER REFERENCES rooms(id), reservation_id INTEGER REFERENCES reservations(id),
            occurred_at TEXT NOT NULL, method TEXT NOT NULL,
            result TEXT NOT NULL CHECK(result IN ('Successful','Denied','Pending')),
            event_type TEXT NOT NULL CHECK(event_type IN ('QR scan attempt','Verified room entry','Access decision','System event')),
            reason TEXT DEFAULT '', campus_name TEXT DEFAULT '', building TEXT DEFAULT '',
            user_name_snapshot TEXT DEFAULT '', user_role_snapshot TEXT DEFAULT '')""",
        """CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES users(id),
            action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id INTEGER, occurred_at TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '')""",
        "CREATE INDEX IF NOT EXISTS idx_rooms_campus_status ON rooms(campus_id,status)",
        "CREATE INDEX IF NOT EXISTS idx_res_room_time_status ON reservations(room_id,start_at,end_at,status)",
        "CREATE INDEX IF NOT EXISTS idx_res_user_created ON reservations(user_id,created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_history_room_time ON room_status_history(room_id,changed_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_access_time_room ON access_logs(occurred_at DESC,room_id)",
        "CREATE INDEX IF NOT EXISTS idx_access_user ON access_logs(user_id,occurred_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_logs(occurred_at DESC)",
    ]
    for s in stmts:
        c.execute(s)


def _add_column(c, table, column, ddl):
    cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _m2_features(c):
    """Adds columns, rate-limit storage, app metadata, indexes and DB-level integrity triggers."""
    _add_column(c, "users", "last_login_at", "TEXT")
    _add_column(c, "users", "session_nonce", "TEXT NOT NULL DEFAULT ''")
    _add_column(c, "rooms", "description", "TEXT NOT NULL DEFAULT ''")
    _add_column(c, "rooms", "features", "TEXT NOT NULL DEFAULT ''")
    _add_column(c, "rooms", "status_source", "TEXT NOT NULL DEFAULT 'system'")
    _add_column(c, "reservations", "cancelled_at", "TEXT")
    _add_column(c, "reservations", "cancelled_by", "INTEGER REFERENCES users(id)")
    c.execute("CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("""CREATE TABLE IF NOT EXISTS rate_limit_events(id INTEGER PRIMARY KEY, bucket TEXT NOT NULL,
                 key TEXT NOT NULL, occurred_at REAL NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_rl_bucket_key ON rate_limit_events(bucket,key,occurred_at)")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_userid_ci ON users(lower(user_id))")
    c.execute("CREATE INDEX IF NOT EXISTS idx_res_status_start ON reservations(status,start_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_logs(actor_id,occurred_at DESC)")
    # Hard guarantee: two Approved reservations can never overlap in the same room.
    c.execute("DROP TRIGGER IF EXISTS trg_res_no_overlap_ins")
    c.execute("""CREATE TRIGGER trg_res_no_overlap_ins BEFORE INSERT ON reservations
        WHEN NEW.status='Approved'
        BEGIN
          SELECT RAISE(ABORT,'overlapping approved reservation')
          WHERE EXISTS(SELECT 1 FROM reservations WHERE room_id=NEW.room_id AND status='Approved'
                       AND start_at<NEW.end_at AND end_at>NEW.start_at);
        END""")
    c.execute("DROP TRIGGER IF EXISTS trg_res_no_overlap_upd")
    c.execute("""CREATE TRIGGER trg_res_no_overlap_upd BEFORE UPDATE OF status,start_at,end_at,room_id ON reservations
        WHEN NEW.status='Approved'
        BEGIN
          SELECT RAISE(ABORT,'overlapping approved reservation')
          WHERE EXISTS(SELECT 1 FROM reservations WHERE room_id=NEW.room_id AND status='Approved' AND id<>NEW.id
                       AND start_at<NEW.end_at AND end_at>NEW.start_at);
        END""")
    c.execute("DROP TRIGGER IF EXISTS trg_rooms_capacity_ins")
    c.execute("""CREATE TRIGGER trg_rooms_capacity_ins BEFORE INSERT ON rooms
        WHEN NEW.capacity<0 OR NEW.capacity>2000
        BEGIN SELECT RAISE(ABORT,'invalid room capacity'); END""")
    c.execute("DROP TRIGGER IF EXISTS trg_rooms_capacity_upd")
    c.execute("""CREATE TRIGGER trg_rooms_capacity_upd BEFORE UPDATE OF capacity ON rooms
        WHEN NEW.capacity<0 OR NEW.capacity>2000
        BEGIN SELECT RAISE(ABORT,'invalid room capacity'); END""")

def _m3_qr_checkin(c):
    """Store when each reservation was checked in."""
    _add_column(c, "reservations", "checked_in_at", "TEXT")
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_res_no_show
        ON reservations(status, start_at, checked_in_at)
    """)
    MIGRATIONS = [
    (1, _m1_baseline),
    (2, _m2_features),
    (3, _m3_qr_checkin),
],
LATEST_VERSION = MIGRATIONS[-1][0]


def migrate(conn, db_path=None):
    """Bring the schema to LATEST_VERSION without touching existing rows.

    An existing non-empty database is copied to a timestamped backup before its first upgrade.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= LATEST_VERSION:
        return False
    has_users = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if has_users and db_path and db_path != ":memory:" and os.path.exists(db_path):
        backup = f"{db_path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
        dest = sqlite3.connect(backup)
        try:
            conn.backup(dest)
        finally:
            dest.close()
    conn.execute("PRAGMA journal_mode=WAL")
    for v, fn in MIGRATIONS:
        if v <= version:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            fn(conn)
            conn.execute(f"PRAGMA user_version={v}")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    return True


# --------------------------------------------------------------------------- seeding
DEMO_PASSWORD = "DemoPass123!"
DEMO_CAMPUSES = ["PUP Main Campus (demo data)", "PUP Branch Campus (demo data)"]
DEMO_ROOMS = [
    # campus index, number, name, building, capacity, status, features
    (0, "101", "Lecture Room", "Main Building", 40, "Available", "Projector, Whiteboard, Air conditioning"),
    (0, "102", "Computer Lab", "Technology Building", 30, "Occupied", "30 workstations, Projector, Air conditioning"),
    (0, "103", "Conference Room", "Main Building", 12, "Available", "Video conferencing, Whiteboard"),
    (0, "104", "Study Room", "Library Building", 8, "Available", "Quiet zone, Whiteboard"),
    (0, "105", "Science Lab", "Science Building", 24, "Under Maintenance", "Lab benches, Fume hood"),
    (1, "201", "Seminar Hall", "Annex Building", 60, "Available", "Projector, Sound system, Air conditioning"),
    (1, "202", "Collaboration Room", "Annex Building", 10, "Available", "Whiteboard, Large display"),
    (1, "203", "Computer Lab 2", "Technology Annex", 25, "Available", "25 workstations, Projector"),
]
DEMO_USERS = [
    ("DEMO-STU-001", "Demo Student", "student@example.test", "Student", "Computer Studies", "BS Information Technology", "2nd year"),
    ("DEMO-PROF-001", "Demo Professor", "professor@example.test", "Professor", "Computer Studies", "", ""),
    ("DEMO-ADM-001", "Demo Administrator", "admin@example.test", "Administrator", "Administration", "", ""),
]


def hash_password(pw):
    method = current_app.config.get("PASSWORD_HASH_METHOD", "scrypt") if current_app else "scrypt"
    return generate_password_hash(pw, method=method)


def _meta_get(c, key):
    r = c.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def _meta_set(c, key, value):
    c.execute("INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def seed_demo(c, with_users=True):
    """Runs at most once per database (tracked in app_meta); never duplicates or overwrites records."""
    if _meta_get(c, "demo_seeded"):
        return False
    stamp = iso(now())
    c.execute("BEGIN IMMEDIATE")
    try:
        has_rooms = c.execute("SELECT COUNT(*) n FROM rooms").fetchone()["n"] > 0
        campus_ids = []
        if not has_rooms:
            for name in DEMO_CAMPUSES:
                c.execute("INSERT OR IGNORE INTO campuses(name,is_demo) VALUES(?,1)", (name,))
                campus_ids.append(c.execute("SELECT id FROM campuses WHERE name=?", (name,)).fetchone()["id"])
            for ci, num, name, bldg, cap, status, feats in DEMO_ROOMS:
                c.execute("""INSERT OR IGNORE INTO rooms(room_number,name,building,campus_id,capacity,status,qr_token,is_demo,
                             updated_at,features,status_source) VALUES(?,?,?,?,?,?,?,1,?,?,?)""",
                          (num, name, bldg, campus_ids[ci], cap, status, secrets.token_urlsafe(24), stamp, feats,
                           "manual" if status != "Available" else "system"))
        first_campus = c.execute("SELECT id FROM campuses ORDER BY id LIMIT 1").fetchone()
        campus = first_campus["id"] if first_campus else None
        for uid, name, email, role, dept, prog, yr in (DEMO_USERS if with_users else []):
            exists = c.execute("SELECT 1 FROM users WHERE lower(user_id)=lower(?) OR email=?", (uid, email)).fetchone()
            if not exists:
                c.execute("""INSERT INTO users(user_id,full_name,email,password_hash,role,campus_id,department,program,year_level,created_at)
                             VALUES(?,?,?,?,?,?,?,?,?,?)""",
                          (uid, name, email, hash_password(DEMO_PASSWORD), role, campus, dept, prog, yr, stamp))
        _meta_set(c, "demo_seeded", stamp)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return True


def bootstrap_admin(c, user_id, email, password, name="System Administrator"):
    """Create an administrator from ADMIN_* settings if (and only if) that account does not exist yet."""
    if not (user_id and email and password):
        return False
    if c.execute("SELECT 1 FROM users WHERE lower(user_id)=lower(?) OR email=?", (user_id, email.lower())).fetchone():
        return False
    campus = c.execute("SELECT id FROM campuses ORDER BY id LIMIT 1").fetchone()
    c.execute("""INSERT INTO users(user_id,full_name,email,password_hash,role,campus_id,created_at)
                 VALUES(?,?,?,?,'Administrator',?,?)""",
              (user_id, name, email.lower(), hash_password(password), campus["id"] if campus else None, iso(now())))
    return True


def init_db(app=None):
    """Create/upgrade the database and apply configured seeding. Safe to run on every start."""
    app = app or current_app
    with app.app_context():
        path = app.config["DATABASE"]
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        conn = connect(path)
        try:
            migrate(conn, path)
            if app.config.get("SEED_DEMO"):
                seed_demo(conn, with_users=app.config.get("SEED_DEMO_USERS", False))
            cfg = app.config
            if cfg.get("ADMIN_USER_ID") and cfg.get("ADMIN_PASSWORD"):
                created = bootstrap_admin(conn, cfg["ADMIN_USER_ID"], cfg["ADMIN_EMAIL"] or f"{cfg['ADMIN_USER_ID'].lower()}@pup-srr.local",
                                          cfg["ADMIN_PASSWORD"], cfg["ADMIN_NAME"])
                if created:
                    app.logger.info("Administrator account %s created from environment settings.", cfg["ADMIN_USER_ID"])
        finally:
            conn.close()
