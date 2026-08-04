import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "projects" / "exodus" / "leadgen"))

import council as cn  # noqa: E402


class FakeResp:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def anthropic_resp(text):
    return FakeResp(json.dumps({"content": [{"type": "text", "text": text}]}).encode())


LEADS = [
    {"place_id": "clear_win", "business_name": "Sunny Bakery", "address": "1 Main St",
     "phone": "555-1", "website_quality": "none", "rating": 4.5, "review_count": 12,
     "business_status": "OPERATIONAL", "opened_signal": None},
    {"place_id": "mixed_case", "business_name": "Contested Cafe", "address": "2 Oak St",
     "phone": "555-2", "website_quality": "sparse", "rating": 4.0, "review_count": 20,
     "business_status": "OPERATIONAL", "opened_signal": None},
    {"place_id": "wildcard_only", "business_name": "Odd Boba Spot", "address": "3 Elm St",
     "phone": None, "website_quality": "none", "rating": 3.6, "review_count": 5,
     "business_status": "OPERATIONAL", "opened_signal": None},
    {"place_id": "no_mentions", "business_name": "Ghost Business", "address": "4 Pine St",
     "phone": None, "website_quality": "none", "rating": 4.0, "review_count": 3,
     "business_status": "OPERATIONAL", "opened_signal": None},
]


def fake_council_responses():
    presence = json.dumps([
        {"place_id": "clear_win", "score": 9, "verdict": "pursue", "note": "No site, 12 reviews"},
        {"place_id": "mixed_case", "score": 8, "verdict": "pursue", "note": "Sparse site, real gap"},
        {"place_id": "wildcard_only", "score": 4, "verdict": "unsure", "note": "Small gap"},
    ])
    conversion = json.dumps([
        {"place_id": "clear_win", "score": 8, "verdict": "pursue", "note": "New and growing"},
        {"place_id": "mixed_case", "score": 7, "verdict": "pursue", "note": "Open to new tools"},
        {"place_id": "wildcard_only", "score": 3, "verdict": "unsure", "note": "Unclear"},
    ])
    redflag = json.dumps([
        {"place_id": "clear_win", "severity": 0, "note": "no concerns"},
        {"place_id": "mixed_case", "severity": 8, "note": "looks like a franchise location"},
        {"place_id": "wildcard_only", "severity": 1, "note": "minor"},
    ])
    wildcard = json.dumps([
        {"place_id": "wildcard_only", "note": "Unusual review pattern, worth a human look"},
    ])
    return [presence, conversion, redflag, wildcard]


@mock.patch("time.sleep", lambda *_: None)
class CouncilTest(unittest.TestCase):
    def test_extract_json_handles_raw_and_fenced(self):
        raw = '[{"place_id": "p1", "score": 5}]'
        fenced = '```json\n[{"place_id": "p1", "score": 5}]\n```'
        self.assertEqual(cn._extract_json(raw), cn._extract_json(fenced))

    def test_call_claude_success(self):
        def ok(req, timeout=None):
            return anthropic_resp('[{"place_id":"p1","score":5}]')

        with mock.patch("urllib.request.urlopen", side_effect=ok):
            text = cn.call_claude("sys", "user", "fake-key")
        self.assertIn("p1", text)

    def test_call_claude_retries_429(self):
        attempts = {"n": 0}

        def rate_limited(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise urllib.error.HTTPError("url", 429, "rate limited", {}, io.BytesIO(b"slow"))
            return anthropic_resp("[]")

        with mock.patch("urllib.request.urlopen", side_effect=rate_limited):
            cn.call_claude("sys", "user", "fake-key")
        self.assertEqual(attempts["n"], 2)

    def test_call_claude_401_fails_fast(self):
        attempts = {"n": 0}

        def auth_fail(req, timeout=None):
            attempts["n"] += 1
            raise urllib.error.HTTPError("url", 401, "unauthorized", {}, io.BytesIO(b"bad key"))

        with mock.patch("urllib.request.urlopen", side_effect=auth_fail):
            with self.assertRaises(cn.ClaudeAPIError):
                cn.call_claude("sys", "user", "fake-key")
        self.assertEqual(attempts["n"], 1)

    def test_run_council_and_report_end_to_end(self):
        call_sequence = fake_council_responses()
        call_log = []

        def fake_call_claude(system_prompt, user_content, api_key, model=cn.DEFAULT_MODEL):
            call_log.append(system_prompt)
            return call_sequence[len(call_log) - 1]

        with mock.patch.object(cn, "call_claude", side_effect=fake_call_claude):
            combined = cn.run_council(LEADS, "fake-key")

        by_id = {c.place_id: c for c in combined}

        self.assertEqual(by_id["clear_win"].final_score, 17)
        self.assertFalse(by_id["clear_win"].mixed_signal)

        self.assertTrue(by_id["mixed_case"].mixed_signal)

        self.assertIsNotNone(by_id["wildcard_only"].wildcard_note)
        self.assertFalse(by_id["wildcard_only"].mixed_signal)

        # Business omitted from all 3 structured reviewers must default gracefully, not crash.
        self.assertEqual(by_id["no_mentions"].presence_score, 0)
        self.assertEqual(by_id["no_mentions"].final_score, 0)

        report = cn.generate_report(combined)
        self.assertIn("## Ranked Recommendations", report)
        self.assertIn("## Mixed Signal", report)
        ranked_section = report.split("## Ranked Recommendations")[1].split("## Mixed Signal")[0]
        mixed_section = report.split("## Mixed Signal")[1]
        self.assertIn("Sunny Bakery", ranked_section)
        self.assertNotIn("Contested Cafe", ranked_section)
        self.assertIn("Contested Cafe", mixed_section)


if __name__ == "__main__":
    unittest.main()
