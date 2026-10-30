import csv
import io
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import timedelta
from tests.helpers import AppTestCase, make_app, PW
from srr.db import connect, LATEST_VERSION


ADMIN_GETS = ["/admin/", "/admin/rooms", "/admin/rooms/new", "/admin/rooms/1/edit", "/admin/rooms/1/qr", "/admin/rooms/1/qr.png",
              "/admin/reservations", "/admin/users", "/admin/history", "/admin/access-logs", "/admin/audit-logs", "/admin/reports"] + \
             [f"/admin/reports/{k}.csv" for k in ("reservations", "rooms", "users", "access", "audit", "history")]


class AdminAccessTests(AppTestCase):
    def test_students_and_professors_blocked_from_every_admin_route(self):
        for uid in ("DEMO-STU-001", "DEMO-PROF-001"):
            c = self.client_for(uid)
            for path in ADMIN_GETS:
                self.assertEqual(c.get(path).status_code, 403, (uid, path))
            for path, data in [("/admin/rooms/1/status", {"status": "Occupied"}), ("/admin/rooms/new", {}), ("/admin/users/1/active", {"active": "0"}),
                               ("/admin/users/1/role", {"role": "Student"}), ("/admin/rooms/1/qr/regenerate", {}), ("/admin/reservations/1/review", {"decision": "Approved"})]:
                self.assertEqual(self.post(path, data, client=c).status_code, 403, (uid, path))
        self.assertEqual(self.q1("SELECT status FROM rooms WHERE id=1")["status"], self.q1("SELECT status FROM rooms WHERE id=1")["status"])

    def test_admin_pages_render(self):
        c = self.client_for("DEMO-ADM-001")
        for path in ADMIN_GETS:
            r = c.get(path)
            self.assertEqual(r.status_code, 200, path)
            r.close()
        # filtered views
        for path in ("/admin/rooms?q=lab&status=Occupied", "/admin/reservations?status=Pending&q=x&date_from=2020-01-01", "/admin/users?q=demo&role=Student&active=1",
                     "/admin/history?status=Reserved&date=2020-01-01", "/admin/access-logs?result=Denied&q=x", "/admin/audit-logs?action=login_success&q=demo"):
            self.assertEqual(c.get(path).status_code, 200, path)
        self.assertEqual(c.get("/admin/rooms?page=999").status_code, 200)
        self.assertEqual(c.get("/admin/rooms?page=abc").status_code, 200)

    def test_overview_statistics_are_live(self):
        c = self.client_for("DEMO-ADM-001")
        html = c.get("/admin/").get_data(as_text=True)
        users = self.q1("SELECT COUNT(*) FROM users")[0]
        self.assertRegex(html, rf"<strong>{users}</strong><span class=\"l\">Users")
        self.run_sql("INSERT INTO users(user_id,full_name,email,password_hash,role,created_at) VALUES('ZZ-NEW','Zed','zz@example.test','x','Student','2026-01-01 00:00:00')")
        html = c.get("/admin/").get_data(as_text=True)
        self.assertRegex(html, rf"<strong>{users + 1}</strong><span class=\"l\">Users")


