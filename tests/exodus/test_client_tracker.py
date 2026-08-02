import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "projects" / "exodus" / "src"))

from client_tracker import ClientError, ClientTracker  # noqa: E402


class ClientTrackerTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.tracker = ClientTracker(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_add_client_and_mrr_excludes_non_active(self):
        self.tracker.add_client("deli1", "Corner Deli", monthly_rate=25)
        self.tracker.add_client("barber1", "Fade Barbershop", monthly_rate=30)
        self.tracker.add_client("paused1", "Paused Biz", card_status="paused", monthly_rate=25)
        self.assertEqual(self.tracker.get_mrr(), 55)

    def test_duplicate_client_id_raises(self):
        self.tracker.add_client("deli1", "Corner Deli")
        with self.assertRaises(ClientError):
            self.tracker.add_client("deli1", "Dup Biz")

    def test_card_status_check_constraint_enforced_at_db_layer(self):
        with self.assertRaises(ClientError):
            self.tracker.add_client("x1", "X Biz", card_status="bogus")

    def test_log_snapshot_for_unknown_client_raises_fk_violation(self):
        with self.assertRaises(ClientError):
            self.tracker.log_review_snapshot("ghost", review_count=1, average_rating=5.0)

    def test_report_growth_deltas(self):
        self.tracker.add_client("deli1", "Corner Deli")
        self.tracker.log_review_snapshot("deli1", 12, 4.2, snapshot_date="2026-07-01")
        self.tracker.log_review_snapshot("deli1", 31, 4.6, snapshot_date="2026-08-01")
        report = self.tracker.get_client_report("deli1")
        self.assertEqual(report["review_count_start"], 12)
        self.assertEqual(report["review_count_now"], 31)
        self.assertEqual(report["review_count_gain"], 19)
        self.assertEqual(report["average_rating_now"], 4.6)

    def test_report_no_snapshots(self):
        self.tracker.add_client("barber1", "Fade Barbershop")
        report = self.tracker.get_client_report("barber1")
        self.assertEqual(report["snapshot_count"], 0)

    def test_report_unknown_client_raises(self):
        with self.assertRaises(ClientError):
            self.tracker.get_client_report("ghost")

    def test_update_card_status(self):
        self.tracker.add_client("deli1", "Corner Deli", monthly_rate=25)
        self.tracker.update_card_status("deli1", "paused")
        self.assertEqual(self.tracker.get_mrr(), 0)

    def test_update_card_status_unknown_client_raises(self):
        with self.assertRaises(ClientError):
            self.tracker.update_card_status("ghost", "paused")

    def test_clients_with_stripe_subscription_filters_correctly(self):
        self.tracker.add_client("deli1", "Corner Deli", stripe_subscription_id="sub_a")
        self.tracker.add_client("cash1", "Cash Biz")  # no stripe id
        result = self.tracker.clients_with_stripe_subscription()
        self.assertEqual([c.client_id for c in result], ["deli1"])

    def test_indexes_are_actually_used(self):
        self.tracker.add_client("deli1", "Corner Deli")
        conn = sqlite3.connect(self.db_path)
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM clients WHERE card_status='active'"
        ).fetchall()
        plan_text = " ".join(str(row) for row in plan)
        self.assertIn("idx_clients_card_status", plan_text)

    def test_dashboard_html_reflects_mrr_and_escapes_content(self):
        self.tracker.add_client("deli1", 'Corner "Deli" & Co', monthly_rate=25)
        html = self.tracker.generate_dashboard_html()
        self.assertIn("$25", html)
        self.assertNotIn('"Deli" &', html)  # raw quote/amp must be escaped
        self.assertIn("&amp;", html)

    def test_dashboard_html_empty_state(self):
        html = self.tracker.generate_dashboard_html()
        self.assertIn("No clients yet.", html)


if __name__ == "__main__":
    unittest.main()
