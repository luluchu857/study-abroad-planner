"""Installer and updater tests: temporary directories and mocked HTTP only."""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def _module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = _module("skill_installer_under_test", "install.py")
updates = _module("skill_updater_under_test", "check_update.py")


def make_package(directory, version="0.8.0", files=None):
    directory.mkdir(parents=True, exist_ok=True)
    contents = {"VERSION": (version + "\n").encode(),
                "SKILL.md": b"---\nname: study-abroad-planner\n---\nSkill instructions.\n",
                "references/example.md": b"Reference content.\n"}
    contents.update(files or {})
    manifest = {"version": version, "files": {}}
    for name, data in contents.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest["files"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    (directory / install.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def published(version="0.8.1"):
    return {"tag_name": "v" + version, "html_url": updates.RELEASE_PREFIX + version,
            "draft": False, "prerelease": False}


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        # Unexpected HTTP is forbidden even if a test accidentally uses a default fetcher.
        self.no_network = mock.patch("urllib.request.OpenerDirector.open",
                                     side_effect=AssertionError("Real HTTP forbidden in tests"))
        self.no_network.start()

    def tearDown(self):
        self.no_network.stop()
        self.temp.cleanup()

    def symlink(self, target, link):
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic link creation is unavailable")


class InstallerTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.source = self.base / "source"
        self.target = self.base / "skills" / "study-abroad-planner"
        self.manifest = make_package(self.source)

    def write_manifest(self, value):
        (self.source / install.MANIFEST_NAME).write_text(json.dumps(value), encoding="utf-8")

    def test_install_copies_only_manifest_files_and_is_idempotent(self):
        (self.source / "unlisted-secret.txt").write_text("Do not install this")
        sibling = self.target.parent / "another-skill"
        sibling.mkdir(parents=True)
        (sibling / "sentinel").write_bytes(b"unchanged")
        result = install.install_package(self.source, self.target)
        self.assertEqual(result["status"], "installed")
        self.assertFalse((self.target / "unlisted-secret.txt").exists())
        self.assertEqual(install._tree_files(self.target), set(self.manifest["files"]) | {install.MANIFEST_NAME})
        times = {str(path): path.stat().st_mtime_ns for path in [self.target] + list(self.target.rglob("*"))}
        self.assertEqual(install.install_package(self.source, self.target)["status"], "unchanged")
        self.assertEqual(times, {str(path): path.stat().st_mtime_ns for path in [self.target] + list(self.target.rglob("*"))})
        self.assertEqual((sibling / "sentinel").read_bytes(), b"unchanged")
        self.assertEqual(list(self.target.parent.glob("*.backup-*")), [])

    def test_update_preserves_entire_old_directory_outside_active_skills_root(self):
        install.install_package(self.source, self.target)
        (self.target / "local-note.txt").write_text("keep in backup")
        make_package(self.source, "0.8.1", {"references/example.md": b"New reference.\n"})
        result = install.install_package(self.source, self.target)
        backup = Path(result["backup"])
        self.assertEqual(result["status"], "updated")
        self.assertEqual(backup.parent, self.target.parent.parent / "skill-backups")
        self.assertNotIn(self.target.parent, backup.parents)
        self.assertEqual((backup / "VERSION").read_text().strip(), "0.8.0")
        self.assertEqual((backup / "local-note.txt").read_text(), "keep in backup")
        self.assertFalse((self.target / "local-note.txt").exists())
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.8.1")
        install.verify_package(self.target)

    def test_custom_destination_uses_hidden_backup_directory(self):
        target = self.base / "custom-directory"
        install.install_package(self.source, target)
        make_package(self.source, "0.8.1")
        result = install.install_package(self.source, target)
        backup = Path(result["backup"])
        self.assertEqual(backup.parent, self.base / ".skill-backups")
        self.assertEqual((backup / "VERSION").read_text().strip(), "0.8.0")

    def test_unsafe_backup_directory_is_rejected_before_moving_original(self):
        install.install_package(self.source, self.target)
        make_package(self.source, "0.8.1")
        outside = self.base / "external-backups"
        outside.mkdir()
        self.symlink(outside, self.base / "skill-backups")
        with self.assertRaises(install.InstallError):
            install.install_package(self.source, self.target)
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.8.0")
        self.assertEqual(list(outside.iterdir()), [])

    def test_backup_write_preflight_failure_preserves_original(self):
        install.install_package(self.source, self.target)
        make_package(self.source, "0.8.1")
        with mock.patch.object(install.tempfile, "mkstemp", side_effect=PermissionError("Read-only backup root")):
            with self.assertRaises(PermissionError):
                install.install_package(self.source, self.target)
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.8.0")
        self.assertEqual(list(self.target.parent.glob(".*.staging-*")), [])
        self.assertFalse((self.target.parent / ".study-abroad-planner.install.lock").exists())

    def test_python_runtime_caches_do_not_trigger_reinstallation(self):
        install.install_package(self.source, self.target)
        generated = self.target / "scripts/__pycache__/install.cpython-39.pyc"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b"runtime cache")
        legacy_cache = self.target / "legacy.pyc"
        legacy_cache.write_bytes(b"old-style cache")
        before = self.target.stat().st_mtime_ns
        self.assertEqual(install.install_package(self.source, self.target)["status"], "unchanged")
        self.assertEqual(self.target.stat().st_mtime_ns, before)
        self.assertEqual(generated.read_bytes(), b"runtime cache")
        make_package(self.source, "0.8.1")
        result = install.install_package(self.source, self.target)
        self.assertEqual((Path(result["backup"]) / generated.relative_to(self.target)).read_bytes(), b"runtime cache")
        self.assertFalse(generated.exists())

    def test_dry_run_creates_nothing(self):
        result = install.install_package(self.source, self.target, dry_run=True)
        self.assertEqual(result["status"], "would_install")
        self.assertFalse(self.target.parent.exists())

    def test_hash_or_size_failure_leaves_destination_untouched(self):
        install.install_package(self.source, self.target)
        before = (self.target / "SKILL.md").read_bytes()
        (self.source / "SKILL.md").write_bytes(b"tampered")
        with self.assertRaises(install.InstallError):
            install.install_package(self.source, self.target)
        self.assertEqual((self.target / "SKILL.md").read_bytes(), before)
        self.assertEqual(list(self.target.parent.glob("*.backup-*")), [])

    def test_same_size_hash_mismatch_is_rejected(self):
        path = self.source / "references/example.md"
        path.write_bytes(b"X" * path.stat().st_size)
        with self.assertRaisesRegex(install.InstallError, "SHA-256 mismatch"):
            install.verify_package(self.source)

    def test_unsafe_manifest_paths_are_rejected(self):
        for name in ("../outside", "/absolute", "C:/outside", "a\\b", "a/../b", "a//b", "./b",
                     "a/./b", "a/", "NUL.txt", "a/trailing.", "a/trailing ", "a*", "a\x00b"):
            with self.subTest(name=name):
                changed = json.loads(json.dumps(self.manifest))
                changed["files"][name] = {"sha256": "0" * 64, "size": 0}
                self.write_manifest(changed)
                with self.assertRaises(install.InstallError):
                    install.read_manifest(self.source)

    def test_duplicate_keys_case_collisions_and_file_parent_collisions(self):
        manifest_path = self.source / install.MANIFEST_NAME
        manifest_path.write_text('{"version":"0.8.0","version":"0.8.1","files":{}}')
        with self.assertRaisesRegex(install.InstallError, "Duplicate JSON"):
            install.read_manifest(self.source)
        for names in (("a.md", "A.md"), ("a", "a/b"), ("caf\u00e9.md", "cafe\u0301.md")):
            with self.subTest(names=names):
                changed = json.loads(json.dumps(self.manifest))
                for name in names:
                    changed["files"][name] = {"sha256": "0" * 64, "size": 0}
                self.write_manifest(changed)
                with self.assertRaises(install.InstallError):
                    install.read_manifest(self.source)

    def test_manifest_limits_and_invalid_sizes(self):
        cases = []
        for size in (-1, True, 1.5, install.MAX_FILE_SIZE + 1):
            changed = json.loads(json.dumps(self.manifest))
            changed["files"]["VERSION"]["size"] = size
            cases.append(changed)
        too_many = {"version": "0.8.0", "files": {str(i): {"sha256": "0" * 64, "size": 0}
                                                   for i in range(install.MAX_FILES + 1)}}
        cases.append(too_many)
        too_large = {"version": "0.8.0", "files": {str(i): {"sha256": "0" * 64,
                                                               "size": install.MAX_FILE_SIZE}
                                                    for i in range(3)}}
        cases.append(too_large)
        for changed in cases:
            self.write_manifest(changed)
            with self.assertRaises(install.InstallError):
                install.read_manifest(self.source)

    def test_deep_manifest_json_is_reported_as_invalid(self):
        (self.source / install.MANIFEST_NAME).write_text("[" * 2000 + "0" + "]" * 2000)
        with self.assertRaisesRegex(install.InstallError, "Invalid package manifest"):
            install.read_manifest(self.source)

    def test_required_files_manifest_self_reference_and_version_match(self):
        for name in ("VERSION", "SKILL.md"):
            changed = json.loads(json.dumps(self.manifest))
            del changed["files"][name]
            self.write_manifest(changed)
            with self.assertRaises(install.InstallError):
                install.read_manifest(self.source)
        changed = json.loads(json.dumps(self.manifest))
        changed["files"][install.MANIFEST_NAME] = {"sha256": "0" * 64, "size": 0}
        self.write_manifest(changed)
        with self.assertRaises(install.InstallError):
            install.read_manifest(self.source)
        changed = json.loads(json.dumps(self.manifest))
        changed["version"] = "0.8.1"
        self.write_manifest(changed)
        with self.assertRaisesRegex(install.InstallError, "differs"):
            install.verify_package(self.source)

    def test_symlink_file_directory_manifest_and_destination_are_rejected(self):
        outside = self.base / "outside.md"
        outside.write_bytes(b"Reference content.\n")
        path = self.source / "references/example.md"
        path.unlink()
        self.symlink(outside, path)
        with self.assertRaises(install.InstallError):
            install.verify_package(self.source)
        path.unlink()
        path.parent.rmdir()
        external_dir = self.base / "external-dir"
        external_dir.mkdir()
        (external_dir / "example.md").write_bytes(outside.read_bytes())
        self.symlink(external_dir, path.parent)
        with self.assertRaises(install.InstallError):
            install.verify_package(self.source)
        path.parent.unlink()
        make_package(self.source)
        original = (self.source / install.MANIFEST_NAME).read_bytes()
        (self.source / install.MANIFEST_NAME).unlink()
        outside.write_bytes(original)
        self.symlink(outside, self.source / install.MANIFEST_NAME)
        with self.assertRaises(install.InstallError):
            install.read_manifest(self.source)
        (self.source / install.MANIFEST_NAME).unlink()
        make_package(self.source)
        self.target.parent.mkdir()
        self.symlink(external_dir, self.target)
        with self.assertRaises(install.InstallError):
            install.install_package(self.source, self.target)

    def test_existing_destination_internal_symlink_is_rejected(self):
        install.install_package(self.source, self.target)
        self.symlink(self.source / "VERSION", self.target / "extra-link")
        with self.assertRaises(install.InstallError):
            install.install_package(self.source, self.target)
        self.assertTrue((self.target / "extra-link").is_symlink())

    def test_roots_source_ancestors_descendants_and_other_host_names_rejected(self):
        for target in (Path(self.base.anchor), self.source, self.source.parent, self.source / "child"):
            with self.subTest(target=target), self.assertRaises(install.InstallError):
                install.install_package(self.source, target, dry_run=True)
        with self.assertRaises(install.InstallError):
            install.destination_for("codex", self.base / "other-skill")
        with self.assertRaises(install.InstallError):
            install.destination_for("workbuddy", self.base / "other-skill")
        with self.assertRaises(install.InstallError):
            install.destination_for("custom")
        self.assertEqual(install.destination_for("custom", self.base / "explicit-test"), self.base / "explicit-test")

    def test_default_destinations_are_host_specific(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.base / "codex")}), mock.patch.object(Path, "home", return_value=self.base):
            self.assertEqual(install.destination_for("codex"), self.base / "codex/skills/study-abroad-planner")
            self.assertEqual(install.destination_for("workbuddy"), self.base / ".workbuddy/skills/study-abroad-planner")

    def test_replace_failure_rolls_back_and_removes_stage_and_lock(self):
        install.install_package(self.source, self.target)
        make_package(self.source, "0.8.1")
        real_replace = os.replace

        def fail_stage(source, destination):
            if ".staging-" in str(source):
                raise OSError("Simulated replacement failure")
            return real_replace(source, destination)

        with mock.patch.object(install.os, "replace", side_effect=fail_stage):
            with self.assertRaisesRegex(install.InstallError, "restored"):
                install.install_package(self.source, self.target)
        self.assertEqual((self.target / "VERSION").read_text().strip(), "0.8.0")
        self.assertEqual(list(self.target.parent.glob("*.backup-*")), [])
        self.assertEqual(list(self.target.parent.glob(".*.staging-*")), [])
        self.assertFalse((self.target.parent / ".study-abroad-planner.install.lock").exists())

    def test_rollback_failure_reports_preserved_backup(self):
        install.install_package(self.source, self.target)
        make_package(self.source, "0.8.1")
        with mock.patch.object(install.os, "replace", side_effect=OSError("Simulated failure")):
            with self.assertRaises(install.InstallError) as caught:
                install.install_package(self.source, self.target)
        self.assertIsNotNone(caught.exception.backup)
        self.assertEqual((Path(caught.exception.backup) / "VERSION").read_text().strip(), "0.8.0")

    def test_concurrent_install_lock_is_not_removed(self):
        self.target.parent.mkdir(parents=True)
        lock = self.target.parent / ".study-abroad-planner.install.lock"
        lock.write_text("other process")
        with self.assertRaisesRegex(install.InstallError, "Another install"):
            install.install_package(self.source, self.target)
        self.assertEqual(lock.read_text(), "other process")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs unavailable")
    def test_non_regular_source_file_is_rejected_without_reading(self):
        path = self.source / "references/example.md"
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(install.InstallError):
            install.verify_package(self.source)


class UpdateTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.cache = self.base / "cache"
        self.now = 100000.0

    def checked(self, version="0.8.1"):
        return updates.check_update(self.cache, "0.8.0", clock=lambda: self.now,
                                    fetch=lambda: published(version))

    def test_strict_semver_and_precedence(self):
        ordered = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
                   "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0"]
        for first, second in zip(ordered, ordered[1:]):
            self.assertTrue(updates.newer_than(second, first))
            self.assertFalse(updates.newer_than(first, second))
        self.assertFalse(updates.newer_than("1.0.0+other", "1.0.0+build"))
        for value in ("v0.8.0", "01.2.3", "1.02.3", "1.2", "1.2.3.4", "1.2.3-01", "1.2.3-", "1.2.3+", "1.2.3\n", "1.2.3/../../x", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                updates.parse_version(value)

    def test_official_release_url_and_stable_metadata_only(self):
        self.assertEqual(updates.validate_release(published()), ("0.8.1", updates.RELEASE_PREFIX + "0.8.1"))
        changes = [{"html_url": "https://example.com/v0.8.1"},
                   {"html_url": updates.RELEASE_PREFIX + "0.8.1?redirect=evil"},
                   {"html_url": updates.RELEASE_PREFIX.replace("luluchu857", "someone") + "0.8.1"},
                   {"tag_name": "v0.8.1/../../x"}, {"tag_name": "0.8.1"},
                   {"draft": True}, {"prerelease": True}, {"draft": 0},
                   {"tag_name": "v0.9.0-rc.1", "html_url": updates.RELEASE_PREFIX + "0.9.0-rc.1"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                updates.validate_release(dict(published(), **change))

    def test_check_caches_new_version_and_does_not_download(self):
        fetch = mock.Mock(return_value=published())
        result = updates.check_update(self.cache, "0.8.0", clock=lambda: self.now, fetch=fetch)
        fetch.assert_called_once_with()
        self.assertEqual(result["status"], "checked")
        self.assertTrue(result["update_available"])
        self.assertEqual(result["release_url"], updates.RELEASE_PREFIX + "0.8.1")
        self.assertEqual({p.name for p in self.cache.iterdir()}, {updates.CACHE_NAME, updates.LOCK_NAME})
        self.assertEqual(updates.status(self.cache, "0.8.1", clock=lambda: self.now)["release_url"], None)

    def test_ttl_hit_returns_without_spawn_and_boundary_starts_worker(self):
        self.checked()
        spawn = mock.Mock()
        before = (self.cache / updates.CACHE_NAME).stat().st_mtime_ns
        result = updates.background_check(self.cache, "0.8.0", clock=lambda: self.now + updates.CACHE_TTL - 0.01, spawn=spawn)
        self.assertEqual(result["status"], "cached")
        spawn.assert_not_called()
        self.assertEqual((self.cache / updates.CACHE_NAME).stat().st_mtime_ns, before)
        result = updates.background_check(self.cache, "0.8.0", clock=lambda: self.now + updates.CACHE_TTL, spawn=spawn)
        self.assertEqual(result["status"], "checking")
        spawn.assert_called_once_with(self.cache)

    def test_background_worker_rechecks_cache_under_lock(self):
        self.checked()
        fetch = mock.Mock(side_effect=AssertionError("Fresh cache must not fetch"))
        result = updates.check_update(self.cache, "0.8.0", clock=lambda: self.now + 1, fetch=fetch, force=False)
        self.assertEqual(result["status"], "cached")
        fetch.assert_not_called()

    def test_explicit_check_refreshes_even_fresh_cache(self):
        self.checked()
        fetch = mock.Mock(return_value=published("0.8.2"))
        result = updates.check_update(self.cache, "0.8.0", clock=lambda: self.now + 1, fetch=fetch)
        fetch.assert_called_once()
        self.assertEqual(result["latest_version"], "0.8.2")

    def test_nonblocking_lock_prevents_http_and_duplicate_background_start(self):
        lock = updates.CheckLock(self.cache)
        self.assertTrue(lock.acquire())
        fetch, spawn = mock.Mock(), mock.Mock()
        try:
            result = updates.check_update(self.cache, "0.8.0", clock=lambda: self.now, fetch=fetch)
            self.assertEqual(result["status"], "checking")
            result = updates.background_check(self.cache, "0.8.0", clock=lambda: self.now, spawn=spawn)
            self.assertEqual(result["status"], "checking")
        finally:
            lock.release()
        fetch.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual(self.checked()["status"], "checked")

    def test_process_exit_releases_lock_without_deleting_lock_file(self):
        code = ("import importlib.util, os, sys; "
                "s=importlib.util.spec_from_file_location('up', sys.argv[1]); "
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "lock=m.CheckLock(sys.argv[2]); ok=lock.acquire(); os._exit(0 if ok else 2)")
        subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts/check_update.py"), str(self.cache)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertTrue((self.cache / updates.LOCK_NAME).exists())
        self.assertEqual(self.checked()["status"], "checked")

    def test_offline_errors_are_cached_without_losing_last_known_release(self):
        self.checked()
        later = self.now + updates.CACHE_TTL
        result = updates.check_update(self.cache, "0.8.0", clock=lambda: later,
                                      fetch=mock.Mock(side_effect=TimeoutError("offline")))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "network_error")
        self.assertEqual(result["latest_version"], "0.8.1")
        spawn = mock.Mock()
        cached = updates.background_check(self.cache, "0.8.0", clock=lambda: later + 1, spawn=spawn)
        self.assertEqual(cached["status"], "cached")
        spawn.assert_not_called()

    def test_invalid_release_does_not_poison_previous_cache(self):
        self.checked()
        bad = dict(published("999.0.0"), html_url="https://evil.example/release")
        result = updates.check_update(self.cache, "0.8.0", clock=lambda: self.now + 1, fetch=lambda: bad)
        self.assertEqual(result["error"], "invalid_release")
        self.assertEqual(result["latest_version"], "0.8.1")
        self.assertNotIn("evil", (self.cache / updates.CACHE_NAME).read_text())

    def test_corrupt_cross_repo_future_and_url_tampered_cache_is_ignored(self):
        self.checked()
        original = json.loads((self.cache / updates.CACHE_NAME).read_text())
        invalid = ["{broken", json.dumps(dict(original, repository="someone/else")),
                   json.dumps(dict(original, checked_at=self.now + 1000)),
                   json.dumps(dict(original, release_url="https://evil.example")),
                   json.dumps(dict(original, latest_version="0.9.0-rc.1",
                                   release_url=updates.RELEASE_PREFIX + "0.9.0-rc.1")),
                   json.dumps(dict(original, schema_version=True)),
                   json.dumps(dict(original, checked_at=float("nan"))),
                   json.dumps(dict(original, checked_at=10 ** 1000)),
                   "[" * 2000 + "0" + "]" * 2000]
        for raw in invalid:
            (self.cache / updates.CACHE_NAME).write_text(raw)
            result = updates.status(self.cache, "0.8.0", clock=lambda: self.now)
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["update_available"])

    def test_status_is_read_only_and_uses_no_network(self):
        result = updates.status(self.cache, "0.8.0", clock=lambda: self.now)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(self.cache.exists())

    def test_background_start_failure_is_nonfatal(self):
        result = updates.background_check(self.cache, "0.8.0", clock=lambda: self.now,
                                         spawn=mock.Mock(side_effect=OSError("cannot spawn")))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "background_start_failed")
        self.assertFalse(result["update_available"])
        self.assertEqual(self.checked()["status"], "checked")

    def test_cache_write_failure_is_nonfatal(self):
        with mock.patch.object(updates, "_save_cache", side_effect=OSError("read-only")):
            result = self.checked()
        self.assertEqual(result["error"], "cache_write_error")
        self.assertEqual(result["latest_version"], "0.8.1")

    def test_symlink_cache_and_lock_are_not_followed(self):
        outside = self.base / "outside"
        outside.write_text("keep")
        self.cache.mkdir()
        self.symlink(outside, self.cache / updates.CACHE_NAME)
        self.assertIsNone(updates.load_cache(self.cache, self.now))
        result = self.checked()
        self.assertEqual(result["error"], "cache_write_error")
        self.assertEqual(outside.read_text(), "keep")
        (self.cache / updates.CACHE_NAME).unlink()
        (self.cache / updates.LOCK_NAME).unlink()
        self.symlink(outside, self.cache / updates.LOCK_NAME)
        self.assertEqual(self.checked()["error"], "cache_unavailable")
        self.assertEqual(outside.read_text(), "keep")

    def test_fetch_uses_fixed_endpoint_timeout_and_no_authentication(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = updates.API_URL
        response.read.return_value = json.dumps(published()).encode()
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(updates.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(updates.fetch_release(), published())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, updates.API_URL)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)
        self.assertNotIn("Authorization", dict(request.header_items()))
        response.read.assert_called_once_with(updates.MAX_RESPONSE_SIZE + 1)

    def test_fetch_rejects_redirects_and_oversized_responses(self):
        request = updates.urllib.request.Request(updates.API_URL)
        with self.assertRaises(updates.urllib.error.HTTPError):
            updates._NoRedirects().redirect_request(request, None, 302, "redirect", {}, "https://evil.example")
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = updates.API_URL
        response.read.return_value = b"x" * (updates.MAX_RESPONSE_SIZE + 1)
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(updates.urllib.request, "build_opener", return_value=opener), self.assertRaises(ValueError):
            updates.fetch_release()

    def test_background_launch_is_detached_and_silences_child_output(self):
        self.cache.mkdir()
        with mock.patch.object(updates.subprocess, "Popen") as popen:
            updates._spawn_worker(self.cache)
        arguments, options = popen.call_args.args[0], popen.call_args.kwargs
        self.assertIn("--_worker", arguments)
        self.assertIn("--check", arguments)
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertTrue(options["close_fds"])
        if os.name != "nt":
            self.assertTrue(options["start_new_session"])

    def test_cli_missing_version_returns_json_success_and_no_spawn(self):
        with mock.patch.object(updates, "__file__", str(self.base / "scripts/check_update.py")), \
                mock.patch.object(updates.subprocess, "Popen") as popen, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(updates.main(["--background", "--cache-dir", str(self.cache)]), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