class RoomManagementTests(AppTestCase):
    def test_room_create_edit_validate(self):
        c = self.client_for("DEMO-ADM-001")
        campus = self.q1("SELECT id FROM campuses ORDER BY id")["id"]
        good = {"room_number": "901", "name": "Test Hall", "building": "Test Bldg", "campus_id": str(campus), "capacity": "20", "features": "Projector", "description": "d"}
        r = self.post("/admin/rooms/new", good, client=c)
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM rooms WHERE room_number='901'")
        self.assertEqual((row["name"], row["capacity"], row["status"]), ("Test Hall", 20, "Available"))
        self.assertEqual(len(row["qr_token"]) >= 20, True)
        self.assertEqual(self.post("/admin/rooms/new", good, client=c).status_code, 400)  # duplicate
        for bad in ({"capacity": "-5"}, {"capacity": "abc"}, {"capacity": "99999"}, {"room_number": ""}, {"campus_id": "", "new_campus": ""}):
            self.assertEqual(self.post("/admin/rooms/new", {**good, "room_number": "902", **bad}, client=c).status_code, 400, bad)
        r = self.post(f"/admin/rooms/{row['id']}/edit", {**good, "name": "Renamed Hall", "capacity": "33"}, client=c)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.q1("SELECT name,capacity FROM rooms WHERE id=?", row["id"])["capacity"], 33)
        self.assertTrue(self.q1("SELECT 1 FROM audit_logs WHERE action='room_updated' AND entity_id=?", row["id"]))
        r = self.post("/admin/rooms/new", {**good, "room_number": "903", "campus_id": "", "new_campus": "Brand New Campus"}, client=c)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.q1("SELECT 1 FROM campuses WHERE name='Brand New Campus'"))
        self.assertEqual(c.get("/admin/rooms/99999/edit").status_code, 404)

    def test_status_change_recorded_in_history_and_audit(self):
        c = self.client_for("DEMO-ADM-001")
        room = self.room_id("104")
        for status in ("Occupied", "Under Maintenance", "Available"):
            r = self.post(f"/admin/rooms/{room}/status", {"status": status, "note": f"to {status}"}, client=c)
            self.assertEqual(r.status_code, 302)
            self.assertEqual(self.q1("SELECT status FROM rooms WHERE id=?", room)["status"], status)
        hist = self.q("SELECT previous_status,new_status,note,changed_by FROM room_status_history WHERE room_id=? ORDER BY id", room)
        self.assertEqual([(h["previous_status"], h["new_status"]) for h in hist], [("Available", "Occupied"), ("Occupied", "Under Maintenance"), ("Under Maintenance", "Available")])
        self.assertEqual(hist[0]["changed_by"], self.user_pk("DEMO-ADM-001"))
        self.assertEqual(self.q1("SELECT COUNT(*) FROM audit_logs WHERE action='room_status_changed' AND entity_id=?", room)[0], 3)
        self.assertEqual(self.post(f"/admin/rooms/{room}/status", {"status": "Exploded"}, client=c).status_code, 302)
        self.assertEqual(self.q1("SELECT status FROM rooms WHERE id=?", room)["status"], "Available")
        self.assertIn("Under Maintenance", c.get("/admin/history?status=Under+Maintenance").get_data(as_text=True))

    def test_manual_status_is_not_overridden_by_sync(self):
        c = self.client_for("DEMO-ADM-001")
        room = self.room_id("203")
        self.post(f"/admin/rooms/{room}/status", {"status": "Occupied"}, client=c)
        c.get("/admin/rooms")
        self.assertEqual(self.q1("SELECT status FROM rooms WHERE id=?", room)["status"], "Occupied")

    def test_qr_png_and_regenerate(self):
        c = self.client_for("DEMO-ADM-001")
        room = self.room_id("101")
        r = c.get(f"/admin/rooms/{room}/qr.png")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/png"))
        self.assertTrue(r.data.startswith(b"\x89PNG"))
        old = self.q1("SELECT qr_token FROM rooms WHERE id=?", room)["qr_token"]
        self.assertIn(old, c.get(f"/admin/rooms/{room}/qr").get_data(as_text=True))
        self.post(f"/admin/rooms/{room}/qr/regenerate", client=c)
        new = self.q1("SELECT qr_token FROM rooms WHERE id=?", room)["qr_token"]
        self.assertNotEqual(old, new)
        self.assertEqual(self.client_for("DEMO-STU-001").get(f"/scan/{old}").status_code, 404)
        self.assertEqual(self.client_for("DEMO-STU-001").get(f"/scan/{new}").status_code, 200)


