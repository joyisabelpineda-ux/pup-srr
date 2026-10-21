import os
from tests.helpers import AppTestCase


class MiscTests(AppTestCase):
    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual((r.status_code, r.get_json()), (200, {"status": "ok"}))

    def test_backup_command_creates_readable_copy(self):
        runner = self.app.test_cli_runner()
        res = runner.invoke(args=["backup-db"])
        self.assertIn("Backup written", res.output)
        backups = [f for f in os.listdir(self.tmp) if ".backup-" in f]
        self.assertEqual(len(backups), 1)
        import sqlite3
        c = sqlite3.connect(os.path.join(self.tmp, backups[0]))
        self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 5)
        c.close()

    def test_anonymous_error_page_renders_without_session(self):
        r = self.client.get("/definitely-missing")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Go to login", r.get_data(as_text=True))
