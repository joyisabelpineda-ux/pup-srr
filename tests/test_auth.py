import os
import re
import sqlite3
import tempfile
import shutil
from tests.helpers import AppTestCase, make_app, PW, CSRF_RE
from srr.db import connect, LATEST_VERSION


class StartupTests(AppTestCase):
    def test_database_initialises_with_schema_and_seed_once(self):
        tables = {r["name"] for r in self.q("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("users", "rooms", "reservations", "campuses", "room_status_history", "access_logs", "audit_logs", "rate_limit_events"):
            self.assertIn(t, tables)
        self.assertEqual(self.q1("PRAGMA user_version")[0], LATEST_VERSION)
        before = (self.q1("SELECT COUNT(*) FROM rooms")[0], self.q1("SELECT COUNT(*) FROM users")[0], self.q1("SELECT COUNT(*) FROM campuses")[0])
        make_app(self.db_path)  # simulate an application restart
        make_app(self.db_path)
        after = (self.q1("SELECT COUNT(*) FROM rooms")[0], self.q1("SELECT COUNT(*) FROM users")[0], self.q1("SELECT COUNT(*) FROM campuses")[0])
        self.assertEqual(before, after)
        self.assertGreaterEqual(before[0], 5)

    def test_foreign_keys_enforced(self):
        c = self.db()
        self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO reservations(user_id,room_id,purpose,start_at,end_at,status,created_at) VALUES(9999,1,'x','2030-01-01 10:00:00','2030-01-01 11:00:00','Pending','now')")
        c.close()

    def test_login_page_and_security_headers(self):
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200)
        self.assertIn("PUP Smart Room Reservation", r.get_data(as_text=True))
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        for path in ("/static/css/app.css", "/static/js/app.js"):
            r2 = self.client.get(path)
            self.assertEqual(r2.status_code, 200)
            r2.close()

    def test_no_inline_styles_or_scripts_in_pages(self):
        c = self.client_for("DEMO-ADM-001")
        for path in ("/admin/", "/admin/rooms", "/admin/reservations", "/admin/users", "/dashboard", "/rooms", "/reserve", "/profile"):
            html = c.get(path).get_data(as_text=True)
            self.assertNotRegex(html, r'\sstyle="', path)
            self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)", path)
            self.assertNotRegex(html, r'\son(click|submit|change)=', path)