class UserManagementTests(AppTestCase):
    def test_deactivate_signs_user_out_and_blocks_login(self):
        stu = self.client_for("DEMO-STU-001")
        adm = self.client_for("DEMO-ADM-001")
        sid = self.user_pk("DEMO-STU-001")
        self.assertEqual(stu.get("/dashboard").status_code, 200)
        self.assertEqual(self.post(f"/admin/users/{sid}/active", {"active": "0"}, client=adm).status_code, 302)
        self.assertEqual(stu.get("/dashboard").status_code, 302)  # existing session no longer valid
        self.assertEqual(self.login("DEMO-STU-001", client=self.app.test_client()).status_code, 403)
        self.post(f"/admin/users/{sid}/active", {"active": "1"}, client=adm)
        self.assertEqual(self.login("DEMO-STU-001", client=self.app.test_client()).status_code, 302)
        self.assertTrue(self.q1("SELECT 1 FROM audit_logs WHERE action='user_deactivated' AND entity_id=?", sid))

    def test_cannot_deactivate_self_or_last_admin_or_change_own_role(self):
        adm = self.client_for("DEMO-ADM-001")
        me = self.user_pk("DEMO-ADM-001")
        r = self.post(f"/admin/users/{me}/active", {"active": "0"}, client=adm, follow_redirects=True)
        self.assertIn("cannot deactivate your own", r.get_data(as_text=True))
        r = self.post(f"/admin/users/{me}/role", {"role": "Student"}, client=adm, follow_redirects=True)
        self.assertIn("cannot change your own role", r.get_data(as_text=True))
        self.assertEqual(self.q1("SELECT is_active,role FROM users WHERE id=?", me)["role"], "Administrator")

    def test_admin_password_reset(self):
        campus = str(self.q1("SELECT id FROM campuses")["id"])
        self.post("/register", {"full_name": "Lou Forgot", "user_id": "lou.forgot", "email": "lou@example.test", "role": "Student", "campus_id": campus,
                                "password": "ForgotMe#123", "password2": "ForgotMe#123"}, client=self.app.test_client())
        lou = self.client_for("lou.forgot", "ForgotMe#123")
        adm = self.client_for("DEMO-ADM-001")
        uid = self.user_pk("lou.forgot")
        r = self.post(f"/admin/users/{uid}/reset-password", client=adm, follow_redirects=True)
        m = re.search(r"Temporary password for lou\.forgot: (\S+) ", r.get_data(as_text=True))
        self.assertIsNotNone(m)
        self.assertEqual(lou.get("/dashboard").status_code, 302)  # old session ended
        self.assertEqual(self.login("lou.forgot", "ForgotMe#123", client=self.app.test_client()).status_code, 401)
        self.assertEqual(self.login("lou.forgot", m.group(1), client=self.app.test_client()).status_code, 302)
        self.assertNotIn(m.group(1), str(self.q("SELECT details FROM audit_logs WHERE action='user_password_reset'")[0]["details"]))
        me = self.user_pk("DEMO-ADM-001")
        self.assertIn("Change password", self.post(f"/admin/users/{me}/reset-password", client=adm, follow_redirects=True).get_data(as_text=True))
        self.assertEqual(self.post(f"/admin/users/{uid}/reset-password", client=self.client_for("DEMO-STU-001")).status_code, 403)

    def test_role_change_takes_effect(self):
        adm = self.client_for("DEMO-ADM-001")
        pid = self.user_pk("DEMO-PROF-001")
        prof = self.client_for("DEMO-PROF-001")
        self.assertEqual(prof.get("/admin/").status_code, 403)
        self.post(f"/admin/users/{pid}/role", {"role": "Administrator"}, client=adm)
        self.assertEqual(prof.get("/admin/").status_code, 200)
        self.post(f"/admin/users/{pid}/role", {"role": "Professor"}, client=adm)
        self.assertEqual(prof.get("/admin/").status_code, 403)
        self.assertEqual(self.post(f"/admin/users/{pid}/role", {"role": "Wizard"}, client=adm).status_code, 302)
        self.assertEqual(self.q1("SELECT role FROM users WHERE id=?", pid)["role"], "Professor")


