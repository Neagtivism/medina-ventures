import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "projects" / "talisman" / "src"))

from queue_controller import QueueController  # noqa: E402


class QueueControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.controller = QueueController(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_add_and_list_pending(self):
        content_id = self.controller.add_content(
            "instagram", "caption", "prompt", "2026-08-05T09:00:00Z"
        )
        pending = self.controller.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].id, content_id)
        self.assertIsNone(pending[0].image_path)

    def test_mark_posted_removes_from_pending(self):
        content_id = self.controller.add_content(
            "instagram", "caption", "prompt", "2026-08-05T09:00:00Z"
        )
        self.controller.mark_posted(content_id)
        self.assertEqual(self.controller.list_pending(), [])

    def test_image_path_helpers(self):
        content_id = self.controller.add_content(
            "instagram", "caption", "prompt", "2026-08-05T09:00:00Z"
        )
        missing = self.controller.list_pending_without_image()
        self.assertEqual(len(missing), 1)
        self.controller.set_image_path(content_id, "data/images/1.png")
        self.assertEqual(self.controller.list_pending_without_image(), [])
        self.assertEqual(self.controller.list_pending()[0].image_path, "data/images/1.png")

    def test_log_metrics_and_monthly_summary(self):
        content_id = self.controller.add_content(
            "instagram", "caption", "prompt", "2026-08-05T09:00:00Z"
        )
        self.controller.mark_posted(content_id)
        self.controller.log_metrics(content_id, impressions=5000, likes=400, revenue_usd=50)
        summary = self.controller.monthly_revenue_summary("2026-08")
        self.assertEqual(summary["content_revenue_usd"], 50)

    def test_add_sponsorship_counted_when_paid_and_due_in_month(self):
        self.controller.add_sponsorship("TestBrand", "1 IG post", 800, due_date="2026-08-15")
        # sponsorships default to status 'prospecting', not counted until paid.
        summary = self.controller.monthly_revenue_summary("2026-08")
        self.assertEqual(summary["sponsorship_revenue_usd"], 0)


if __name__ == "__main__":
    unittest.main()
