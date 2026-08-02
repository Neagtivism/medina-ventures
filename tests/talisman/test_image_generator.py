import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "projects" / "talisman" / "src"))

import image_generator as ig  # noqa: E402


class FakeResp:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@mock.patch("time.sleep", lambda *_: None)
class ImageGeneratorTest(unittest.TestCase):
    def setUp(self):
        self.gen = ig.ImageGenerator(api_key="fake-key")

    def test_missing_api_key_raises(self):
        import os

        old = os.environ.pop("FAL_KEY", None)
        try:
            with self.assertRaises(ig.FalAPIError):
                ig.ImageGenerator()
        finally:
            if old is not None:
                os.environ["FAL_KEY"] = old

    def test_generate_image_success(self):
        def fake_urlopen(req_or_url, timeout=None):
            if hasattr(req_or_url, "full_url"):
                return FakeResp(json.dumps({"images": [{"url": "https://example/img.png"}]}).encode())
            return FakeResp(b"IMGDATA")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            out = self.gen.generate_image("a prompt", Path("/tmp/gstack_test_ig_success.png"))
        self.assertEqual(out.read_bytes(), b"IMGDATA")
        out.unlink()

    def test_transient_failure_then_success(self):
        calls = {"n": 0}

        def flaky(req_or_url, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("connection reset")
            if hasattr(req_or_url, "full_url"):
                return FakeResp(json.dumps({"images": [{"url": "https://example/img.png"}]}).encode())
            return FakeResp(b"IMGDATA")

        with mock.patch("urllib.request.urlopen", side_effect=flaky):
            out = self.gen.generate_image("a prompt", Path("/tmp/gstack_test_ig_retry.png"))
        self.assertEqual(out.read_bytes(), b"IMGDATA")
        out.unlink()

    def test_permanent_failure_raises(self):
        def always_fails(req_or_url, timeout=None):
            raise urllib.error.URLError("permanently down")

        with mock.patch("urllib.request.urlopen", side_effect=always_fails):
            with self.assertRaises(ig.FalAPIError):
                self.gen.generate_image("a prompt", Path("/tmp/gstack_test_ig_fail.png"))

    def test_auth_error_fails_fast_without_retry(self):
        calls = {"n": 0}

        def auth_error(req_or_url, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError("url", 401, "unauthorized", {}, io.BytesIO(b"bad key"))

        with mock.patch("urllib.request.urlopen", side_effect=auth_error):
            with self.assertRaises(ig.FalAPIError):
                self.gen.generate_image("a prompt", Path("/tmp/gstack_test_ig_401.png"))
        self.assertEqual(calls["n"], 1)

    def test_load_prompt_anchors_from_real_metadata(self):
        anchors = ig._load_prompt_anchors()
        self.assertGreater(len(anchors), 0)
        self.assertTrue(all(isinstance(a, str) for a in anchors))


if __name__ == "__main__":
    unittest.main()