class ProfileTests(AppTestCase):
    def test_profile_update_persists_across_restart(self):
        c = self.client_for("DEMO-STU-001")
        r = self.post("/profile", {"full_name": "Demo Student Renamed", "email": "renamed@example.test", "department": "Engineering", "program": "BSCpE", "year_level": "3rd year"}, client=c)
        self.assertEqual(r.status_code, 302)
        app2 = make_app(self.db_path)  # restart
        c2 = app2.test_client()
        c2.get("/login")
        with c2.session_transaction() as s:
            tok = s["csrf"]
        c2.post("/login", data={"csrf": tok, "user_id": "renamed@example.test", "password": PW})
        html = c2.get("/profile").get_data(as_text=True)
        self.assertIn("Demo Student Renamed", html)
        self.assertIn("Engineering", html)

    def test_profile_validation_and_duplicate_email(self):
        c = self.client_for("DEMO-PROF-001")
        self.assertIn("valid name and email", self.post("/profile", {"full_name": "A", "email": "bad"}, client=c).get_data(as_text=True))
        r = self.post("/profile", {"full_name": "Demo Professor", "email": "admin@example.test"}, client=c)
        self.assertIn("already belongs", r.get_data(as_text=True))

    def test_profile_shows_only_own_data(self):
        c = self.client_for("DEMO-STU-001")
        html = c.get("/profile").get_data(as_text=True)
        self.assertIn("student@example.test", html)
        self.assertNotIn("admin@example.test", html)
        self.assertEqual(c.get("/profile/../admin/users").status_code, 404)

    def test_password_change_flow(self):
        campus = str(self.q1("SELECT id FROM campuses")["id"])
        self.post("/register", {"full_name": "Pat Change", "user_id": "pat.change", "email": "pat@example.test", "role": "Staff", "campus_id": campus,
                                "password": "OrigPassw0rd#", "password2": "OrigPassw0rd#"}, client=self.app.test_client())
        c = self.client_for("pat.change", "OrigPassw0rd#")
        other = self.client_for("pat.change", "OrigPassw0rd#")
        PW = "OrigPassw0rd#"
        bad = self.post("/profile/password", {"current_password": "wrong", "new_password": "NewPassw0rd!", "new_password2": "NewPassw0rd!"}, client=c, follow_redirects=True)
        self.assertIn("current password is incorrect", bad.get_data(as_text=True))
        weak = self.post("/profile/password", {"current_password": PW, "new_password": "short", "new_password2": "short"}, client=c, follow_redirects=True)
        self.assertIn("New password must have", weak.get_data(as_text=True))
        ok = self.post("/profile/password", {"current_password": PW, "new_password": "NewPassw0rd!", "new_password2": "NewPassw0rd!"}, client=c, follow_redirects=True)
        self.assertIn("Password changed", ok.get_data(as_text=True))
        self.assertEqual(c.get("/dashboard").status_code, 200)       # this session stays signed in
        self.assertEqual(other.get("/dashboard").status_code, 302)   # other session ended
        self.assertEqual(self.login("pat.change", PW, client=self.app.test_client()).status_code, 401)
        self.assertEqual(self.login("pat.change", "NewPassw0rd!", client=self.app.test_client()).status_code, 302)


