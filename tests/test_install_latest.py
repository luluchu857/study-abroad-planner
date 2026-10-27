"""Latest-publication installer tests with isolated targets and mocked transport."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import install_latest as latest


def package(path, version, policy=True, cards=None, extra=None):
    path.mkdir(parents=True)
    contents = {"VERSION": version + "\n", "SKILL.md": "# Study abroad\n",
                "scripts/query.py": "print('existing query')\n",
                "references/knowledge-cards.json": json.dumps({"cards": [{"id": "keep", "content": "preserve"}] if cards is None else cards})}
    if policy:
        contents["UPDATE-POLICY.json"] = json.dumps({"schema_version": 1, "capabilities": ["query", "knowledge"]})
    contents.update(extra or {})
    manifest = {"version": version, "files": {}}
    for name, text in contents.items():
        dest = path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode()
        dest.write_bytes(data)
        manifest["files"][name] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    (path / "PACKAGE-MANIFEST.json").write_text(json.dumps(manifest))
    return path


def entry(path):
    return {"version": (path / "VERSION").read_text().strip(), "commit": "a" * 40,
            "manifest_sha256": hashlib.sha256((path / "PACKAGE-MANIFEST.json").read_bytes()).hexdigest()}


def response(stable=None, pilot=None):
    return {"not_modified": False, "etag": None,
            "index": {"schema_version": 1, "repository": latest.feed.REPOSITORY,
                      "channels": {"stable": entry(stable) if stable else None,
                                   "pilot": entry(pilot) if pilot else None}}}


def transport(source):
    def download(selected, destination):
        shutil.copytree(source, destination)
        return destination
    return download


class LatestInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / "skills" / "study-abroad-planner"
        self.state = self.root / "runtime-state"
        self.no_net = mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Real network forbidden"))
        self.no_net.start()

    def tearDown(self):
        self.no_net.stop()
        self.temp.cleanup()

    def install(self, data, source, **options):
        return latest.install_latest("custom", self.target, state=self.state,
                                     fetch=lambda: data, download=transport(source), **options)

    def test_new_install_chooses_highest_public_version_and_does_not_run_candidate(self):
        stable = package(self.root / "stable", "0.9.0")
        pilot = package(self.root / "pilot", "0.10.0-pilot.3", extra={"scripts/install.py": "raise RuntimeError('must not run')\n"})
        result = self.install(response(stable, pilot), pilot)
        self.assertEqual(result["channel"], "pilot")
        self.assertEqual(result["version"], "0.10.0-pilot.3")
        self.assertEqual(result["status"], "installed")
        self.assertEqual((self.target / "scripts/install.py").read_text(), "raise RuntimeError('must not run')\n")
        self.assertFalse(self.state.exists())

    def test_stable_only_is_explicit_and_stable_semver_can_win_default(self):
        stable = package(self.root / "stable", "0.10.0")
        pilot = package(self.root / "pilot", "0.10.0-pilot.3")
        self.assertEqual(latest.select_latest(response(stable, pilot)["index"])[0], "stable")
        newer = package(self.root / "newer", "0.11.0-pilot.1")
        result = self.install(response(stable, newer), stable, stable_only=True)
        self.assertEqual(result["channel"], "stable")
        self.assertEqual(result["version"], "0.10.0")

    def test_empty_channel_reports_no_published_version(self):
        with self.assertRaisesRegex(latest.LatestInstallError, "no_published_version"):
            latest.select_latest(response()["index"])

    def test_existing_runtime_updates_without_replacing_bootstrap_or_pinned_directory(self):
        package(self.target, "0.10.0-pilot.2")
        current = latest.runtime.resolve_runtime(self.target, self.state)
        pinned = Path(current["runtime_path"])
        newer = package(self.root / "newer", "0.10.0-pilot.3", extra={"SKILL.md": "New instructions\n"})
        result = self.install(response(pilot=newer), newer)
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["bootstrap_version"], "0.10.0-pilot.2")
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.10.0-pilot.2")
        self.assertEqual((pinned / "SKILL.md").read_text(), "# Study abroad\n")
        self.assertEqual(latest.runtime.resolve_runtime(self.target, self.state)["version"], "0.10.0-pilot.3")

    def test_existing_effective_version_newer_than_feed_is_not_downgraded(self):
        package(self.target, "0.10.0-pilot.2")
        newer = package(self.root / "newer", "0.11.0-pilot.1")
        latest.runtime.publish_candidate(newer, self.state, self.target)
        old_feed = package(self.root / "old-feed", "0.10.0-pilot.3")
        download = mock.Mock(side_effect=AssertionError("Must not download downgrade"))
        result = latest.install_latest("custom", self.target, state=self.state,
                                      fetch=lambda: response(pilot=old_feed), download=download)
        self.assertEqual(result["status"], "kept_newer")
        self.assertEqual(result["effective_version"], "0.11.0-pilot.1")
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.10.0-pilot.2")

    def test_same_effective_version_is_unchanged_and_same_version_other_hash_is_rejected(self):
        package(self.target, "0.10.0-pilot.2")
        data = response(pilot=self.target)
        download = mock.Mock(side_effect=AssertionError("No download"))
        result = latest.install_latest("custom", self.target, state=self.state,
                                      fetch=lambda: data, download=download)
        self.assertEqual(result["status"], "unchanged")
        data["index"]["channels"]["pilot"]["manifest_sha256"] = "1" * 64
        with self.assertRaisesRegex(latest.LatestInstallError, "same_version_different_package"):
            latest.install_latest("custom", self.target, state=self.state, fetch=lambda: data, download=download)

    def test_legacy_clean_package_migrates_with_full_backup(self):
        package(self.target, "0.9.0", policy=False)
        newer = package(self.root / "newer", "0.10.0-pilot.3")
        result = self.install(response(pilot=newer), newer)
        self.assertEqual(result["status"], "updated")
        self.assertEqual((Path(result["backup"]) / "VERSION").read_text().strip(), "0.9.0")
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.10.0-pilot.3")
        self.assertFalse(self.state.exists())

    def test_legacy_extra_file_is_preserved_and_blocks_all_network(self):
        package(self.target, "0.9.0", policy=False)
        (self.target / "personal-notes.txt").write_text("preserve")
        fetch = mock.Mock(side_effect=AssertionError("No network"))
        with self.assertRaisesRegex(latest.LatestInstallError, "existing_installation_modified_or_invalid"):
            latest.install_latest("custom", self.target, state=self.state, fetch=fetch)
        self.assertEqual((self.target / "personal-notes.txt").read_text(), "preserve")

    def test_new_install_network_failure_does_not_create_target(self):
        with self.assertRaises(latest.feed.FeedError):
            latest.install_latest("custom", self.target, state=self.state,
                                  fetch=mock.Mock(side_effect=latest.feed.FeedError("rate_limited", retry_at=1000)))
        self.assertFalse(self.target.parent.exists())
        self.assertFalse(self.state.exists())

    def test_304_without_fresh_index_is_not_latest(self):
        with self.assertRaisesRegex(latest.LatestInstallError, "fresh_update_index_required"):
            latest.install_latest("custom", self.target, fetch=lambda: {"not_modified": True, "index": None})
        self.assertFalse(self.target.exists())

    def test_legacy_modified_during_download_is_not_overwritten(self):
        package(self.target, "0.9.0", policy=False)
        newer = package(self.root / "newer", "0.10.0-pilot.3")
        def download(selected, destination):
            (self.target / "SKILL.md").write_text("user edit during download")
            return transport(newer)(selected, destination)
        with self.assertRaisesRegex(latest.LatestInstallError, "existing_installation_modified_or_invalid"):
            latest.install_latest("custom", self.target, state=self.state, fetch=lambda: response(pilot=newer), download=download)
        self.assertEqual((self.target / "SKILL.md").read_text(), "user edit during download")

    def test_legacy_modified_during_staging_is_not_overwritten(self):
        package(self.target, "0.9.0", policy=False)
        newer = package(self.root / "newer", "0.10.0-pilot.3")
        verify = latest.install.verify_package
        def edit_while_staging(path, *args, **kwargs):
            result = verify(path, *args, **kwargs)
            if ".staging-" in Path(path).name:
                (self.target / "SKILL.md").write_text("user edit during staging")
            return result
        with mock.patch.object(latest.install, "verify_package", side_effect=edit_while_staging):
            with self.assertRaises(latest.install.InstallError):
                self.install(response(pilot=newer), newer)
        self.assertEqual((self.target / "SKILL.md").read_text(), "user edit during staging")
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.9.0")

    def test_missing_legacy_knowledge_and_runtime_capabilities_block_update(self):
        package(self.target, "0.9.0", policy=False)
        newer = package(self.root / "newer", "0.10.0-pilot.3", cards=[])
        with self.assertRaisesRegex(latest.LatestInstallError, "knowledge_would_change_or_be_removed"):
            self.install(response(pilot=newer), newer)
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.9.0")

    def test_existing_runtime_edit_blocks_replacement(self):
        package(self.target, "0.10.0-pilot.2")
        pinned = Path(latest.runtime.resolve_runtime(self.target, self.state)["runtime_path"])
        (pinned / "SKILL.md").write_text("custom content")
        fetch = mock.Mock(side_effect=AssertionError("No network"))
        with self.assertRaisesRegex(latest.LatestInstallError, "selected_runtime_modified"):
            latest.install_latest("custom", self.target, state=self.state, fetch=fetch)
        self.assertEqual((pinned / "SKILL.md").read_text(), "custom content")

    def test_new_target_appearing_during_download_is_not_overwritten(self):
        newer = package(self.root / "newer", "0.10.0-pilot.3")
        def download(selected, destination):
            self.target.mkdir(parents=True)
            (self.target / "important.txt").write_text("keep")
            return transport(newer)(selected, destination)
        with self.assertRaisesRegex(latest.LatestInstallError, "installation_appeared_during_download"):
            latest.install_latest("custom", self.target, fetch=lambda: response(pilot=newer), download=download)
        self.assertEqual((self.target / "important.txt").read_text(), "keep")

    def test_download_identity_must_match_index_even_for_mock_transport(self):
        newer = package(self.root / "newer", "0.10.0-pilot.3")
        data = response(pilot=newer)
        data["index"]["channels"]["pilot"]["manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(latest.LatestInstallError, "download_identity_mismatch"):
            self.install(data, newer)
        self.assertFalse(self.target.exists())

    def test_cli_reports_rate_limit_failure_without_claiming_install_success(self):
        stream = io.StringIO()
        with mock.patch.object(latest, "install_latest", side_effect=latest.feed.FeedError("rate_limited", retry_at=9999)), contextlib.redirect_stdout(stream):
            result = latest.main(["--host", "custom", "--destination", str(self.target)])
        value = json.loads(stream.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["error"], "rate_limited")
        self.assertEqual(value["retry_at"], 9999)

    def test_cli_reports_recovery_backup_when_replace_and_rollback_fail(self):
        stream = io.StringIO()
        backup = str(self.root / "skill-backups" / "original")
        failure = latest.install.InstallError("Replacement failed; original package remains at " + backup, backup=backup)
        with mock.patch.object(latest, "install_latest", side_effect=failure), contextlib.redirect_stdout(stream):
            result = latest.main(["--host", "custom", "--destination", str(self.target)])
        value = json.loads(stream.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(value["status"], "error")
        self.assertEqual(value["error"], "package_install_failed")
        self.assertEqual(value["backup"], backup)
        self.assertNotIn("existing_files_preserved", value)


if __name__ == "__main__":
    unittest.main()
