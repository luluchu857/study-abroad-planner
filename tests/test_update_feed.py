"""Official static-feed and archive checks; no real network or active installs."""

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request
import urllib.response
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("skill_update_feed_test", ROOT / "scripts/update_feed.py")
feed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(feed)
COMMIT = "a" * 40


def metadata(version="0.10.0-pilot.2"):
    return {"version": version, "commit": COMMIT, "manifest_sha256": "b" * 64}


def index():
    return {"schema_version": 1, "repository": feed.REPOSITORY,
            "channels": {"stable": metadata("0.9.0"), "pilot": metadata()}}


class Response(io.BytesIO):
    def __init__(self, body, url=feed.INDEX_URL, headers=None):
        super().__init__(body)
        self.headers = headers or {}
        self.url = url
        self.status = 200

    def geturl(self):
        return self.url


def package(contents=None, extras=None, manifest_change=None, zip_change=None):
    files = {"VERSION": b"0.10.0-pilot.2\n", "SKILL.md": b"---\nname: study-abroad-planner\n---\nPlan.\n",
             "scripts/example.py": b"raise RuntimeError('must not execute downloaded code')\n",
             "references/knowledge-cards.json": b'{"cards":[]}\n'}
    files.update(contents or {})
    manifest = {"version": "0.10.0-pilot.2", "files": {
        name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()}}
    if manifest_change:
        manifest_change(manifest)
    raw = json.dumps(manifest).encode()
    stream = io.BytesIO()
    prefix = "study-abroad-planner-" + COMMIT + "/"
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(prefix, b"")
        archive.writestr(prefix + "scripts/", b"")
        archive.writestr(prefix + "references/", b"")
        archive.writestr(prefix + "PACKAGE-MANIFEST.json", raw)
        for name, data in files.items():
            archive.writestr(prefix + name, data)
        for name, data in (extras or {}).items():
            archive.writestr(name, data)
        if zip_change:
            zip_change(archive, prefix)
    body = stream.getvalue()
    entry = metadata()
    entry.update(manifest_sha256=hashlib.sha256(raw).hexdigest(),
                 archive_sha256=hashlib.sha256(body).hexdigest(), archive_size=len(body))
    return body, entry, files


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.network = mock.patch("urllib.request.OpenerDirector.open",
                                  side_effect=AssertionError("real HTTP forbidden"))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.tmp.cleanup()

    def test_index_channels_validate_and_unknown_schema_is_rejected(self):
        self.assertEqual(feed.validate_index(index()), index())
        for key, value in (("schema_version", 2), ("schema_version", True), ("repository", "other/repo")):
            item = index()
            item[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(feed.FeedError):
                feed.validate_index(item)
        item = index()
        item["extra"] = True
        with self.assertRaises(feed.FeedError):
            feed.validate_index(item)

    def test_stable_never_accepts_prerelease_and_pilot_is_explicit(self):
        item = index()
        item["channels"]["stable"] = metadata()
        with self.assertRaises(feed.FeedError):
            feed.validate_index(item)
        for version in ("0.10.0", "0.10.0-beta.1", "0.10.0-pilot.01"):
            item = index()
            item["channels"]["pilot"] = metadata(version)
            with self.subTest(version=version), self.assertRaises(feed.FeedError):
                feed.validate_index(item)

    def test_entries_require_immutable_commit_hash_and_bounded_archive_metadata(self):
        for key, value in (("commit", "main"), ("commit", "A" * 40), ("manifest_sha256", "z" * 64),
                           ("archive_size", True), ("archive_size", feed.MAX_ARCHIVE_SIZE + 1),
                           ("archive_sha256", "https://elsewhere"), ("version", "not-a-version")):
            entry = metadata()
            entry[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(feed.FeedError):
                feed.validate_entry(entry)

    def test_fetch_index_uses_static_url_and_etag(self):
        opener = mock.Mock()
        opener.open.return_value = Response(json.dumps(index()).encode(), headers={"ETag": '"abc"'})
        with mock.patch.object(feed.urllib.request, "build_opener", return_value=opener):
            result = feed.fetch_index(etag='"previous"')
        request = opener.open.call_args[0][0]
        self.assertEqual(request.full_url, feed.INDEX_URL)
        self.assertNotIn("api.github.com", request.full_url)
        self.assertEqual(request.get_header("If-none-match"), '"previous"')
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(result, {"not_modified": False, "etag": '"abc"', "index": index()})

    def test_conditional_304_is_only_accepted_when_etag_was_sent(self):
        for etag in (None, '"old"'):
            opener = mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError(feed.INDEX_URL, 304, "unchanged", {}, None)
            with mock.patch.object(feed.urllib.request, "build_opener", return_value=opener):
                if etag:
                    self.assertEqual(feed.fetch_index(etag), {"not_modified": True, "etag": etag, "index": None})
                else:
                    with self.assertRaises(feed.FeedError):
                        feed.fetch_index()

    def test_rate_limit_headers_are_respected_without_api_fallback(self):
        for status in (403, 429):
            opener = mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError(feed.INDEX_URL, status, "limit", {
                "Retry-After": "120", "X-RateLimit-Reset": "1300"}, None)
            with mock.patch.object(feed.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(feed.FeedError) as caught:
                    feed.fetch_index(now=1000)
            self.assertEqual((caught.exception.code, caught.exception.retry_at, caught.exception.status),
                             ("rate_limited", 1300, status))
            self.assertEqual(opener.open.call_count, 1)

    def test_retry_after_http_date_and_invalid_values(self):
        self.assertEqual(feed._retry_at({"Retry-After": "Thu, 01 Jan 1970 00:30:00 GMT"}, 1000), 1800)
        self.assertEqual(feed._retry_at({"Retry-After": "nan", "X-RateLimit-Reset": "inf"}, 1000), 1060)
        self.assertEqual(feed._retry_at({}, 1000), 1060)

    def test_index_duplicate_keys_invalid_json_oversize_and_redirect_are_rejected(self):
        bodies = [b'{"schema_version":1,"schema_version":1}', b"not json", b"x" * (feed.MAX_INDEX_SIZE + 1)]
        for body in bodies:
            opener = mock.Mock()
            opener.open.return_value = Response(body)
            with self.subTest(size=len(body)), mock.patch.object(feed.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(feed.FeedError):
                    feed.fetch_index()
        opener.open.return_value = Response(json.dumps(index()).encode(), url="https://evil.invalid/")
        with mock.patch.object(feed.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(feed.FeedError, "unexpected_redirect"):
                feed.fetch_index()
        with self.assertRaisesRegex(feed.FeedError, "unexpected_redirect"):
            feed._NoRedirects().redirect_request(None, None, 302, "redirect", {}, "https://evil.invalid")

    def test_network_error_is_typed_and_etag_cannot_inject_headers(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("offline")
        with mock.patch.object(feed.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(feed.FeedError, "network_error"):
                feed.fetch_index()
            with self.assertRaisesRegex(feed.FeedError, "invalid_etag"):
                feed.fetch_index('"abc"\r\nAuthorization: secret')

    def test_content_length_limits_and_short_body_are_rejected(self):
        for length, body in ((-1, b"a"), (feed.MAX_INDEX_SIZE + 1, b"a"), (3, b"ab"), ("no", b"ab")):
            with self.subTest(length=length), self.assertRaises(feed.FeedError):
                feed._bounded_body(Response(body, headers={"Content-Length": str(length)}), feed.MAX_INDEX_SIZE, float("inf"))

    def test_deadline_stops_slow_body(self):
        with mock.patch.object(feed.time, "monotonic", side_effect=[1, 3]):
            with self.assertRaisesRegex(feed.FeedError, "download_timeout"):
                feed._bounded_body(Response(b"value"), 100, 2)

    def test_real_urllib_redirect_pipeline_does_not_follow_redirect(self):
        requests = []
        class RedirectingHTTPS(urllib.request.HTTPSHandler):
            def https_open(self, request):
                requests.append(request.full_url)
                response = urllib.response.addinfourl(io.BytesIO(b""),
                    {"Location": "https://evil.invalid/package.zip"}, request.full_url, 302)
                response.msg = "Found"
                return response
        # Real urllib dispatch with a fake transport: no socket or remote I/O.
        opener = urllib.request.build_opener(feed._NoRedirects(), RedirectingHTTPS())
        self.network.stop()
        try:
            with mock.patch.object(feed.urllib.request, "build_opener", return_value=opener):
                with self.assertRaisesRegex(feed.FeedError, "unexpected_redirect"):
                    feed.fetch_index()
        finally:
            self.network.start()
        self.assertEqual(requests, [feed.INDEX_URL])

    def test_download_deadline_includes_time_spent_opening_connection(self):
        opener = mock.Mock()
        response = Response(b"value")
        opener.open.return_value = response
        with mock.patch.object(feed.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(feed.time, "monotonic", side_effect=[100, 102]):
            with self.assertRaisesRegex(feed.FeedError, "download_timeout"):
                feed._request(feed.INDEX_URL, 100, 3, 1)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 1)

    def download(self, body, entry, destination=None):
        target = destination or self.base / "new"
        with mock.patch.object(feed, "_request", return_value=(body, None)):
            return feed.download_package(entry, target)

    def test_valid_package_extracts_only_manifest_without_executing_python(self):
        extra = "study-abroad-planner-" + COMMIT + "/maintenance.json"
        body, entry, files = package(extras={extra: b"not part of runtime"})
        result = self.download(body, entry)
        self.assertEqual(result, self.base / "new")
        self.assertFalse((result / "maintenance.json").exists())
        self.assertEqual(feed.install._tree_files(result), set(files) | {"PACKAGE-MANIFEST.json"})
        self.assertEqual(feed.install.verify_package(result)["version"], entry["version"])

    def test_package_fetch_uses_only_exact_codeload_commit(self):
        body, entry, unused = package()
        with mock.patch.object(feed, "_request", return_value=(body, None)) as request:
            feed.download_package(entry, self.base / "new")
        self.assertEqual(request.call_args[0][0], feed.ARCHIVE_PREFIX + COMMIT)
        with self.assertRaisesRegex(feed.FeedError, "untrusted_url"):
            feed._request("https://example.com/anything", 10, 3, 10)

    def test_archive_hash_size_and_manifest_hash_failure_never_leave_a_package(self):
        body, entry, unused = package()
        for key, value in (("archive_sha256", "0" * 64), ("archive_size", len(body) + 1),
                           ("manifest_sha256", "0" * 64)):
            changed = dict(entry, **{key: value})
            with self.subTest(key=key), self.assertRaises(feed.FeedError):
                self.download(body, changed)
            self.assertFalse((self.base / "new").exists())

    def test_missing_file_and_tampered_digest_fail_and_clean_owned_directory(self):
        for mutate in (lambda m: m["files"]["VERSION"].update(sha256="0" * 64),
                       lambda m: m["files"].update({"absent.md": {"size": 1, "sha256": "0" * 64}}),
                       lambda m: m.update(version="0.10.0")):
            body, entry, unused = package(manifest_change=mutate)
            with self.assertRaises(feed.FeedError):
                self.download(body, entry)
            self.assertFalse((self.base / "new").exists())

    def test_existing_directory_and_symlink_parent_are_never_overwritten(self):
        body, entry, unused = package()
        target = self.base / "new"
        target.mkdir()
        (target / "precious.txt").write_bytes(b"keep")
        with self.assertRaisesRegex(feed.FeedError, "invalid_destination"):
            self.download(body, entry)
        self.assertEqual((target / "precious.txt").read_bytes(), b"keep")
        link = self.base / "link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        with self.assertRaisesRegex(feed.FeedError, "invalid_destination"):
            self.download(body, entry, link / "new")
        self.assertFalse((target / "new").exists())

    def test_traversal_absolute_wrong_root_and_windows_names_are_rejected(self):
        prefix = "study-abroad-planner-" + COMMIT + "/"
        paths = [prefix + "../outside", "/absolute", "different-root/extra", prefix + "a\\b", prefix + "CON.txt",
                 prefix + "nested/trailing.", prefix + "bad:stream", prefix + "a//b"]
        for path in paths:
            body, entry, unused = package(extras={path: b"unsafe"})
            with self.subTest(path=path), self.assertRaises(feed.FeedError):
                self.download(body, entry)
            self.assertFalse((self.base / "new").exists())
        self.assertFalse((self.base / "outside").exists())

    def test_casefold_unicode_duplicate_and_file_parent_collisions_are_rejected(self):
        prefix = "study-abroad-planner-" + COMMIT + "/"
        for extras in ({prefix + "skill.md": b"collision"},
                       {prefix + "caf\u00e9.md": b"one", prefix + "cafe\u0301.md": b"two"},
                       {prefix + "x": b"file", prefix + "x/child": b"child"}):
            body, entry, unused = package(extras=extras)
            with self.assertRaises(feed.FeedError):
                self.download(body, entry)
            self.assertFalse((self.base / "new").exists())

    def test_symlink_and_nonregular_members_are_rejected_even_if_unlisted(self):
        for kind in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
            def add(archive, prefix):
                member = zipfile.ZipInfo(prefix + "unsafe")
                member.create_system = 3
                member.external_attr = (kind | 0o777) << 16
                archive.writestr(member, b"../../outside")
            body, entry, unused = package(zip_change=add)
            with self.subTest(kind=kind), self.assertRaisesRegex(feed.FeedError, "unsafe_archive_type"):
                self.download(body, entry)
            self.assertFalse((self.base / "new").exists())

    def test_zip_bomb_and_member_count_limit_are_rejected(self):
        body, entry, unused = package(contents={"large.txt": b"A" * 2000000})
        with self.assertRaisesRegex(feed.FeedError, "archive_too_large"):
            self.download(body, entry)
        body, entry, unused = package()
        with mock.patch.object(feed, "MAX_ARCHIVE_FILES", 2):
            with self.assertRaisesRegex(feed.FeedError, "invalid_archive"):
                self.download(body, entry)
        self.assertFalse((self.base / "new").exists())

    def test_bad_zip_is_rejected(self):
        entry = metadata()
        with self.assertRaises(feed.FeedError):
            self.download(b"not a zip", entry)
        self.assertFalse((self.base / "new").exists())

    def test_embedded_nul_filename_is_rejected_before_zipinfo_truncation(self):
        prefix = "study-abroad-planner-" + COMMIT + "/"
        body, entry, unused = package(extras={prefix + "badXYZ": b"unsafe"})
        body = body.replace(b"badXYZ", b"bad\x00YZ")
        entry["archive_sha256"] = hashlib.sha256(body).hexdigest()
        with self.assertRaisesRegex(feed.FeedError, "unsafe_archive_path"):
            self.download(body, entry)
        self.assertFalse((self.base / "new").exists())

    def test_failure_does_not_delete_another_directory(self):
        body, entry, unused = package()
        target = self.base / "new"
        moved = self.base / "original-stage"
        def replace_stage(unused_body, unused_entry, path, unused_deadline):
            path.rename(moved)
            path.mkdir()
            (path / "precious").write_bytes(b"keep")
            raise feed.FeedError("interrupted")
        with mock.patch.object(feed, "_extract_verified", side_effect=replace_stage):
            with self.assertRaisesRegex(feed.FeedError, "interrupted"):
                self.download(body, entry)
        self.assertEqual((target / "precious").read_bytes(), b"keep")
        self.assertTrue(moved.exists())


if __name__ == "__main__":
    unittest.main()