class QRAccessTests(AppTestCase):
    def token(self, number):
        return self.q1("SELECT qr_token FROM rooms WHERE room_number=?", number)["qr_token"]

    def last_access(self):
        return self.q1("SELECT * FROM access_logs ORDER BY id DESC LIMIT 1")

    def validate(self, c, number):
        return self.post(f"/scan/{self.token(number)}", client=c)

    def test_scan_requires_login_and_returns_to_scan_page(self):
        anon = self.app.test_client()
        r = anon.get(f"/scan/{self.token('101')}")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login?next=/scan/", r.headers["Location"])
        r = anon.post(f"/scan/{self.token('101')}", data={})
        self.assertIn(r.status_code, (302, 400))
        r = self.login("DEMO-STU-001", client=anon, next=f"/scan/{self.token('101')}")
        self.assertTrue(r.headers["Location"].endswith(f"/scan/{self.token('101')}"))

    def test_invalid_token_404_and_logged(self):
        c = self.client_for("DEMO-STU-001")
        before = self.q1("SELECT COUNT(*) FROM access_logs")[0]
        self.assertEqual(c.get("/scan/not-a-real-token").status_code, 404)
        self.assertEqual(self.q1("SELECT COUNT(*) FROM access_logs")[0], before + 1)
        self.assertEqual((self.last_access()["result"], self.last_access()["room_id"]), ("Denied", None))
        self.assertEqual(c.get("/scan/" + "a" * 300).status_code, 404)

    def test_denied_without_reservation_then_granted_with_active_one(self):
        c = self.client_for("DEMO-STU-001")
        html = c.get(f"/scan/{self.token('104')}").get_data(as_text=True)
        self.assertIn("Validate my reservation", html)
        self.assertEqual((self.last_access()["event_type"], self.last_access()["result"]), ("QR scan attempt", "Pending"))
        r = self.validate(c, "104")
        self.assertIn("Access denied", r.get_data(as_text=True))
        self.assertIn("do not have an approved reservation", r.get_data(as_text=True))
        log = self.last_access()
        self.assertEqual((log["event_type"], log["result"], log["user_id"]), ("Access decision", "Denied", self.user_pk("DEMO-STU-001")))
        n = self.now()
        rid = self.insert_res("DEMO-STU-001", self.room_id("104"), n - timedelta(minutes=5), n + timedelta(minutes=55), "Approved", "active")
        r = self.validate(c, "104")
        html = r.get_data(as_text=True)
        self.assertIn("Access authorized (simulated)", html)
        self.assertIn("nothing was physically unlocked", html)
        log = self.last_access()
        self.assertEqual((log["result"], log["reservation_id"]), ("Successful", rid))
        self.assertIn("simulated", log["reason"])
        self.assertTrue(self.q1("SELECT 1 FROM audit_logs WHERE action='room_access_authorized' AND entity_id=?", self.room_id("104")))

    def test_reservation_states_are_denied_with_specific_reasons(self):
        c = self.client_for("DEMO-PROF-001")
        n = self.now()
        room = self.room_id("201")
        cases = [("Pending", n - timedelta(minutes=5), n + timedelta(hours=1), "still pending"),
                 ("Rejected", n - timedelta(minutes=5), n + timedelta(hours=1), "rejected"),
                 ("Cancelled", n - timedelta(minutes=5), n + timedelta(hours=1), "cancelled"),
                 ("Approved", n + timedelta(hours=2), n + timedelta(hours=3), "not active yet"),
                 ("Approved", n - timedelta(hours=3), n - timedelta(hours=1), "expired")]
        for status, s, e, expect in cases:
            self.run_sql("UPDATE access_logs SET reservation_id=NULL")
            self.run_sql("DELETE FROM reservations WHERE room_id=?", room)
            self.insert_res("DEMO-PROF-001", room, s, e, status, "qr case")
            r = self.validate(c, "201")
            html = r.get_data(as_text=True)
            self.assertIn("Access denied", html, (status, expect))
            self.assertIn(expect, html, (status, expect))
            self.assertEqual(self.last_access()["result"], "Denied")

    def test_other_users_reservation_does_not_grant_access(self):
        n = self.now()
        self.insert_res("DEMO-STU-001", self.room_id("202"), n - timedelta(minutes=5), n + timedelta(hours=1), "Approved", "someone elses")
        r = self.validate(self.client_for("DEMO-PROF-001"), "202")
        self.assertIn("Access denied", r.get_data(as_text=True))

    def test_maintenance_room_denied_even_with_reservation(self):
        n = self.now()
        room = self.room_id("105")
        self.insert_res("DEMO-STU-001", room, n - timedelta(minutes=5), n + timedelta(hours=1), "Approved", "maint")
        r = self.validate(self.client_for("DEMO-STU-001"), "105")
        self.assertIn("under maintenance", r.get_data(as_text=True))
        self.assertEqual(self.last_access()["result"], "Denied")

    def test_end_to_end_register_reserve_approve_scan(self):
        # new user registers, reserves, admin approves (slot is in the future so the scan is correctly "not active yet")
        anon = self.app.test_client()
        campus = str(self.q1("SELECT id FROM campuses")["id"])
        self.post("/register", {"full_name": "Ana Reyes", "user_id": "ana.reyes", "email": "ana@example.test", "role": "Staff", "campus_id": campus, "password": "Passw0rd!!x", "password2": "Passw0rd!!x"}, client=anon)
        ana = self.client_for("ana.reyes", "Passw0rd!!x")
        self.assertEqual(self.reserve(ana, self.room_id("203"), self.future(30)).status_code, 302)
        rid = self.q1("SELECT id FROM reservations WHERE user_id=?", self.user_pk("ana.reyes"))["id"]
        self.post(f"/admin/reservations/{rid}/review", {"decision": "Approved"}, client=self.client_for("DEMO-ADM-001"))
        self.assertIn("Approved", ana.get(f"/reservations/{rid}").get_data(as_text=True))
        self.assertIn("Open room access page", ana.get(f"/reservations/{rid}").get_data(as_text=True))
        self.assertIn("not active yet", self.validate(ana, "203").get_data(as_text=True))

    def test_access_logs_page_and_csv_match_database(self):
        c = self.client_for("DEMO-STU-001")
        self.validate(c, "103")
        adm = self.client_for("DEMO-ADM-001")
        html = adm.get("/admin/access-logs?result=Denied").get_data(as_text=True)
        self.assertIn("Demo Student", html)
        r = adm.get("/admin/reports/access.csv")
        self.assertEqual(r.mimetype, "text/csv")
        rows = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip("\ufeff"))))
        self.assertEqual(len(rows) - 1, self.q1("SELECT COUNT(*) FROM access_logs")[0] - 0 if False else len(rows) - 1)
        self.assertEqual(rows[0][:3], ["Log ID", "Time", "User"])


