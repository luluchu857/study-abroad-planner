"""Private-directory ACLs: actual token user, atomic failures and POSIX parity."""

import ctypes
import importlib.util
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest import mock

from test_install_update import install, make_package as make_install_package
from test_update_runtime import runtime, make_package
import test_support as tempfile


class ApiFunction:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class WindowsApiFixture:
    """A split token: the logon user and the elevated owner are different SIDs."""
    user_sid = 0x112233
    owner_sid = 0x445566

    def __init__(self, fail=None, null_acl=False):
        self.fail = fail
        self.null_acl = null_acl
        self.token_queries = []
        self.grants = []
        self.writes = []
        self.freed = []
        self.closed = []
        self.kernel = SimpleNamespace(
            GetCurrentProcess=ApiFunction(lambda: 0x1000),
            CloseHandle=ApiFunction(lambda handle: self.closed.append(handle.value) or 1),
            LocalFree=ApiFunction(lambda pointer: self.freed.append(pointer.value)),
        )
        self.security = SimpleNamespace(
            OpenProcessToken=ApiFunction(self.open_token),
            GetTokenInformation=ApiFunction(self.token_information),
            IsValidSid=ApiFunction(lambda sid: sid in (self.user_sid, self.owner_sid)),
            GetNamedSecurityInfoW=ApiFunction(self.read_acl),
            SetEntriesInAclW=ApiFunction(self.merge_acl),
            SetNamedSecurityInfoW=ApiFunction(self.write_acl),
        )

    def load(self, name, **kwargs):
        return self.kernel if name == "kernel32" else self.security

    def open_token(self, process, access, output):
        if self.fail == "token_open":
            return 0
        assert access == 8, "Only TOKEN_QUERY may be requested"
        output._obj.value = 0x2000
        return 1

    def token_information(self, token, information_class, buffer, size, needed):
        self.token_queries.append(information_class)
        needed._obj.value = 32
        if buffer is None or self.fail == "token_read":
            return 0
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0] = (
            self.user_sid if information_class == 1 else self.owner_sid)
        return 1

    def read_acl(self, path, kind, flags, owner, group, dacl, sacl, descriptor):
        descriptor._obj.value = 0x6000
        dacl._obj.value = None if self.null_acl else 0x5000
        return 5 if self.fail == "acl_read" else 0

    def merge_acl(self, count, entry, old_acl, output):
        entry = entry._obj
        self.grants.append({
            "count": count, "sid": entry.trustee.sid, "form": entry.trustee.form,
            "type": entry.trustee.type, "mode": entry.mode,
            "permissions": entry.permissions, "inheritance": entry.inheritance,
            "old_acl": old_acl.value,
        })
        if self.fail == "acl_merge":
            return 5
        output._obj.value = 0x7000
        return 0

    def write_acl(self, path, kind, flags, owner, group, dacl, sacl):
        self.writes.append((kind, flags, owner, group, dacl.value, sacl))
        return 5 if self.fail == "acl_write" else 0


class WindowsDirectoryAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def grant(self, fixture):
        # Replace the module's os reference, not the process-wide os.name;
        # pathlib must continue using the actual platform in this portable test.
        with mock.patch.object(install, "os", SimpleNamespace(name="nt")), \
                mock.patch.object(ctypes, "WinDLL", fixture.load, create=True):
            install._grant_windows_user_modify(self.temp)

    def test_elevated_owner_is_not_used_and_original_acl_is_merged(self):
        fixture = WindowsApiFixture()
        self.grant(fixture)
        self.assertEqual(fixture.token_queries, [1, 1])
        self.assertEqual(fixture.grants, [{
            "count": 1, "sid": fixture.user_sid, "form": 0, "type": 1,
            "mode": 1, "permissions": 0x1301BF, "inheritance": 3, "old_acl": 0x5000,
        }])
        self.assertNotEqual(fixture.grants[0]["sid"], fixture.owner_sid)
        self.assertFalse(fixture.grants[0]["permissions"] & (0x40000 | 0x80000))
        self.assertEqual(fixture.writes, [(1, 4, None, None, 0x7000, None)])
        self.assertEqual(fixture.freed, [0x7000, 0x6000])
        self.assertEqual(fixture.closed, [0x2000])

    def test_native_permission_failures_propagate_without_alternate_authorization(self):
        for failure in ("token_open", "token_read", "acl_read", "acl_merge", "acl_write"):
            with self.subTest(failure=failure):
                fixture = WindowsApiFixture(fail=failure)
                with self.assertRaisesRegex(install.WindowsDirectoryAccessError, "windows_user_acl_failed"):
                    self.grant(fixture)
                self.assertLessEqual(len(fixture.grants), 1)
                self.assertLessEqual(len(fixture.writes), 1)
                if failure != "token_open":
                    self.assertEqual(fixture.closed, [0x2000])

    def test_unrestricted_null_dacl_is_rejected_without_replacing_it(self):
        fixture = WindowsApiFixture(null_acl=True)
        with self.assertRaises(install.WindowsDirectoryAccessError):
            self.grant(fixture)
        self.assertEqual(fixture.grants, [])
        self.assertEqual(fixture.writes, [])
        self.assertEqual(fixture.freed, [0x6000])

    @unittest.skipIf(os.name == "nt", "POSIX mode assertion")
    def test_posix_keeps_0700_and_never_loads_windows_apis(self):
        path = self.temp / "private"
        with mock.patch.object(install, "_windows_acl_api", side_effect=AssertionError("No Windows calls")):
            install._private_directory(path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_existing_directory_acl_is_never_rewritten(self):
        path = self.temp / "existing"
        path.mkdir()
        with mock.patch.object(install, "_grant_windows_user_modify") as grant:
            install._private_directory(path)
        grant.assert_not_called()

    def test_new_leaf_failure_is_removed_so_retry_cannot_bypass_acl_setup(self):
        path = self.temp / "private"
        with mock.patch.object(install, "_grant_windows_user_modify",
                               side_effect=install.WindowsDirectoryAccessError("windows_user_acl_failed")) as grant:
            for _ in range(2):
                with self.assertRaises(install.WindowsDirectoryAccessError):
                    install._private_directory(path)
                self.assertFalse(path.exists())
            self.assertEqual(grant.call_count, 2)

    def load_cache_module(self):
        spec = importlib.util.spec_from_file_location(
            "acl_test_check_update", Path(install.__file__).with_name("check_update.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_windows_network_lock_directory_uses_token_user_setup(self):
        cache = self.load_cache_module()
        path = self.temp / "network-lock"
        with mock.patch.object(cache, "os", SimpleNamespace(name="nt", path=os.path)), \
                mock.patch.object(cache, "_directory_installer", return_value=install), \
                mock.patch.object(install, "_grant_windows_user_modify") as grant:
            cache._safe_cache_dir(path, create=True)
            cache._safe_cache_dir(path, create=True)
        grant.assert_called_once_with(path)

    def test_windows_network_lock_acl_failure_leaves_no_lock_file(self):
        cache = self.load_cache_module()
        path = self.temp / "network-lock"
        with mock.patch.object(cache, "os", SimpleNamespace(name="nt", path=os.path)), \
                mock.patch.object(cache, "_directory_installer", return_value=install), \
                mock.patch.object(install, "_grant_windows_user_modify",
                    side_effect=install.WindowsDirectoryAccessError("windows_user_acl_failed")):
            with self.assertRaisesRegex(OSError, "windows_user_acl_failed"):
                cache.CheckLock(path).acquire()
        self.assertFalse(path.exists())

    def test_install_grants_stage_before_copy_and_preserves_original_on_failure(self):
        source = self.temp / "source"
        make_install_package(source)
        target = self.temp / "skills" / "study-abroad-planner"
        install.install_package(source, target)
        original = (target / "SKILL.md").read_bytes()
        make_install_package(source, version="0.8.1")
        seen = []

        def fail_staging(path):
            seen.append(path)
            self.assertEqual(list(path.iterdir()), [])
            if ".staging-" in path.name:
                raise install.WindowsDirectoryAccessError("windows_user_acl_failed")

        with mock.patch.object(install, "_grant_windows_user_modify", side_effect=fail_staging):
            with self.assertRaises(install.WindowsDirectoryAccessError):
                install.install_package(source, target)
        self.assertTrue(any(".staging-" in path.name for path in seen))
        self.assertEqual((target / "SKILL.md").read_bytes(), original)
        self.assertEqual((target / "VERSION").read_text().strip(), "0.8.0")
        self.assertFalse(list(target.parent.glob(".*.staging-*")))
        self.assertFalse(list(target.parent.glob("*.lock")))

    def test_runtime_prepares_state_versions_and_empty_snapshot_before_pointer(self):
        base = make_package(self.temp / "installed")
        state = self.temp / "state"
        granted = []

        def grant(path):
            self.assertEqual(list(path.iterdir()), [])
            self.assertFalse((state / "active.json").exists())
            granted.append(path)

        with mock.patch.object(runtime.installer, "_grant_windows_user_modify", side_effect=grant):
            result = runtime.resolve_runtime(base, state)
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(granted[:2], [state, state / "versions"])
        self.assertTrue(granted[2].name.startswith(".prepare-"))
        self.assertEqual(len(granted), 3)
        self.assertTrue((Path(result["runtime_path"]) / "SKILL.md").is_file())

    def test_runtime_acl_failure_is_explicit_and_never_publishes_pointer(self):
        base = make_package(self.temp / "installed")
        state = self.temp / "state"
        with mock.patch.object(runtime.installer, "_grant_windows_user_modify",
                               side_effect=runtime.installer.WindowsDirectoryAccessError("windows_user_acl_failed")):
            result = runtime.resolve_runtime(base, state)
        self.assertEqual(result["status"], "fallback")
        self.assertEqual(result["error"], "windows_user_acl_failed")
        self.assertFalse(result["updates_allowed"])
        self.assertFalse((state / "active.json").exists())

    def test_snapshot_acl_failure_preserves_existing_pointer_and_snapshot(self):
        base = make_package(self.temp / "installed")
        state = self.temp / "state"
        first = runtime.resolve_runtime(base, state)
        pointer = (state / "active.json").read_bytes()
        candidate = make_package(self.temp / "candidate", version="0.10.0-pilot.3")
        with mock.patch.object(runtime.installer, "_grant_windows_user_modify",
                               side_effect=runtime.installer.WindowsDirectoryAccessError("windows_user_acl_failed")):
            with self.assertRaisesRegex(runtime.RuntimeUpdateError, "windows_user_acl_failed"):
                runtime.publish_candidate(candidate, state, base)
        self.assertEqual((state / "active.json").read_bytes(), pointer)
        self.assertTrue((Path(first["runtime_path"]) / "SKILL.md").is_file())
        self.assertFalse(list((state / "versions").glob(".prepare-*")))


if __name__ == "__main__":
    unittest.main()
