import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "projects" / "exodus" / "src"))

from client_tracker import ClientTracker  # noqa: E402
import stripe_sync as ss  # noqa: E402


class FakeResp:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body=b"error"):
    return urllib.error.HTTPError("url", code, "err", {}, io.BytesIO(body))


@mock.patch("time.sleep", lambda *_: None)
class StripeSyncTest(unittest.TestCase):
    def setUp(self):
        self.client = ss.StripeClient(api_key="rk_test_fake")

    def test_missing_api_key_raises(self):
        import os

        old = os.environ.pop("STRIPE_API_KEY", None)
        try:
            with self.assertRaises(ss.StripeAPIError):
                ss.StripeClient()
        finally:
            if old is not None:
                os.environ["STRIPE_API_KEY"] = old

    def test_status_mapping_table(self):
        cases = {
            "active": "active",
            "trialing": "active",
            "past_due": "paused",
            "unpaid": "paused",
            "paused": "paused",
            "incomplete": "paused",
            "incomplete_expired": "canceled",
            "canceled": "canceled",
            "some_future_status": "paused",
        }
        for stripe_status, expected in cases.items():
            self.assertEqual(ss.map_status(stripe_status), expected, stripe_status)

    def test_successful_fetch(self):
        def fake_urlopen(req, timeout=None):
            self.assertTrue(req.headers["Authorization"].startswith("Basic "))
            return FakeResp(json.dumps({"status": "active"}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(self.client.get_subscription_status("sub_1"), "active")

    def test_401_fails_fast_without_retrying(self):
        calls = {"n": 0}

        def fail(req, timeout=None):
            calls["n"] += 1
            raise http_error(401, b"bad key")

        with mock.patch("urllib.request.urlopen", side_effect=fail):
            with self.assertRaises(ss.StripeAPIError):
                self.client.get_subscription_status("sub_1")
        self.assertEqual(calls["n"], 1)

    def test_404_fails_fast_without_retrying(self):
        calls = {"n": 0}

        def fail(req, timeout=None):
            calls["n"] += 1
            raise http_error(404, b"not found")

        with mock.patch("urllib.request.urlopen", side_effect=fail):
            with self.assertRaises(ss.StripeAPIError):
                self.client.get_subscription_status("sub_1")
        self.assertEqual(calls["n"], 1)

    def test_429_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise http_error(429, b"slow down")
            return FakeResp(json.dumps({"status": "canceled"}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=flaky):
            self.assertEqual(self.client.get_subscription_status("sub_1"), "canceled")
        self.assertEqual(calls["n"], 3)

    def test_500_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise http_error(500, b"oops")
            return FakeResp(json.dumps({"status": "trialing"}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=flaky):
            self.assertEqual(self.client.get_subscription_status("sub_1"), "trialing")

    def test_plain_urlerror_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise urllib.error.URLError("dns failure")
            return FakeResp(json.dumps({"status": "active"}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=flaky):
            self.assertEqual(self.client.get_subscription_status("sub_1"), "active")

    def test_sync_all_end_to_end(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        tracker = ClientTracker(Path(tmpdir.name) / "test.db")
        tracker.add_client("deli1", "Corner Deli", card_status="active", stripe_subscription_id="sub_deli")
        tracker.add_client("barber1", "Fade Barbershop", card_status="active", stripe_subscription_id="sub_barber")
        tracker.add_client("cash1", "Cash Biz", card_status="active")  # no stripe id
        tracker.add_client("ghost1", "Deleted Sub", card_status="active", stripe_subscription_id="sub_ghost")

        def fake_urlopen(req, timeout=None):
            if "sub_deli" in req.full_url:
                return FakeResp(json.dumps({"status": "past_due"}).encode())
            if "sub_barber" in req.full_url:
                return FakeResp(json.dumps({"status": "active"}).encode())
            if "sub_ghost" in req.full_url:
                raise http_error(404, b"no such subscription")
            raise AssertionError(f"unexpected url {req.full_url}")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = ss.sync_all(tracker, self.client)

        self.assertEqual(len(result["updated"]), 1)
        self.assertEqual(result["updated"][0]["client_id"], "deli1")
        self.assertEqual(result["updated"][0]["to"], "paused")
        self.assertIn("barber1", result["unchanged"])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["client_id"], "ghost1")

        # cash1 was never touched (no subscription id) and stays active.
        report = tracker.clients_with_stripe_subscription()
        self.assertNotIn("cash1", [c.client_id for c in report])


if __name__ == "__main__":
    unittest.main()