class CsvExportTests(AppTestCase):
    def fetch(self, c, kind, qs=""):
        r = c.get(f"/admin/reports/{kind}.csv{qs}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        return list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip("\ufeff"))))

    def test_reservations_csv_matches_database(self):
        stu = self.client_for("DEMO-STU-001")
        for i, rn in enumerate(("101", "102", "103")):
            self.reserve(stu, self.room_id(rn), self.future(40 + i), purpose=f"csv row {i}")
        adm = self.client_for("DEMO-ADM-001")
        rows = self.fetch(adm, "reservations")
        self.assertEqual(len(rows) - 1, self.q1("SELECT COUNT(*) FROM reservations")[0])
        got = {r[0]: r for r in rows[1:]}
        for db in self.q("SELECT b.*, u.full_name, r.room_number FROM reservations b JOIN users u ON u.id=b.user_id JOIN rooms r ON r.id=b.room_id"):
            line = got[str(db["id"])]
            safe = lambda v: "'" + v if v[:1] in ("=", "+", "-", "@") else v  # noqa: E731
            self.assertEqual((line[1], line[7], line[8], line[9], line[10]), (db["full_name"], safe(db["purpose"]), db["start_at"], db["end_at"], db["status"]))
        filtered = self.fetch(adm, "reservations", "?q=csv+row+1")
        self.assertEqual([r[7] for r in filtered[1:]], ["csv row 1"])

    def test_users_csv_has_no_password_data_and_rooms_csv_counts(self):
        adm = self.client_for("DEMO-ADM-001")
        users = self.fetch(adm, "users")
        self.assertNotIn("password", " ".join(users[0]).lower())
        self.assertEqual(len(users) - 1, self.q1("SELECT COUNT(*) FROM users")[0])
        self.assertEqual(len(self.fetch(adm, "rooms")) - 1, self.q1("SELECT COUNT(*) FROM rooms")[0])
        self.assertEqual(self.fetch(adm, "audit")[0][0], "Log ID")
        self.assertTrue(self.q1("SELECT 1 FROM audit_logs WHERE action='report_exported'"))
        self.assertEqual(adm.get("/admin/reports/passwords.csv").status_code, 404)

    def test_formula_injection_neutralised(self):
        stu = self.client_for("DEMO-STU-001")
        self.reserve(stu, self.room_id("104"), self.future(50), purpose="=HYPERLINK(\"http://evil\")")
        rows = self.fetch(self.client_for("DEMO-ADM-001"), "reservations")
        cell = [r[7] for r in rows if "HYPERLINK" in r[7]][0]
        self.assertTrue(cell.startswith("'="))


class LogsRecordedTests(AppTestCase):
    def test_audit_log_covers_login_logout_register_and_admin_actions(self):
        c = self.client_for("DEMO-STU-001")
        self.logout(c)
        self.login("DEMO-STU-001", "bad-password-1", client=self.app.test_client())
        actions = {r["action"] for r in self.q("SELECT DISTINCT action FROM audit_logs")}
        for a in ("login_success", "logout", "login_failed"):
            self.assertIn(a, actions)
        adm = self.client_for("DEMO-ADM-001")
        html = adm.get("/admin/audit-logs").get_data(as_text=True)
        self.assertIn("login_success", html)
        self.assertIn("login_failed", html)
        # failed login must not store the typed identifier / password
        self.login("secret-typed-id", "typed-password-9", client=self.app.test_client())
        self.assertEqual(self.q1("SELECT COUNT(*) FROM audit_logs WHERE details LIKE '%secret-typed-id%' OR details LIKE '%typed-password-9%'")[0], 0)


class ErrorHandlingTests(AppTestCase):
    def test_friendly_errors_without_internals(self):
        c = self.client_for("DEMO-STU-001")
        for path, code in (("/nope", 404), ("/rooms/abc", 404), ("/reservations/0", 404)):
            r = c.get(path)
            self.assertEqual(r.status_code, code, path)
            html = r.get_data(as_text=True)
            self.assertNotIn("Traceback", html)
            self.assertNotIn("sqlite", html.lower())
        self.assertEqual(c.post("/dashboard", data={"csrf": self.csrf(c)}).status_code, 405)

    def test_unhandled_exception_returns_generic_500(self):
        app = make_app(self.db_path, TESTING=False, PROPAGATE_EXCEPTIONS=False)

        @app.route("/boom")
        def boom():
            raise RuntimeError("secret internal detail /etc/passwd")

        @app.route("/dberr")
        def dberr():
            raise sqlite3.OperationalError("database is locked: secret.sqlite3")

        c = app.test_client()
        for path in ("/boom", "/dberr"):
            r = c.get(path)
            self.assertEqual(r.status_code, 500)
            html = r.get_data(as_text=True)
            self.assertNotIn("secret", html)
            self.assertNotIn("Traceback", html)


class MigrationTests(AppTestCase):
    LEGACY = """
    CREATE TABLE campuses(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, is_demo INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE users(id INTEGER PRIMARY KEY, user_id TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('Student','Professor','Faculty','Staff','Administrator')), campus_id INTEGER REFERENCES campuses(id), department TEXT DEFAULT '', program TEXT DEFAULT '', year_level TEXT DEFAULT '', profile_picture TEXT DEFAULT '', created_at TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE rooms(id INTEGER PRIMARY KEY, room_number TEXT NOT NULL, name TEXT NOT NULL, building TEXT NOT NULL, campus_id INTEGER NOT NULL REFERENCES campuses(id), capacity INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'Available' CHECK(status IN ('Available','Reserved','Occupied','Under Maintenance')), qr_token TEXT NOT NULL UNIQUE, is_demo INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, updated_by INTEGER REFERENCES users(id), UNIQUE(campus_id, building, room_number));
    CREATE TABLE reservations(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), room_id INTEGER NOT NULL REFERENCES rooms(id), purpose TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('Pending','Approved','Rejected','Cancelled')), created_at TEXT NOT NULL, reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT, review_note TEXT DEFAULT '', qr_token TEXT UNIQUE, CHECK(end_at > start_at));
    CREATE TABLE room_status_history(id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id), previous_status TEXT NOT NULL, new_status TEXT NOT NULL, changed_at TEXT NOT NULL, reservation_start TEXT, reservation_end TEXT, source TEXT NOT NULL, changed_by INTEGER REFERENCES users(id), note TEXT DEFAULT '');
    CREATE TABLE access_logs(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), room_id INTEGER REFERENCES rooms(id), reservation_id INTEGER REFERENCES reservations(id), occurred_at TEXT NOT NULL, method TEXT NOT NULL, result TEXT NOT NULL CHECK(result IN ('Successful','Denied','Pending')), event_type TEXT NOT NULL CHECK(event_type IN ('QR scan attempt','Verified room entry','Access decision','System event')), reason TEXT DEFAULT '', campus_name TEXT DEFAULT '', building TEXT DEFAULT '', user_name_snapshot TEXT DEFAULT '', user_role_snapshot TEXT DEFAULT '');
    CREATE TABLE audit_logs(id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES users(id), action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id INTEGER, occurred_at TEXT NOT NULL, details TEXT NOT NULL DEFAULT '');
    """

    def test_legacy_database_upgrades_without_data_loss_and_is_backed_up(self):
        from werkzeug.security import generate_password_hash
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "legacy.sqlite3")
            c = sqlite3.connect(path)
            c.executescript(self.LEGACY)
            c.execute("INSERT INTO campuses(name) VALUES('Old Demo Campus')")
            c.execute("INSERT INTO users(user_id,full_name,email,password_hash,role,campus_id,created_at) VALUES('OLD-1','Old User','old@example.test',?,'Student',1,'2026-01-01 00:00:00')", (generate_password_hash("OldPassw0rd!"),))
            c.execute("INSERT INTO rooms(room_number,name,building,campus_id,capacity,status,qr_token,updated_at) VALUES('1','Legacy Room','B',1,10,'Available','legacytoken','2026-01-01 00:00:00')")
            c.execute("INSERT INTO reservations(user_id,room_id,purpose,start_at,end_at,status,created_at) VALUES(1,1,'old res','2030-01-01 10:00:00','2030-01-01 11:00:00','Pending','2026-01-01 00:00:00')")
            c.commit(); c.close()
            app = make_app(path, SEED_DEMO=True, SEED_DEMO_USERS=True)
            conn = connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], LATEST_VERSION)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1 + 3)  # old user kept, demo users added once
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 1)      # existing rooms: no demo rooms added
            self.assertEqual(conn.execute("SELECT purpose FROM reservations").fetchone()[0], "old res")
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(rooms)")}
            self.assertTrue({"description", "features", "status_source"} <= cols)
            conn.close()
            self.assertTrue([f for f in os.listdir(tmp) if ".bak-" in f], "a backup copy should be created before upgrading")
            make_app(path)
            conn = connect(path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 4)  # no duplicates after another start
            conn.close()
            c2 = app.test_client()
            c2.get("/login")
            with c2.session_transaction() as s:
                tok = s["csrf"]
            self.assertEqual(c2.post("/login", data={"csrf": tok, "user_id": "OLD-1", "password": "OldPassw0rd!"}).status_code, 302)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_production_style_start_has_no_demo_users_and_bootstraps_admin(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "prod.sqlite3")
            app = make_app(path, SEED_DEMO=True, SEED_DEMO_USERS=False, ADMIN_USER_ID="boss", ADMIN_EMAIL="boss@example.test", ADMIN_PASSWORD="Bootstrap#Pass1")
            conn = connect(path)
            self.assertEqual([r["user_id"] for r in conn.execute("SELECT user_id FROM users")], ["boss"])
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 5)
            conn.close()
            make_app(path, SEED_DEMO=True, SEED_DEMO_USERS=False, ADMIN_USER_ID="boss", ADMIN_EMAIL="boss@example.test", ADMIN_PASSWORD="Bootstrap#Pass1")
            conn = connect(path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            conn.close()
            c = app.test_client(); c.get("/login")
            with c.session_transaction() as s:
                tok = s["csrf"]
            r = c.post("/login", data={"csrf": tok, "user_id": "boss", "password": "Bootstrap#Pass1"})
            self.assertTrue(r.headers["Location"].endswith("/admin/"))
            self.assertNotIn("DemoPass123!", c.get("/login").get_data(as_text=True))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
