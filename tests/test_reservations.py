import threading
from datetime import timedelta
from tests.helpers import AppTestCase, make_app, PW


class RoomBrowsingTests(AppTestCase):
    def test_dashboard_shows_database_values(self):
        c = self.client_for("DEMO-STU-001")
        html = c.get("/dashboard").get_data(as_text=True)
        total = self.q1("SELECT COUNT(*) FROM rooms")[0]
        self.assertGreaterEqual(total, 5)
        self.assertIn("Hello, Demo", html)
        r = c.get("/rooms")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Computer Lab", r.get_data(as_text=True))

    def test_search_and_filters(self):
        c = self.client_for("DEMO-STU-001")
        html = c.get("/rooms?q=Conference").get_data(as_text=True)
        self.assertIn("Conference Room", html)
        self.assertNotIn("Seminar Hall", html)
        html = c.get("/rooms?min_capacity=50").get_data(as_text=True)
        self.assertIn("Seminar Hall", html)
        self.assertNotIn("Study Room", html)
        html = c.get("/rooms?status=Under+Maintenance").get_data(as_text=True)
        self.assertIn("Science Lab", html)
        self.assertNotIn("Study Room", html)
        campus = self.q1("SELECT id FROM campuses ORDER BY id DESC LIMIT 1")["id"]
        html = c.get(f"/rooms?campus={campus}").get_data(as_text=True)
        self.assertIn("Seminar Hall", html)
        self.assertNotIn("Lecture Room", html)
        html = c.get("/rooms?q=%25").get_data(as_text=True)  # wildcard characters are escaped, not treated as match-all
        self.assertIn("No rooms match", html)

    def test_availability_window_filter(self):
        d = self.future(5)
        rid = self.room_id("104")
        c = self.client_for("DEMO-STU-001")
        self.assertEqual(self.reserve(c, rid, d, "09:00", "10:00").status_code, 302)
        html = c.get(f"/rooms?date={d}&start=09:30&end=10:30").get_data(as_text=True)
        self.assertNotIn("Study Room", html)
        self.assertIn("Showing rooms free", html)
        html = c.get(f"/rooms?date={d}&start=11:00&end=12:00").get_data(as_text=True)
        self.assertIn("Study Room", html)
        self.assertNotIn("Science Lab", html)  # maintenance rooms never count as free

    def test_room_detail_and_missing_room(self):
        c = self.client_for("DEMO-STU-001")
        self.assertEqual(c.get(f"/rooms/{self.room_id('101')}").status_code, 200)
        r = c.get("/rooms/99999")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("Traceback", r.get_data(as_text=True))


