import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "projects" / "exodus" / "leadgen"))

import places_search as ps  # noqa: E402


def make_lead(**kw):
    base = dict(
        place_id="p1", business_name="Test", address="123 St", phone=None,
        website=None, rating=4.0, review_count=10, business_status="OPERATIONAL",
        opened_signal=None, website_quality="none",
    )
    base.update(kw)
    return ps.Lead(**base)


def make_place(i, **overrides):
    place = {
        "id": f"p{i}", "displayName": {"text": f"Biz {i}"}, "formattedAddress": "addr",
        "nationalPhoneNumber": None, "websiteUri": None, "rating": 4.0,
        "userRatingCount": 5, "businessStatus": "OPERATIONAL",
    }
    place.update(overrides)
    return place


class FakeResp:
    def __init__(self, data):
        self._data = data

    def read(self, n=None):
        return self._data if n is None else self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@mock.patch("time.sleep", lambda *_: None)
class PlacesSearchTest(unittest.TestCase):
    def test_passes_filter_boundaries_and_missing_data(self):
        self.assertTrue(ps.passes_filter(make_lead()))
        self.assertFalse(ps.passes_filter(make_lead(website_quality="present")))
        self.assertFalse(ps.passes_filter(make_lead(review_count=51)))
        self.assertTrue(ps.passes_filter(make_lead(review_count=50)))
        self.assertFalse(ps.passes_filter(make_lead(rating=3.4)))
        self.assertTrue(ps.passes_filter(make_lead(rating=3.5)))
        self.assertFalse(ps.passes_filter(make_lead(rating=None)))
        self.assertFalse(ps.passes_filter(make_lead(review_count=None)))
        self.assertTrue(ps.passes_filter(make_lead(website_quality="sparse")))

    def test_check_website_sparse_heuristic(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(b"x" * 100)):
            self.assertTrue(ps.check_website_sparse("http://example.com"))
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(b"x" * 5000)):
            self.assertFalse(ps.check_website_sparse("http://example.com"))
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            self.assertTrue(ps.check_website_sparse("http://example.com"))

    def test_evaluate_lead_maps_fields_and_opened_signal_is_honest(self):
        place = make_place(1, displayName={"text": "Sunny Bakery"}, rating=4.2, userRatingCount=8)
        lead = ps.evaluate_lead(place)
        self.assertEqual(lead.business_name, "Sunny Bakery")
        self.assertEqual(lead.website_quality, "none")
        self.assertIsNone(lead.opened_signal)

    def test_pagination_joins_pages(self):
        calls = {"n": 0}

        def paged(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResp(json.dumps(
                    {"places": [make_place(i) for i in range(20)], "nextPageToken": "tok2"}
                ).encode())
            return FakeResp(json.dumps({"places": [make_place(i) for i in range(20, 25)]}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=paged):
            results = ps.search_places("bakery near X", "fake-key", max_results=60)
        self.assertEqual(len(results), 25)

    def test_429_retries_then_succeeds(self):
        attempts = {"n": 0}

        def rate_limited(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise urllib.error.HTTPError("url", 429, "rate limited", {}, io.BytesIO(b"slow"))
            return FakeResp(json.dumps({"places": [make_place(1)]}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=rate_limited):
            results = ps.search_places("bakery near X", "fake-key")
        self.assertEqual(len(results), 1)
        self.assertEqual(attempts["n"], 3)

    def test_403_fails_fast(self):
        attempts = {"n": 0}

        def forbidden(req, timeout=None):
            attempts["n"] += 1
            raise urllib.error.HTTPError("url", 403, "forbidden", {}, io.BytesIO(b"bad key"))

        with mock.patch("urllib.request.urlopen", side_effect=forbidden):
            with self.assertRaises(ps.PlacesAPIError):
                ps.search_places("bakery near X", "fake-key")
        self.assertEqual(attempts["n"], 1)


if __name__ == "__main__":
    unittest.main()
