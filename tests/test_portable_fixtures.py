"""Regression checks for temp fallback and symlink fixture failures."""
import errno
import os
from pathlib import Path
import tempfile as standard_tempfile
import unittest
from unittest import mock
import test_support


class PortableFixtureTests(unittest.TestCase):
    def test_test_temp_ignores_cached_cwd_and_never_probes_skill(self):
        root = test_support.ROOT
        before = sorted(str(item.relative_to(root)) for item in root.rglob("*") if "__pycache__" not in item.parts)
        with mock.patch.object(standard_tempfile, "tempdir", str(root)), \
             mock.patch.object(standard_tempfile, "gettempdir", side_effect=AssertionError("default lookup forbidden")), \
             mock.patch.dict(os.environ, {"STUDY_ABROAD_TEST_TMPDIR": str(root), "TMPDIR": str(root),
                                          "TEMP": str(root), "TMP": str(root)}):
            if os.name == "nt":
                # Keep the real LOCALAPPDATA/Temp candidate available.
                self.assertTrue(os.environ.get("LOCALAPPDATA"))
            with test_support.TemporaryDirectory() as directory:
                path = Path(directory).resolve()
                self.assertNotEqual(root, path)
                self.assertNotIn(root, path.parents)
                (path / "probe.txt").write_text("synthetic")
        self.assertEqual(before, sorted(str(item.relative_to(root)) for item in root.rglob("*") if "__pycache__" not in item.parts))

    def test_no_temp_location_fails_without_cwd_fallback(self):
        safe = test_support.safe_temp
        with mock.patch.object(safe.Path, "is_dir", return_value=True), \
             mock.patch.object(safe.tempfile, "TemporaryDirectory", side_effect=PermissionError("unavailable")) as create:
            with self.assertRaisesRegex(OSError, "No writable temporary directory"):
                test_support.TemporaryDirectory()
        self.assertGreater(create.call_count, 0)
        for call in create.call_args_list:
            self.assertTrue(Path(call.kwargs["dir"]).is_absolute())
            self.assertNotIn(test_support.ROOT, Path(call.kwargs["dir"]).parents)
            self.assertNotEqual(test_support.ROOT, Path(call.kwargs["dir"]))

    def test_symlink_permission_limit_is_reported_as_skip(self):
        with mock.patch.object(Path, "symlink_to", side_effect=PermissionError(errno.EPERM, "denied")):
            with self.assertRaises(unittest.SkipTest):
                test_support.symlink_or_skip(self, Path("target"), Path("link"))

    def test_unexpected_symlink_creation_failure_is_not_hidden(self):
        with mock.patch.object(Path, "symlink_to", side_effect=FileExistsError(errno.EEXIST, "fixture bug")):
            with self.assertRaises(FileExistsError):
                test_support.symlink_or_skip(self, Path("target"), Path("link"))
