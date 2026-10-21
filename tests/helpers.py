"""Shared test helpers. Every test class gets its own temporary SQLite database."""
import os
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("SECRET_KEY", "test-secret-key-test-secret-key-0123456789")
os.environ.pop("APP_ENV", None)

from srr import create_app  # noqa: E402
from srr.db import connect  # noqa: E402

PW = "DemoPass123!"
CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


def make_app(path, **extra):
    cfg = dict(DATABASE=path, TESTING=True, PASSWORD_HASH_METHOD="pbkdf2:sha256:1000", ROOM_SYNC_INTERVAL=0,
               SEED_DEMO=True, SEED_DEMO_USERS=True, SECRET_KEY="test-secret-key-test-secret-key-0123456789",
               TIMEZONE="Asia/Manila")
    cfg.update(extra)
    return create_app(cfg)


class AppTestCase(unittest.TestCase):
    app_overrides = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pup_srr_test_")
        cls.db_path = os.path.join(cls.tmp, "test.sqlite3")
        cls.app = make_app(cls.db_path, **cls.app_overrides)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.client = self.app.test_client()

    # ---- helpers
    def db(self):
        return connect(self.db_path)

    def q(self, sql, *params):
        c = self.db()
        try:
            return c.execute(sql, params).fetchall()
        finally:
            c.close()

    def q1(self, sql, *params):
        rows = self.q(sql, *params)
        return rows[0] if rows else None

    def run_sql(self, sql, *params):
        c = self.db()
        try:
            cur = c.execute(sql, params)
            return cur.lastrowid
        finally:
            c.close()

    def csrf(self, client=None, path="/login"):
        client = client or self.client
        with client.session_transaction() as s:
            tok = s.get("csrf")
        if tok:
            return tok
        r = client.get(path)
        m = CSRF_RE.search(r.get_data(as_text=True))
        return m.group(1) if m else ""

    def login(self, uid, pw=PW, client=None, **kw):
        client = client or self.client
        tok = self.csrf(client)
        return client.post("/login", data={"csrf": tok, "user_id": uid, "password": pw, **kw})

    def logout(self, client=None):
        client = client or self.client
        return client.post("/logout", data={"csrf": self.csrf(client)})

    def post(self, path, data=None, client=None, **kw):
        client = client or self.client
        d = {"csrf": self.csrf(client)}
        d.update(data or {})
        return client.post(path, data=d, **kw)

    def client_for(self, uid, pw=PW):
        c = self.app.test_client()
        r = self.login(uid, pw, client=c)
        assert r.status_code == 302, f"login failed for {uid}: {r.status_code}"
        return c

    def now(self):
        with self.app.app_context():
            from srr.util import now
            return now()

    def future(self, days=3, hour=10):
        d = (self.now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0)
        return d.strftime("%Y-%m-%d")

    def room_id(self, number="101"):
        return self.q1("SELECT id FROM rooms WHERE room_number=?", number)["id"]

    def user_pk(self, uid):
        return self.q1("SELECT id FROM users WHERE user_id=?", uid)["id"]

    def reserve(self, client, room, date, start="10:00", end="11:00", purpose="Group study session"):
        return self.post("/reserve", {"room_id": str(room), "date": date, "start": start, "end": end, "purpose": purpose}, client=client)

    def insert_res(self, uid, room, start, end, status="Approved", purpose="direct insert"):
        return self.run_sql("""INSERT INTO reservations(user_id,room_id,purpose,start_at,end_at,status,created_at)
                               VALUES(?,?,?,?,?,?,?)""", self.user_pk(uid), room, purpose, start.strftime("%Y-%m-%d %H:%M:%S"),
                            end.strftime("%Y-%m-%d %H:%M:%S"), status, self.now().strftime("%Y-%m-%d %H:%M:%S"))