class ReservationTests(AppTestCase):
    def test_valid_reservation_saved_as_pending(self):
        c = self.client_for("DEMO-STU-001")
        r = self.reserve(c, self.room_id("101"), self.future(2), purpose="Thesis defense rehearsal")
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM reservations WHERE purpose='Thesis defense rehearsal'")
        self.assertEqual(row["status"], "Pending")
        self.assertEqual(row["user_id"], self.user_pk("DEMO-STU-001"))
        page = c.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("Pending", page)
        self.assertIn("Thesis defense rehearsal", c.get("/reservations").get_data(as_text=True))
        self.assertTrue(self.q1("SELECT 1 FROM audit_logs WHERE action='reservation_requested' AND entity_id=?", row["id"]))

    def test_invalid_reservations_rejected(self):
        c = self.client_for("DEMO-STU-001")
        room = self.room_id("101")
        yesterday = (self.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        cases = {
            "past date": (yesterday, "10:00", "11:00", "Past test"),
            "end before start": (self.future(2), "11:00", "10:00", "Order test"),
            "equal times": (self.future(2), "10:00", "10:00", "Equal test"),
            "too long for student (5h)": (self.future(2), "08:00", "13:00", "Long test"),
            "too short": (self.future(2), "10:00", "10:10", "Short test"),
            "before opening": (self.future(2), "04:00", "05:00", "Early test"),
            "after closing": (self.future(2), "21:00", "23:00", "Late test"),
            "too far ahead": (self.future(400), "10:00", "11:00", "Far test"),
            "garbage date": ("not-a-date", "10:00", "11:00", "Garbage test"),
            "empty purpose": (self.future(2), "10:00", "11:00", " "),
        }
        for label, (d, s, e, p) in cases.items():
            r = self.reserve(c, room, d, s, e, p)
            self.assertEqual(r.status_code, 400, label)
        self.assertEqual(self.q1("SELECT COUNT(*) FROM reservations")[0], 0)
        self.assertEqual(self.reserve(c, 99999, self.future(2)).status_code, 400)
        self.assertEqual(self.reserve(c, "abc", self.future(2)).status_code, 400)

    def test_started_slot_today_is_past(self):
        c = self.client_for("DEMO-STU-001")
        n = self.now()
        start = (n - timedelta(hours=1)).strftime("%H:%M")
        if n.hour >= 7:
            r = self.reserve(c, self.room_id("101"), n.strftime("%Y-%m-%d"), start, (n + timedelta(hours=1)).strftime("%H:%M"))
            self.assertEqual(r.status_code, 400)

    def test_professor_gets_longer_limit(self):
        p = self.client_for("DEMO-PROF-001")
        r = self.reserve(p, self.room_id("201"), self.future(6), "08:00", "14:00", "Seminar")
        self.assertEqual(r.status_code, 302)

    def test_overlap_rejected_adjacent_allowed(self):
        a = self.client_for("DEMO-STU-001")
        b = self.client_for("DEMO-PROF-001")
        room, d = self.room_id("103"), self.future(4)
        self.assertEqual(self.reserve(a, room, d, "10:00", "12:00").status_code, 302)
        for s, e in [("10:00", "12:00"), ("11:00", "13:00"), ("09:00", "10:30"), ("10:30", "11:00")]:
            r = self.reserve(b, room, d, s, e)
            self.assertEqual(r.status_code, 409, (s, e))
            self.assertIn("not available at that time", r.get_data(as_text=True))
        self.assertEqual(self.reserve(b, room, d, "12:00", "13:00").status_code, 302)  # back-to-back is fine
        self.assertEqual(self.reserve(b, room, d, "08:00", "10:00").status_code, 302)
        self.assertEqual(self.reserve(b, self.room_id("104"), d, "10:00", "12:00").status_code, 302)  # other room

    def test_maintenance_room_cannot_be_reserved(self):
        c = self.client_for("DEMO-STU-001")
        r = self.reserve(c, self.room_id("105"), self.future(3))
        self.assertEqual(r.status_code, 400)
        self.assertIn("maintenance", r.get_data(as_text=True))

    def test_reserve_requires_csrf(self):
        c = self.client_for("DEMO-STU-001")
        r = c.post("/reserve", data={"room_id": self.room_id("101"), "date": self.future(2), "start": "10:00", "end": "11:00", "purpose": "No token"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.q1("SELECT COUNT(*) FROM reservations WHERE purpose='No token'")[0], 0)

    def test_sql_injection_and_html_are_inert(self):
        c = self.client_for("DEMO-STU-001")
        payload = "x'); DROP TABLE reservations;-- <script>alert(1)</script>"
        r = self.reserve(c, self.room_id("202"), self.future(7), purpose=payload)
        self.assertEqual(r.status_code, 302)
        html = c.get("/reservations?q=" + "%27+OR+1%3D1+--").get_data(as_text=True)
        self.assertEqual(self.q1("SELECT COUNT(*) FROM reservations WHERE purpose LIKE '%DROP TABLE%'")[0], 1)
        html = c.get(r.headers["Location"]).get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class ApprovalAndCancelTests(AppTestCase):
    def make_pending(self, uid="DEMO-STU-001", room="101", days=3, s="10:00", e="11:00", purpose="Approval flow"):
        c = self.client_for(uid)
        d = self.future(days)
        r = self.reserve(c, self.room_id(room), d, s, e, purpose)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:200])
        return c, self.q1("SELECT * FROM reservations WHERE purpose=? ORDER BY id DESC", purpose)["id"]

    def test_admin_approves_and_rejects(self):
        _, rid = self.make_pending(purpose="to approve")
        adm = self.client_for("DEMO-ADM-001")
        r = self.post(f"/admin/reservations/{rid}/review", {"decision": "Approved", "note": "Enjoy"}, client=adm)
        self.assertEqual(r.status_code, 302)
        row = self.q1("SELECT * FROM reservations WHERE id=?", rid)
        self.assertEqual((row["status"], row["review_note"]), ("Approved", "Enjoy"))
        self.assertEqual(row["reviewed_by"], self.user_pk("DEMO-ADM-001"))
        self.assertTrue(self.q1("SELECT 1 FROM audit_logs WHERE action='reservation_approved' AND entity_id=?", rid))
        # already reviewed -> cannot be reviewed again
        r = self.post(f"/admin/reservations/{rid}/review", {"decision": "Rejected"}, client=adm, follow_redirects=True)
        self.assertIn("Only pending", r.get_data(as_text=True))
        _, rid2 = self.make_pending(room="104", purpose="to reject")
        self.post(f"/admin/reservations/{rid2}/review", {"decision": "Rejected", "note": "Room needed"}, client=adm)
        self.assertEqual(self.q1("SELECT status FROM reservations WHERE id=?", rid2)["status"], "Rejected")
        c = self.client_for("DEMO-STU-001")
        self.assertIn("Room needed", c.get(f"/reservations/{rid2}").get_data(as_text=True))
        self.assertEqual(self.post(f"/admin/reservations/{rid2}/review", {"decision": "Maybe"}, client=adm).status_code, 302)

    def test_approval_rechecks_availability_and_db_trigger_blocks_overlap(self):
        # Two overlapping pending rows (e.g. from legacy data) - only one may ever be approved.
        d = self.now() + timedelta(days=9)
        s = d.replace(hour=10, minute=0, second=0)
        room = self.room_id("201")
        a = self.insert_res("DEMO-STU-001", room, s, s + timedelta(hours=2), "Pending", "legacy A")
        b = self.insert_res("DEMO-PROF-001", room, s + timedelta(hours=1), s + timedelta(hours=3), "Pending", "legacy B")
        adm = self.client_for("DEMO-ADM-001")
        self.post(f"/admin/reservations/{a}/review", {"decision": "Approved"}, client=adm)
        r = self.post(f"/admin/reservations/{b}/review", {"decision": "Approved"}, client=adm, follow_redirects=True)
        self.assertIn("already approved for an overlapping time", r.get_data(as_text=True))
        self.assertEqual(self.q1("SELECT status FROM reservations WHERE id=?", b)["status"], "Pending")
        c = self.db()
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("UPDATE reservations SET status='Approved' WHERE id=?", (b,))
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("""INSERT INTO reservations(user_id,room_id,purpose,start_at,end_at,status,created_at)
                         VALUES(1,?,?,?,?,'Approved','x')""", (room, "t", s.strftime("%Y-%m-%d %H:%M:%S"), (s + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")))
        c.close()

    def test_admin_cannot_review_own_request_and_past_cannot_be_approved(self):
        adm = self.client_for("DEMO-ADM-001")
        self.reserve(adm, self.room_id("202"), self.future(8), purpose="admin own")
        rid = self.q1("SELECT id FROM reservations WHERE purpose='admin own'")["id"]
        r = self.post(f"/admin/reservations/{rid}/review", {"decision": "Approved"}, client=adm, follow_redirects=True)
        self.assertIn("cannot review your own", r.get_data(as_text=True))
        past = self.now() - timedelta(days=2)
        old = self.insert_res("DEMO-STU-001", self.room_id("104"), past, past + timedelta(hours=1), "Pending", "stale")
        r = self.post(f"/admin/reservations/{old}/review", {"decision": "Approved"}, client=adm, follow_redirects=True)
        self.assertIn("already passed", r.get_data(as_text=True))

    def test_only_admins_can_review(self):
        stu, rid = self.make_pending(room="201", days=16, purpose="no student review")
        r = self.post(f"/admin/reservations/{rid}/review", {"decision": "Approved"}, client=stu)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.q1("SELECT status FROM reservations WHERE id=?", rid)["status"], "Pending")

    def test_cancel_pending_and_approved_and_room_status_follows(self):
        stu, rid = self.make_pending(room="201", days=15, purpose="cancel me")
        self.assertEqual(self.post(f"/reservations/{rid}/cancel", client=stu).status_code, 302)
        row = self.q1("SELECT * FROM reservations WHERE id=?", rid)
        self.assertEqual(row["status"], "Cancelled")
        self.assertTrue(row["cancelled_at"])
        r = self.post(f"/reservations/{rid}/cancel", client=stu, follow_redirects=True)
        self.assertIn("cannot be cancelled", r.get_data(as_text=True))
        # approved + currently active -> room becomes Reserved; cancelling as admin frees it
        n = self.now()
        room = self.room_id("202")
        active = self.insert_res("DEMO-STU-001", room, n - timedelta(minutes=10), n + timedelta(hours=1), "Approved", "active now")
        adm = self.client_for("DEMO-ADM-001")
        adm.get("/admin/rooms")
        self.assertEqual(self.q1("SELECT status FROM rooms WHERE id=?", room)["status"], "Reserved")
        self.assertTrue(self.q1("SELECT 1 FROM room_status_history WHERE room_id=? AND new_status='Reserved'", room))
        self.post(f"/reservations/{active}/cancel", client=adm)
        self.assertEqual(self.q1("SELECT status FROM rooms WHERE id=?", room)["status"], "Available")
        self.assertTrue(self.q1("SELECT 1 FROM room_status_history WHERE room_id=? AND new_status='Available'", room))

    def test_user_cannot_cancel_after_start_or_others_reservation(self):
        n = self.now()
        started = self.insert_res("DEMO-STU-001", self.room_id("103"), n - timedelta(minutes=30), n + timedelta(minutes=30), "Approved", "started")
        stu = self.client_for("DEMO-STU-001")
        r = self.post(f"/reservations/{started}/cancel", client=stu, follow_redirects=True)
        self.assertIn("before they start", r.get_data(as_text=True))
        self.assertEqual(self.q1("SELECT status FROM reservations WHERE id=?", started)["status"], "Approved")
        _, rid = self.make_pending("DEMO-STU-001", room="104", days=12, purpose="owned by student")
        other = self.client_for("DEMO-PROF-001")
        self.assertEqual(self.post(f"/reservations/{rid}/cancel", client=other).status_code, 404)
        self.assertEqual(self.q1("SELECT status FROM reservations WHERE id=?", rid)["status"], "Pending")

    def test_idor_reservation_detail(self):
        _, rid = self.make_pending(purpose="private one", room="203", days=13)
        other = self.client_for("DEMO-PROF-001")
        self.assertEqual(other.get(f"/reservations/{rid}").status_code, 404)
        self.assertNotIn("private one", other.get("/reservations").get_data(as_text=True))
        adm = self.client_for("DEMO-ADM-001")
        self.assertEqual(adm.get(f"/reservations/{rid}").status_code, 200)
        self.assertEqual(self.client_for("DEMO-STU-001").get("/reservations/987654").status_code, 404)

    def test_reservation_history_filters(self):
        stu, rid = self.make_pending(purpose="filter me", room="203", days=14)
        self.assertIn("filter me", stu.get("/reservations?status=Pending").get_data(as_text=True))
        self.assertNotIn("filter me", stu.get("/reservations?status=Approved").get_data(as_text=True))
        self.assertIn("filter me", stu.get("/reservations?q=filter").get_data(as_text=True))


class ConcurrencyTests(AppTestCase):
    def test_simultaneous_requests_cannot_double_book(self):
        room, d = self.room_id("201"), self.future(20)
        clients = [self.client_for("DEMO-STU-001") for _ in range(2)] + [self.client_for("DEMO-PROF-001") for _ in range(4)]
        results, barrier = [], threading.Barrier(len(clients))

        def go(c, i):
            tok = self.csrf(c)
            barrier.wait()
            r = c.post("/reserve", data={"csrf": tok, "room_id": room, "date": d, "start": "10:00", "end": "11:00", "purpose": f"race {i}"})
            results.append(r.status_code)

        ts = [threading.Thread(target=go, args=(c, i)) for i, c in enumerate(clients)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results).count(302), 1, results)
        self.assertEqual(self.q1("SELECT COUNT(*) FROM reservations WHERE purpose LIKE 'race %'")[0], 1)

    def test_simultaneous_approvals_only_one_wins(self):
        s = (self.now() + timedelta(days=21)).replace(hour=9, minute=0, second=0)
        room = self.room_id("202")
        ids = [self.insert_res("DEMO-STU-001", room, s, s + timedelta(hours=2), "Pending", f"appr {i}") for i in range(4)]
        admins = [self.client_for("DEMO-ADM-001") for _ in ids]
        barrier, out = threading.Barrier(len(ids)), []

        def go(c, rid):
            tok = self.csrf(c)
            barrier.wait()
            out.append(c.post(f"/admin/reservations/{rid}/review", data={"csrf": tok, "decision": "Approved"}).status_code)

        ts = [threading.Thread(target=go, args=(c, r)) for c, r in zip(admins, ids)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.q1("SELECT COUNT(*) FROM reservations WHERE purpose LIKE 'appr %' AND status='Approved'")[0], 1)
