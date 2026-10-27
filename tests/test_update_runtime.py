"""Runtime update safety checks, entirely offline and in temporary directories."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runtime_under_test", ROOT / "scripts/update_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def make_package(path, version="0.10.0-pilot.2", cards=None, caps=None, files=None):
    path.mkdir(parents=True, exist_ok=True)
    contents = {
        "VERSION": (version + "\n").encode(),
        "SKILL.md": b"---\nname: study-abroad-planner\n---\nUse the pinned runtime.\n",
        "UPDATE-POLICY.json": json.dumps({"schema_version": 1, "capabilities": caps or ["school-query", "knowledge-query"]}).encode(),
        "references/knowledge-cards.json": json.dumps({"cards": [{"id": "existing-card", "content": "Preserve this knowledge"}] if cards is None else cards}).encode(),
        "scripts/query.py": b"def query():\n    return 'old'\n",
    }
    contents.update(files or {})
    for name, value in contents.items():
        output = path / name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(value)
    rebuild_manifest(path, version)
    return path


def rebuild_manifest(path, version=None):
    version = version or (path / "VERSION").read_text().strip()
    manifest = {"version": version, "files": {}}
    for file in sorted(path.rglob("*")):
        if not file.is_file() or file.name == "PACKAGE-MANIFEST.json" or "__pycache__" in file.parts:
            continue
        data = file.read_bytes()
        manifest["files"][file.relative_to(path).as_posix()] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    (path / "PACKAGE-MANIFEST.json").write_text(json.dumps(manifest))


def contents(path):
    return {file.relative_to(path).as_posix(): file.read_bytes() for file in path.rglob("*") if file.is_file() and "__pycache__" not in file.parts}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name).resolve()
        self.base = make_package(self.temp / "skills" / "study-abroad-planner")
        self.state = runtime.state_directory(self.base, self.temp / "cache")
        self.candidate = make_package(self.temp / "candidate", "0.10.0-pilot.3")
        self.original = contents(self.base)

    def tearDown(self):
        self.temporary.cleanup()

    def initialize(self):
        answer = runtime.resolve_runtime(self.base, self.state)
        self.assertTrue(answer["updates_allowed"], answer)
        self.assertEqual(answer["version"], "0.10.0-pilot.2")
        self.assertNotEqual(answer["runtime_path"], str(self.base))
        return answer

    def assertPreserved(self, pointer=None):
        self.assertEqual(contents(self.base), self.original)
        if pointer is not None:
            self.assertEqual((self.state / "active.json").read_bytes(), pointer)

    def test_first_invocation_pins_verified_snapshot_without_modifying_install(self):
        answer = self.initialize()
        snapshot = Path(answer["runtime_path"])
        self.assertEqual(contents(snapshot), self.original)
        second = runtime.resolve_runtime(self.base, self.state)
        self.assertEqual(second, answer)
        self.assertPreserved()

    def test_update_switches_next_invocation_and_old_pinned_runtime_stays_readable(self):
        first = self.initialize()
        old = Path(first["runtime_path"])
        new = runtime.publish_candidate(self.candidate, self.state, self.base)
        self.assertEqual(new["status"], "updated")
        self.assertEqual(new["version"], "0.10.0-pilot.3")
        self.assertEqual(runtime.resolve_runtime(self.base, self.state)["runtime_path"], new["runtime_path"])
        self.assertEqual(contents(old), self.original)
        self.assertPreserved()
        repeated = runtime.publish_candidate(self.candidate, self.state, self.base)
        self.assertEqual(repeated["status"], "unchanged")

    def test_lower_stable_release_and_equal_repacked_version_do_not_replace_pilot(self):
        self.initialize()
        pointer = (self.state / "active.json").read_bytes()
        for version in ("0.9.0", "0.9.9", "0.10.0-pilot.1", "0.10.0-pilot.2"):
            with self.subTest(version=version):
                shutil.rmtree(self.candidate)
                make_package(self.candidate, version, files={"SKILL.md": b"Changed even for same version"})
                with self.assertRaisesRegex(runtime.RuntimeUpdateError, "version_not_newer"):
                    runtime.publish_candidate(self.candidate, self.state, self.base)
                self.assertPreserved(pointer)

    def test_stable_successor_is_newer_than_same_core_pilot(self):
        self.initialize()
        make_package(self.candidate, "0.10.0")
        self.assertEqual(runtime.publish_candidate(self.candidate, self.state, self.base)["version"], "0.10.0")

    def test_missing_or_changed_existing_knowledge_is_blocked_but_additions_allowed(self):
        self.initialize()
        pointer = (self.state / "active.json").read_bytes()
        for cards in ([], [{"id": "existing-card", "content": "overwritten knowledge"}]):
            with self.subTest(cards=cards):
                make_package(self.candidate, "0.10.1", cards=cards)
                with self.assertRaisesRegex(runtime.RuntimeUpdateError, "knowledge_would_change_or_be_removed"):
                    runtime.publish_candidate(self.candidate, self.state, self.base)
                self.assertPreserved(pointer)
        make_package(self.candidate, "0.10.1", cards=[{"id": "existing-card", "content": "Preserve this knowledge"}, {"id": "new-card", "content": "Additional knowledge"}])
        self.assertEqual(runtime.publish_candidate(self.candidate, self.state, self.base)["status"], "updated")

    def test_missing_capability_or_runtime_file_is_blocked(self):
        self.initialize()
        pointer = (self.state / "active.json").read_bytes()
        make_package(self.candidate, "0.10.1", caps=["school-query"])
        with self.assertRaisesRegex(runtime.RuntimeUpdateError, "capability_would_be_removed"):
            runtime.publish_candidate(self.candidate, self.state, self.base)
        make_package(self.candidate, "0.10.1")
        (self.candidate / "scripts/query.py").unlink()
        rebuild_manifest(self.candidate)
        with self.assertRaisesRegex(runtime.RuntimeUpdateError, "required_file_would_be_removed"):
            runtime.publish_candidate(self.candidate, self.state, self.base)
        self.assertPreserved(pointer)

    def test_policy_required_and_strict(self):
        self.initialize()
        for value in (None, {"schema_version": True, "capabilities": ["school-query"]}, {"schema_version": 1, "capabilities": ["same", "same"]}):
            make_package(self.candidate, "0.10.1")
            path = self.candidate / "UPDATE-POLICY.json"
            if value is None:
                path.unlink()
            else:
                path.write_text(json.dumps(value))
            rebuild_manifest(self.candidate)
            with self.assertRaises(runtime.RuntimeUpdateError):
                runtime.publish_candidate(self.candidate, self.state, self.base)
        self.assertPreserved()

    def test_modified_baseline_and_extra_files_stay_in_place_and_block_updates(self):
        self.initialize()
        for filename in ("SKILL.md", "personal-notes.txt"):
            with self.subTest(filename=filename):
                path = self.base / filename
                before = path.read_bytes() if path.exists() else None
                path.write_bytes(b"Keep my local modification")
                result = runtime.resolve_runtime(self.base, self.state)
                self.assertEqual(result["runtime_path"], str(self.base))
                self.assertFalse(result["updates_allowed"])
                with self.assertRaises(runtime.RuntimeUpdateError):
                    runtime.publish_candidate(self.candidate, self.state, self.base)
                self.assertEqual(path.read_bytes(), b"Keep my local modification")
                if before is None:
                    path.unlink()
                else:
                    path.write_bytes(before)

    def test_modified_selected_snapshot_is_preserved_and_not_hidden_by_fallback(self):
        first = self.initialize()
        path = Path(first["runtime_path"])
        (path / "my-local.txt").write_text("Keep me")
        result = runtime.resolve_runtime(self.base, self.state)
        self.assertEqual(result["runtime_path"], str(path))
        self.assertFalse(result["updates_allowed"])
        with self.assertRaises(runtime.RuntimeUpdateError):
            runtime.publish_candidate(self.candidate, self.state, self.base)
        self.assertEqual((path / "my-local.txt").read_text(), "Keep me")

    def test_manifest_changed_even_with_recomputed_local_hashes_blocks_updates(self):
        self.initialize()
        (self.base / "SKILL.md").write_text("My edited instructions")
        rebuild_manifest(self.base)
        result = runtime.resolve_runtime(self.base, self.state)
        self.assertEqual(result["error"], "baseline_manifest_changed")
        self.assertFalse(result["updates_allowed"])
        self.assertEqual(result["runtime_path"], str(self.base))

    def test_unusable_baseline_uses_existing_snapshot_without_repairing_user_files(self):
        first = self.initialize()
        (self.base / "SKILL.md").unlink()
        result = runtime.resolve_runtime(self.base, self.state)
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["runtime_path"], first["runtime_path"])
        self.assertFalse(result["updates_allowed"])
        self.assertFalse((self.base / "SKILL.md").exists())

    def test_corrupt_pointer_falls_back_without_rewriting_it(self):
        self.initialize()
        path = self.state / "active.json"
        path.write_text("broken pointer")
        result = runtime.resolve_runtime(self.base, self.state)
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["runtime_path"], str(self.base))
        self.assertFalse(result["updates_allowed"])
        self.assertEqual(path.read_text(), "broken pointer")
        self.assertPreserved()

    def test_missing_active_snapshot_falls_back_to_previous_verified_runtime(self):
        first = self.initialize()
        updated = runtime.publish_candidate(self.candidate, self.state, self.base)
        shutil.rmtree(updated["runtime_path"])
        result = runtime.resolve_runtime(self.base, self.state)
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["runtime_path"], first["runtime_path"])
        self.assertFalse(result["updates_allowed"])
        self.assertPreserved()

    def test_unusable_selected_runtime_falls_back_without_erasing_it(self):
        first = self.initialize()
        updated = runtime.publish_candidate(self.candidate, self.state, self.base)
        path = Path(updated["runtime_path"])
        for filename, new_bytes in (("SKILL.md", None), ("SKILL.md", b" "), ("VERSION", b"not a version"), ("scripts/query.py", b"def broken(")):
            with self.subTest(filename=filename, new_bytes=new_bytes):
                file = path / filename
                original = file.read_bytes()
                if new_bytes is None:
                    file.unlink()
                else:
                    file.write_bytes(new_bytes)
                result = runtime.resolve_runtime(self.base, self.state)
                self.assertEqual(result["status"], "fallback")
                self.assertEqual(result["runtime_path"], first["runtime_path"])
                self.assertFalse(result["updates_allowed"])
                self.assertTrue(path.exists())
                file.write_bytes(original)

    def test_changed_selected_manifest_identity_is_not_treated_as_content_edit(self):
        first = self.initialize()
        updated = runtime.publish_candidate(self.candidate, self.state, self.base)
        path = Path(updated["runtime_path"])
        (path / "SKILL.md").write_text("Self-consistent replacement with changed manifest")
        rebuild_manifest(path)
        result = runtime.resolve_runtime(self.base, self.state)
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["runtime_path"], first["runtime_path"])
        self.assertFalse(result["updates_allowed"])
        self.assertEqual((path / "SKILL.md").read_text(), "Self-consistent replacement with changed manifest")

    def test_edited_selected_version_cannot_override_pinned_version_identity(self):
        first = self.initialize()
        updated = runtime.publish_candidate(self.candidate, self.state, self.base)
        path = Path(updated["runtime_path"])
        original = (path / "VERSION").read_bytes()
        for version in ("0.9.0", "99.0.0"):
            with self.subTest(version=version):
                (path / "VERSION").write_text(version + "\n")
                result = runtime.resolve_runtime(self.base, self.state)
                self.assertEqual(result["status"], "fallback")
                self.assertEqual(result["runtime_path"], first["runtime_path"])
                self.assertEqual(result["version"], "0.10.0-pilot.2")
                self.assertFalse(result["updates_allowed"])
                self.assertEqual((path / "VERSION").read_text().strip(), version)
        (path / "VERSION").write_bytes(original)

    def test_consistent_selected_identity_below_baseline_still_cannot_downgrade(self):
        first = self.initialize()
        old_package = make_package(self.temp / "old", "0.9.0")
        manifest, raw, digest = runtime._package(old_package)
        old_snapshot = runtime._snapshot(old_package, self.state, manifest, raw, digest)
        pointer_path = self.state / "active.json"
        pointer = json.loads(pointer_path.read_text())
        pointer["previous"] = [pointer["active"]]
        pointer["active"] = digest
        pointer_path.write_text(json.dumps(pointer))
        result = runtime.resolve_runtime(self.base, self.state)
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["runtime_path"], first["runtime_path"])
        self.assertEqual(result["version"], "0.10.0-pilot.2")
        self.assertFalse(result["updates_allowed"])
        self.assertTrue(old_snapshot.exists())

    def test_symlink_selected_directory_is_never_returned(self):
        first = self.initialize()
        updated = runtime.publish_candidate(self.candidate, self.state, self.base)
        path = Path(updated["runtime_path"])
        saved = path.with_name("saved-runtime")
        path.rename(saved)
        try:
            path.symlink_to(saved, target_is_directory=True)
        except (OSError, NotImplementedError):
            saved.rename(path)
            self.skipTest("symlinks unavailable")
        result = runtime.resolve_runtime(self.base, self.state)
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["runtime_path"], first["runtime_path"])
        self.assertTrue(path.is_symlink())
        self.assertTrue(saved.is_dir())

    def test_pointer_from_other_installation_is_rejected(self):
        self.initialize()
        other = make_package(self.temp / "other-skill")
        self.assertNotEqual(runtime.state_directory(other, self.temp / "cache"), self.state)
        result = runtime.resolve_runtime(other, self.state)
        self.assertEqual(result["error"], "runtime_pointer_invalid")
        self.assertFalse(result["updates_allowed"])

    def test_pointer_write_failure_keeps_old_active_and_orphan_version_is_unused(self):
        first = self.initialize()
        pointer = (self.state / "active.json").read_bytes()
        with mock.patch.object(runtime, "_write_pointer", side_effect=OSError("disk write failed")):
            with self.assertRaises(runtime.RuntimeUpdateError):
                runtime.publish_candidate(self.candidate, self.state, self.base)
        self.assertPreserved(pointer)
        self.assertEqual(runtime.resolve_runtime(self.base, self.state)["runtime_path"], first["runtime_path"])
        self.assertEqual(len(list((self.state / "versions").glob("[0-9a-f]*"))), 2)

    def test_interrupted_preparation_is_ignored_and_next_update_succeeds(self):
        first = self.initialize()
        incomplete = self.state / "versions/.prepare-interrupted"
        incomplete.mkdir()
        (incomplete / "VERSION").write_text("99.0.0")
        self.assertEqual(runtime.resolve_runtime(self.base, self.state)["runtime_path"], first["runtime_path"])
        self.assertEqual(runtime.publish_candidate(self.candidate, self.state, self.base)["status"], "updated")
        self.assertEqual((incomplete / "VERSION").read_text(), "99.0.0")

    def test_modified_during_staging_cannot_be_lost(self):
        self.initialize()
        pointer = (self.state / "active.json").read_bytes()
        real = runtime._snapshot
        def change_base(*args, **kwargs):
            answer = real(*args, **kwargs)
            (self.base / "local-edit.txt").write_text("Created during update")
            return answer
        with mock.patch.object(runtime, "_snapshot", side_effect=change_base):
            with self.assertRaises(runtime.RuntimeUpdateError):
                runtime.publish_candidate(self.candidate, self.state, self.base)
        self.assertEqual((self.state / "active.json").read_bytes(), pointer)
        self.assertEqual((self.base / "local-edit.txt").read_text(), "Created during update")

    def test_compile_rejects_invalid_python_without_executing_candidate(self):
        self.initialize()
        make_package(self.candidate, "0.10.1", files={"scripts/query.py": b"raise RuntimeError('never execute')\n"})
        self.assertEqual(runtime.publish_candidate(self.candidate, self.state, self.base)["status"], "updated")
        make_package(self.candidate, "0.10.2", files={"scripts/query.py": b"def broken(\n"})
        pointer = (self.state / "active.json").read_bytes()
        with self.assertRaises(runtime.RuntimeUpdateError):
            runtime.publish_candidate(self.candidate, self.state, self.base)
        self.assertPreserved(pointer)

    def test_bad_checksum_and_unlisted_candidate_files_cannot_be_selected(self):
        self.initialize()
        pointer = (self.state / "active.json").read_bytes()
        (self.candidate / "SKILL.md").write_text("invalid bytes")
        with self.assertRaises(runtime.RuntimeUpdateError):
            runtime.publish_candidate(self.candidate, self.state, self.base)
        make_package(self.candidate, "0.10.1")
        (self.candidate / "extra.txt").write_text("Unlisted content")
        with self.assertRaises(runtime.RuntimeUpdateError):
            runtime.publish_candidate(self.candidate, self.state, self.base)
        self.assertPreserved(pointer)

    def test_lock_contention_returns_immediately_and_preserves_pointer(self):
        first = self.initialize()
        lock = runtime.RuntimeLock(self.state)
        self.assertTrue(lock.acquire())
        try:
            result = runtime.resolve_runtime(self.base, self.state)
            self.assertEqual(result["runtime_path"], first["runtime_path"])
            with self.assertRaisesRegex(runtime.RuntimeUpdateError, "runtime_busy"):
                runtime.publish_candidate(self.candidate, self.state, self.base)
        finally:
            lock.release()
        self.assertEqual(runtime.publish_candidate(self.candidate, self.state, self.base)["status"], "updated")

    def test_process_crash_releases_os_lock(self):
        self.initialize()
        program = "import importlib.util,os,sys;from pathlib import Path;s=importlib.util.spec_from_file_location('r',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);lock=m.RuntimeLock(Path(sys.argv[2]));assert lock.acquire();os._exit(0)"
        subprocess.run([sys.executable, "-B", "-c", program, str(ROOT / "scripts/update_runtime.py"), str(self.state)], check=True, timeout=10)
        self.assertEqual(runtime.publish_candidate(self.candidate, self.state, self.base)["status"], "updated")

    def test_later_lower_candidate_cannot_replace_a_newer_already_selected_version(self):
        self.initialize()
        make_package(self.candidate, "0.12.0")
        runtime.publish_candidate(self.candidate, self.state, self.base)
        make_package(self.candidate, "0.11.0")
        with self.assertRaisesRegex(runtime.RuntimeUpdateError, "version_not_newer"):
            runtime.publish_candidate(self.candidate, self.state, self.base)
        self.assertEqual(runtime.resolve_runtime(self.base, self.state)["version"], "0.12.0")

    def test_state_in_installation_and_symlink_escape_rejected(self):
        result = runtime.resolve_runtime(self.base, self.base / "state")
        self.assertEqual(result["error"], "runtime_state_unavailable")
        self.assertFalse((self.base / "state").exists())
        outside = self.temp / "outside"
        outside.mkdir()
        linked = self.temp / "linked"
        try:
            linked.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        result = runtime.resolve_runtime(self.base, linked)
        self.assertEqual(result["error"], "runtime_state_unavailable")
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