class RegistrationTests(AppTestCase):
    def reg(self, **over):
        data = {"full_name": "Maria Santos", "user_id": "2024-0001", "email": "maria@example.test", "role": "Student",
                "campus_id": str(self.q1("SELECT id FROM campuses")["id"]), "password": "Str0ngPass99", "password2": "Str0ngPass99"}
        data.update(over)
        return self.post("/register", data)

    def test_register_success_and_password_hashed(self):
        r = self.reg()
        self.assertEqual(r.status_code, 302)
        u = self.q1("SELECT * FROM users WHERE user_id='2024-0001'")
        self.assertIsNotNone(u)
        self.assertNotIn("Str0ngPass99", u["password_hash"])
        self.assertTrue(u["password_hash"].startswith(("pbkdf2:", "scrypt:")))
        self.assertEqual(u["role"], "Student")
        self.assertEqual(self.login("2024-0001", "Str0ngPass99").status_code, 302)

    def test_register_validation(self):
        for over, label in [({"email": "not-an-email", "user_id": "bad1"}, "email"), ({"password": "short1", "password2": "short1", "user_id": "bad2", "email": "b2@example.test"}, "short"),
                            ({"password": "alllettersnodigits", "password2": "alllettersnodigits", "user_id": "bad3", "email": "b3@example.test"}, "digits"),
                            ({"password2": "Different123", "user_id": "bad4", "email": "b4@example.test"}, "mismatch"),
                            ({"full_name": "", "user_id": "bad5", "email": "b5@example.test"}, "name"),
                            ({"role": "Administrator", "user_id": "bad6", "email": "b6@example.test"}, "admin-role")]:
            r = self.reg(**over)
            self.assertEqual(r.status_code, 400, label)
        self.assertEqual(self.q1("SELECT COUNT(*) FROM users WHERE user_id LIKE 'bad%'")[0], 0)

    def test_register_duplicates_case_insensitive(self):
        self.reg(user_id="dup-user", email="dup@example.test")
        r = self.reg(user_id="DUP-USER", email="other@example.test")
        self.assertEqual(r.status_code, 400)
        self.assertIn("already registered", r.get_data(as_text=True))
        r = self.reg(user_id="dup-user-2", email="dup@example.test")
        self.assertEqual(r.status_code, 400)

    def test_csrf_required_on_register_and_login(self):
        r = self.client.post("/register", data={"full_name": "x"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/login", data={"user_id": "DEMO-STU-001", "password": PW})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/login", data={"csrf": "forged", "user_id": "DEMO-STU-001", "password": PW})
        self.assertEqual(r.status_code, 400)


class LoginTests(AppTestCase):
    def test_valid_login_redirects_by_role_and_session_persists(self):
        r = self.login("DEMO-STU-001")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/dashboard"))
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertEqual(self.client.get("/profile").status_code, 200)
        admin = self.app.test_client()
        r = self.login("DEMO-ADM-001", client=admin)
        self.assertTrue(r.headers["Location"].endswith("/admin/"))
        self.assertEqual(self.login("student@example.test", client=self.app.test_client()).status_code, 302)  # email login

    def test_invalid_credentials_rejected_with_clear_message(self):
        for uid, pw in [("DEMO-STU-001", "WrongPass123"), ("nobody", PW), ("", "")]:
            r = self.login(uid, pw, client=self.app.test_client())
            self.assertEqual(r.status_code, 401, uid)
            self.assertIn("Invalid account ID/email or password", r.get_data(as_text=True))

    def test_logout_ends_session(self):
        self.login("DEMO-STU-001")
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertEqual(self.logout().status_code, 302)
        r = self.client.get("/dashboard")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        self.assertEqual(self.client.get("/logout").status_code, 405)  # logout must be POST

    def test_logout_requires_csrf(self):
        self.login("DEMO-STU-001")
        self.assertEqual(self.client.post("/logout").status_code, 400)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    def test_protected_pages_redirect_anonymous_users(self):
        for path in ("/dashboard", "/rooms", "/rooms/1", "/reserve", "/reservations", "/reservations/1", "/profile",
                     "/admin/", "/admin/rooms", "/admin/users", "/admin/reports/users.csv", "/admin/access-logs", "/admin/rooms/1/qr.png"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/login", r.headers["Location"], path)
        r = self.client.post("/reserve", data={})
        self.assertIn(r.status_code, (302, 400))

    def test_open_redirect_blocked(self):
        for nxt in ("https://evil.example/x", "//evil.example", "/\\evil.example", "javascript:alert(1)", "http:evil"):
            c = self.app.test_client()
            r = self.login("DEMO-STU-001", client=c, next=nxt)
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].endswith("/dashboard"), (nxt, r.headers["Location"]))
        c = self.app.test_client()
        r = self.login("DEMO-STU-001", client=c, next="/rooms?q=lab")
        self.assertTrue(r.headers["Location"].endswith("/rooms?q=lab"))

    def test_next_preserved_when_login_required(self):
        c = self.app.test_client()
        r = c.get("/reservations")
        self.assertEqual(r.headers["Location"], "/login?next=/reservations")

    def test_deactivated_account_cannot_login(self):
        self.run_sql("UPDATE users SET is_active=0 WHERE user_id='DEMO-PROF-001'")
        r = self.login("DEMO-PROF-001", client=self.app.test_client())
        self.assertEqual(r.status_code, 403)
        self.assertIn("deactivated", r.get_data(as_text=True))
        self.run_sql("UPDATE users SET is_active=1 WHERE user_id='DEMO-PROF-001'")

    def test_session_cookie_flags(self):
        r = self.login("DEMO-STU-001", client=self.app.test_client())
        cookie = r.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_no_secrets_in_pages(self):
        c = self.client_for("DEMO-ADM-001")
        for path in ("/profile", "/admin/users", "/admin/audit-logs"):
            html = c.get(path).get_data(as_text=True)
            self.assertNotIn("password_hash", html)
            self.assertNotIn("pbkdf2:", html)
            self.assertNotIn("scrypt:", html)
            self.assertNotIn("test-secret-key", html)


class RateLimitTests(AppTestCase):
    app_overrides = {"LOGIN_MAX_FAILS": 3}

    def test_lockout_after_repeated_failures(self):
        c = self.app.test_client()
        for _ in range(3):
            self.assertEqual(self.login("DEMO-PROF-001", "bad-password1", client=c).status_code, 401)
        r = self.login("DEMO-PROF-001", PW, client=c)  # correct password but locked out
        self.assertEqual(r.status_code, 429)
        self.assertIn("Too many failed attempts", r.get_data(as_text=True))


class RegistrationClosedTests(AppTestCase):
    app_overrides = {"ALLOW_REGISTRATION": False}

    def test_registration_can_be_disabled(self):
        self.assertEqual(self.client.get("/register").status_code, 403)


class ProductionConfigTests(AppTestCase):
    def test_production_requires_secret_key(self):
        old = dict(os.environ)
        try:
            os.environ["APP_ENV"] = "production"
            os.environ.pop("SECRET_KEY", None)
            from srr.config import load_config
            with self.assertRaises(RuntimeError):
                load_config()
            os.environ["SECRET_KEY"] = "short"
            with self.assertRaises(RuntimeError):
                load_config()
            os.environ["SECRET_KEY"] = "x" * 40
            cfg = load_config()
            self.assertTrue(cfg["SESSION_COOKIE_SECURE"])
            self.assertFalse(cfg["SEED_DEMO_USERS"])
        finally:
            os.environ.clear()
            os.environ.update(old)
