"""Student code: platform issuance, private storage, fallback, no repeated requests."""
import io
import json
import os
from pathlib import Path
import stat
import sys
import unittest
from unittest import mock
import urllib.error

import test_support

sys.path.insert(0, str(test_support.ROOT / "scripts"))
import student_code as sc  # noqa: E402


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, *results):
        self.results, self.requests = list(results), []

    def open(self, request, timeout=None):
        self.requests.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(json.dumps(result).encode())


def http_error(code, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after else {}
    return urllib.error.HTTPError("https://hi.xwoork.cn/x", code, "err", headers, io.BytesIO(b"{}"))


class StudentCodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = test_support.TemporaryDirectory()
        self.home = Path(self.tmp.__enter__()) / "liuxue"
        self.env = mock.patch.dict(os.environ, {"LIUXUE_HOME": str(self.home)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.__exit__(None, None, None)

    def test_issue_once_store_privately_and_reuse(self):
        opener = FakeOpener({"id": "k7m2qx9p", "pin": "482913"})
        record = sc.ensure(opener=opener)
        self.assertEqual((record["id"], record["pin"]), ("K7M2QX9P", "482913"))
        self.assertEqual(opener.requests[0].full_url, "https://hi.xwoork.cn/api/assist/v1/student-codes")
        self.assertEqual(sc.ensure(opener=FakeOpener()), sc.load())  # no second request
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE((self.home / "id").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o700)
        self.assertEqual(sc.page_url(record), "https://hi.xwoork.cn/s/K7M2QX9P#482913")
        self.assertEqual(sc.page_url(record, "upload"), "https://hi.xwoork.cn/s/K7M2QX9P/upload#482913")

    def test_failure_falls_back_and_backs_off(self):
        opener = FakeOpener(http_error(404))
        self.assertIsNone(sc.ensure(opener=opener))
        self.assertEqual(sc.page_url(None), "https://hi.xwoork.cn/student")
        self.assertEqual(sc.page_url(None, "upload"), "https://hi.xwoork.cn/student/upload")
        later = FakeOpener({"id": "K7M2QX9P", "pin": "482913"})
        self.assertIsNone(sc.ensure(opener=later))
        self.assertEqual(later.requests, [])  # waits before asking again

    def test_rate_limit_respects_retry_after(self):
        self.assertIsNone(sc.ensure(opener=FakeOpener(http_error(429, 7200))))
        self.assertGreater(sc._blocked_until(), __import__("time").time() + 7000)

    def test_bad_payload_is_not_stored(self):
        for payload in ({"id": "K7M2QX9O", "pin": "482913"}, {"id": "K7M2QX9P", "pin": "4829"}, ["x"]):
            for name in ("id", "id.retry"):
                (self.home / name).unlink(missing_ok=True)
            self.assertIsNone(sc.ensure(opener=FakeOpener(payload)))
            self.assertIsNone(sc.load())

    def test_unsafe_file_is_ignored(self):
        self.home.mkdir(parents=True)
        path = self.home / "id"
        path.write_text(json.dumps({"id": "K7M2QX9P", "pin": "482913"}))
        if os.name == "posix":
            os.chmod(path, 0o644)
            self.assertIsNone(sc.load())
            os.chmod(path, 0o600)
        self.assertEqual(sc.load()["id"], "K7M2QX9P")

    def test_origin_override_only_for_local_tests(self):
        with mock.patch.dict(os.environ, {"LIUXUE_PLATFORM_ORIGIN": "https://evil.example"}):
            self.assertEqual(sc.origin(), "https://hi.xwoork.cn")
        with mock.patch.dict(os.environ, {"LIUXUE_PLATFORM_ORIGIN": "http://127.0.0.1:8080"}):
            self.assertEqual(sc.origin(), "http://127.0.0.1:8080")


if __name__ == "__main__":
    unittest.main()
