"""Scheduling and transport-to-runtime integration. No real network or installs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import test_support as tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import auto_update as auto


def package(root, version="0.10.0-pilot.2", extra=None):
    root.mkdir(parents=True)
    contents = {"VERSION": version + "\n", "SKILL.md": "---\nname: study-abroad-planner\n---\nConsult normally.\n",
                "UPDATE-POLICY.json": json.dumps({"schema_version": 1, "capabilities": ["knowledge", "reports"]}),
                "references/knowledge-cards.json": json.dumps({"cards": [{"id": "keep-me", "content": "original knowledge"}]}),
                "scripts/report.py": "print('original feature')\n"}
    contents.update(extra or {})
    manifest = {"version": version, "files": {}}
    for name, text in contents.items():
        path = root / name
        path.parent.mkdir(exist_ok=True, parents=True)
        data = text.encode()
        path.write_bytes(data)
        manifest["files"][name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    (root / "PACKAGE-MANIFEST.json").write_text(json.dumps(manifest))
    return root


def index(version="0.10.0-pilot.3"):
    entry = {"version": version, "commit": "1" * 40, "manifest_sha256": "2" * 64}
    return {"schema_version": 1, "repository": auto.feed.REPOSITORY,
            "channels": {"stable": None, "pilot": entry}}


class AutoUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.base = package(self.root / "installed")
        self.state = self.root / "state"
        self.no_net = mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No network"))
        self.no_net.start()

    def tearDown(self):
        self.no_net.stop()
        self.temp.cleanup()

    def test_first_activation_pins_runtime_without_network(self):
        spawn = mock.Mock()
        with mock.patch.object(auto.feed, "fetch_index", side_effect=AssertionError("foreground network")):
            result = auto.activate(self.base, self.state, spawn=spawn, clock=lambda: 1000)
        self.assertEqual(result["effective_version"], "0.10.0-pilot.2")
        self.assertTrue(Path(result["runtime_skill_path"]).is_file())
        spawn.assert_called_once_with(self.base, self.state)

    def test_update_preserves_pinned_consultation_and_next_call_uses_new(self):
        first = auto.activate(self.base, self.state, spawn=lambda *args: None)
        old_path = Path(first["runtime_path"])
        original = (old_path / "SKILL.md").read_bytes()
        new = package(self.root / "new", "0.10.0-pilot.3", {"SKILL.md": "New instructions\n"})
        def download(entry, destination):
            shutil.copytree(new, destination)
            return destination
        with mock.patch.object(auto.feed, "fetch_index", return_value={"not_modified": False, "etag": '"one"', "index": index()}), mock.patch.object(auto.feed, "download_package", side_effect=download):
            result = auto.worker(self.base, self.state, clock=lambda: 1000)
        self.assertEqual(result["status"], "updated")
        self.assertEqual((old_path / "SKILL.md").read_bytes(), original)
        self.assertEqual((self.base / "VERSION").read_text().strip(), "0.10.0-pilot.2")
        second = auto.activate(self.base, self.state, background=False)
        self.assertEqual(second["effective_version"], "0.10.0-pilot.3")
        self.assertNotEqual(second["runtime_path"], first["runtime_path"])

    def test_failure_keeps_pinned_version_and_uses_short_backoff(self):
        before = auto.activate(self.base, self.state, background=False)
        with mock.patch.object(auto.feed, "fetch_index", side_effect=auto.feed.FeedError("network_error")):
            result = auto.worker(self.base, self.state, clock=lambda: 1000)
        after = auto.activate(self.base, self.state, background=False)
        self.assertEqual(after["runtime_path"], before["runtime_path"])
        self.assertEqual(result["next_check_at"], 1300)
        self.assertEqual(result["status"], "kept_current")

    def test_rate_limit_respected_even_by_force(self):
        with mock.patch.object(auto.feed, "fetch_index", side_effect=auto.feed.FeedError("rate_limited", retry_at=8000)) as fetch:
            result = auto.worker(self.base, self.state, clock=lambda: 1000)
            self.assertEqual(result["next_check_at"], 8000)
            auto.worker(self.base, self.state, force=True, clock=lambda: 2000)
            self.assertEqual(fetch.call_count, 1)

    def test_cold_and_successful_cache_do_not_poll_in_foreground(self):
        with mock.patch.object(auto.feed, "fetch_index", return_value={"not_modified": False, "etag": '"one"', "index": index("0.10.0-pilot.2")}) as fetch:
            result = auto.worker(self.base, self.state, clock=lambda: 1000)
            self.assertEqual(result["next_check_at"], 22600)
            spawn = mock.Mock()
            auto.activate(self.base, self.state, spawn=spawn, clock=lambda: 1001)
            spawn.assert_not_called()
            self.assertEqual(fetch.call_count, 1)

    def test_etag_304_reuses_only_validated_cached_feed(self):
        value = {"schema_version": 1, "checked_at": 1000, "next_check_at": 1001,
                 "etag": '"one"', "index": index("0.10.0-pilot.2")}
        auto.save_status(self.state, value)
        with mock.patch.object(auto.feed, "fetch_index", return_value={"not_modified": True, "etag": '"one"', "index": None}) as fetch:
            result = auto.worker(self.base, self.state, clock=lambda: 1002)
        fetch.assert_called_once_with(etag='"one"')
        self.assertEqual(result["status"], "up_to_date")

    def test_etag_without_valid_index_is_not_sent(self):
        auto.save_status(self.state, {"schema_version": 1, "checked_at": 1, "next_check_at": 2, "etag": '"one"'})
        with mock.patch.object(auto.feed, "fetch_index", return_value={"not_modified": False, "etag": None, "index": index("0.10.0-pilot.2")}) as fetch:
            auto.worker(self.base, self.state, clock=lambda: 3)
        fetch.assert_called_once_with(etag=None)

    def test_local_changes_stop_download_and_preserve_user_file(self):
        auto.activate(self.base, self.state, background=False)
        (self.base / "my-notes.txt").write_text("keep my changes")
        with mock.patch.object(auto.feed, "fetch_index") as fetch:
            result = auto.worker(self.base, self.state, clock=lambda: 1000)
        fetch.assert_not_called()
        self.assertEqual(result["status"], "preserved")
        self.assertEqual((self.base / "my-notes.txt").read_text(), "keep my changes")

    def test_missing_knowledge_blocks_commit_and_keeps_old(self):
        before = auto.activate(self.base, self.state, background=False)
        new = package(self.root / "new", "0.10.0-pilot.3", {"references/knowledge-cards.json": '{"cards": []}'})
        def download(entry, destination):
            shutil.copytree(new, destination)
            return destination
        with mock.patch.object(auto.feed, "fetch_index", return_value={"not_modified": False, "etag": None, "index": index()}), mock.patch.object(auto.feed, "download_package", side_effect=download):
            result = auto.worker(self.base, self.state, clock=lambda: 1000)
        self.assertEqual(result["error"], "knowledge_would_change_or_be_removed")
        self.assertEqual(auto.activate(self.base, self.state, background=False)["runtime_path"], before["runtime_path"])

    def test_downgrade_never_downloaded_and_stable_skips_pilot(self):
        self.assertIsNone(auto.choose_release(index("0.10.0-pilot.1"), "pilot", "0.10.0-pilot.2"))
        self.assertIsNone(auto.choose_release(index("0.11.0-pilot.1"), "stable", "0.10.0"))
        value = index()
        value["channels"]["stable"] = dict(value["channels"]["pilot"], version="0.10.0")
        self.assertEqual(auto.choose_release(value, "pilot", "0.10.0-pilot.2")["version"], "0.10.0")

    def test_state_inside_installation_rejected_before_any_write(self):
        with self.assertRaises(auto.runtime.RuntimeUpdateError):
            auto.worker(self.base, self.base / "bad-state")
        self.assertFalse((self.base / "bad-state").exists())

    def test_background_spawn_failure_does_not_break_consultation(self):
        result = auto.activate(self.base, self.state, spawn=mock.Mock(side_effect=OSError("unavailable")))
        self.assertEqual(result["update_error"], "background_start_failed")
        self.assertTrue(Path(result["runtime_skill_path"]).is_file())

    def test_invalid_cache_and_backward_clock(self):
        auto.save_status(self.state, {"schema_version": 1, "checked_at": 1000, "next_check_at": float("nan")})
        self.assertEqual(auto.read_status(self.state), {})
        self.assertTrue(auto.due({"checked_at": 10000, "next_check_at": 20000}, 1000))


if __name__ == "__main__":
    unittest.main()
