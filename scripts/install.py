#!/usr/bin/env python3
"""Verify and install this Skill package without changing MCP configuration.

--destination is the final Skill directory, not its parent. The package manifest
contains {"version": "0.8.0", "files": {"VERSION": {"sha256": ..., "size": ...}}}.
The manifest itself is copied separately and must not list itself.
"""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import uuid


SKILL_NAME = "study-abroad-planner"
MANIFEST_NAME = "PACKAGE-MANIFEST.json"
MAX_FILES = 1024
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_TOTAL_SIZE = 100 * 1024 * 1024
MAX_MANIFEST_SIZE = 1024 * 1024
VERSION_RE = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    prefix + str(number) for prefix in ("COM", "LPT") for number in range(1, 10)
}


class InstallError(Exception):
    def __init__(self, message, backup=None):
        super().__init__(message)
        self.backup = backup


class WindowsDirectoryAccessError(InstallError):
    """A private directory could not be made usable by the actual token user."""


def _windows_acl_api():
    """Load only the native APIs needed to add a user ACE; no privilege changes."""
    import ctypes

    class Trustee(ctypes.Structure):
        _fields_ = [("multiple", ctypes.c_void_p), ("operation", ctypes.c_int),
                    ("form", ctypes.c_int), ("type", ctypes.c_int),
                    ("sid", ctypes.c_void_p)]

    class ExplicitAccess(ctypes.Structure):
        _fields_ = [("permissions", ctypes.c_uint32), ("mode", ctypes.c_int),
                    ("inheritance", ctypes.c_uint32), ("trustee", Trustee)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.c_void_p
    dword = ctypes.c_uint32
    signatures = (
        (kernel.GetCurrentProcess, [], pointer),
        (kernel.CloseHandle, [pointer], ctypes.c_int),
        (kernel.LocalFree, [pointer], pointer),
        (security.OpenProcessToken, [pointer, dword, ctypes.POINTER(pointer)], ctypes.c_int),
        (security.GetTokenInformation, [pointer, ctypes.c_int, pointer, dword,
                                        ctypes.POINTER(dword)], ctypes.c_int),
        (security.IsValidSid, [pointer], ctypes.c_int),
        (security.GetNamedSecurityInfoW, [ctypes.c_wchar_p, ctypes.c_int, dword,
             ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer),
             ctypes.POINTER(pointer), ctypes.POINTER(pointer)], dword),
        (security.SetEntriesInAclW, [dword, ctypes.POINTER(ExplicitAccess), pointer,
                                    ctypes.POINTER(pointer)], dword),
        (security.SetNamedSecurityInfoW, [ctypes.c_wchar_p, ctypes.c_int, dword,
                                         pointer, pointer, pointer, pointer], dword),
    )
    for function, arguments, result in signatures:
        function.argtypes = arguments
        function.restype = result
    return ctypes, kernel, security, ExplicitAccess


def _grant_windows_user_modify(path):
    """Add inheritable Modify for TokenUser to a newly created private directory.

    Python's Windows 0700 ACL can use OWNER RIGHTS while an elevated process's
    owner is Administrators. TokenUser is the same person's normal-login SID.
    Merge into the existing DACL: keep owner, SYSTEM, Administrators, denials and
    protection unchanged. Do not enable parent inheritance or grant WRITE_DAC /
    WRITE_OWNER. Call before creating children, never to repair an existing tree.
    """
    if os.name != "nt":
        return
    _no_symlinks(path)
    try:
        ctypes, kernel, security, ExplicitAccess = _windows_acl_api()
        token = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        merged = ctypes.c_void_p()
        try:
            if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
                raise OSError("OpenProcessToken failed")
            needed = ctypes.c_uint32()
            # TokenUser == 1. Never use TokenOwner (4) or a textual account name.
            security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            if not ctypes.sizeof(ctypes.c_void_p) <= needed.value <= 65536:
                raise OSError("Invalid TokenUser size")
            user = ctypes.create_string_buffer(needed.value)
            if not security.GetTokenInformation(token, 1, user, needed.value, ctypes.byref(needed)):
                raise OSError("GetTokenInformation(TokenUser) failed")
            sid = ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid or not security.IsValidSid(sid):
                raise OSError("Invalid TokenUser SID")
            old_acl = ctypes.c_void_p()
            error = security.GetNamedSecurityInfoW(str(path), 1, 4, None, None,
                                                  ctypes.byref(old_acl), None, ctypes.byref(descriptor))
            if error or not old_acl.value:
                raise OSError("Cannot read a restricted directory DACL: %s" % error)
            entry = ExplicitAccess()
            entry.permissions = 0x1301BF  # FILE_GENERIC_READ/WRITE/EXECUTE | DELETE (Modify).
            entry.mode = 1  # GRANT_ACCESS merges rather than replacing existing user rights.
            entry.inheritance = 3  # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE.
            entry.trustee.form = 0  # TRUSTEE_IS_SID.
            entry.trustee.type = 1  # TRUSTEE_IS_USER.
            entry.trustee.sid = sid
            error = security.SetEntriesInAclW(1, ctypes.byref(entry), old_acl, ctypes.byref(merged))
            if error or not merged.value:
                raise OSError("Cannot merge TokenUser ACE: %s" % error)
            # DACL only: no owner/group update, no unprotect/inheritance flag.
            error = security.SetNamedSecurityInfoW(str(path), 1, 4, None, None, merged, None)
            if error:
                raise OSError("Cannot apply TokenUser ACE: %s" % error)
        finally:
            if merged.value:
                kernel.LocalFree(merged)
            if descriptor.value:
                kernel.LocalFree(descriptor)
            if token.value:
                kernel.CloseHandle(token)
    except Exception as error:
        raise WindowsDirectoryAccessError("windows_user_acl_failed: " + str(path)) from error


def _private_directory(path):
    """Create a private leaf directory; leave existing permissions untouched."""
    _no_symlinks(path)
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        if not path.is_dir():
            raise
    else:
        try:
            _grant_windows_user_modify(path)
        except Exception:
            # Do not let a retry mistake an incompletely prepared leaf for an
            # existing usable directory. rmdir never removes somebody's files.
            try:
                path.rmdir()
            except OSError:
                pass
            raise
    _no_symlinks(path)


def _version(value):
    if not isinstance(value, str) or len(value) > 128:
        raise InstallError("Invalid semantic version")
    match = VERSION_RE.fullmatch(value)
    if not match or any(
        item.isdigit() and len(item) > 1 and item.startswith("0")
        for item in (match.group(4) or "").split(".")
    ):
        raise InstallError("Invalid semantic version")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InstallError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def _absolute(path):
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _link_like(path):
    """Reject symlinks and Windows reparse points, including junctions."""
    if path.is_symlink():
        return True
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _no_symlinks(path):
    """Check existing components without following a leaf link or junction."""
    for component in [path] + list(path.parents):
        if _link_like(component):
            raise InstallError("Symbolic links or reparse points are not allowed: " + str(component))


def _read_regular(path, limit):
    _no_symlinks(path)
    try:
        initial = path.lstat()
        if not stat.S_ISREG(initial.st_mode) or initial.st_size > limit:
            raise InstallError("Not a bounded regular file: " + str(path))
        flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
        descriptor = os.open(str(path), flags)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise InstallError("Not a bounded regular file: " + str(path))
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise InstallError("File exceeds size limit: " + str(path))
            return data
    except OSError as error:
        raise InstallError("Cannot read package file: " + str(path)) from error


def _manifest_path(name):
    if not isinstance(name, str) or not name or len(name) > 512:
        raise InstallError("Invalid manifest path")
    if any(char in name for char in '\\:<>"|?*') or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise InstallError("Unsafe manifest path: " + repr(name))
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name:
        raise InstallError("Manifest paths must be canonical relative paths")
    for part in name.split("/"):
        if part in ("", ".", "..") or part.endswith((" ", ".")):
            raise InstallError("Unsafe manifest path: " + name)
        if part.split(".")[0].upper() in RESERVED_NAMES:
            raise InstallError("Reserved manifest filename: " + name)
    if name.casefold() == MANIFEST_NAME.casefold():
        raise InstallError("The manifest must not list itself")
    if _python_cache(name):
        raise InstallError("Generated Python cache files must not be packaged")
    return name


def _python_cache(name):
    parts = PurePosixPath(name).parts
    return "__pycache__" in parts or name.endswith((".pyc", ".pyo"))


def read_manifest(source):
    raw = _read_regular(source / MANIFEST_NAME, MAX_MANIFEST_SIZE)
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise InstallError("Invalid package manifest JSON") from error
    if not isinstance(manifest, dict) or set(manifest) != {"version", "files"}:
        raise InstallError("Manifest must contain exactly version and files")
    _version(manifest["version"])
    files = manifest["files"]
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise InstallError("Invalid manifest file count")
    seen = set()
    total = 0
    for name, entry in files.items():
        _manifest_path(name)
        canonical = unicodedata.normalize("NFC", name).casefold()
        if canonical in seen:
            raise InstallError("Colliding manifest paths: " + name)
        seen.add(canonical)
        if not isinstance(entry, dict) or set(entry) != {"sha256", "size"}:
            raise InstallError("Invalid manifest file metadata: " + name)
        if not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise InstallError("Invalid SHA-256: " + name)
        size = entry["size"]
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_FILE_SIZE:
            raise InstallError("Invalid file size: " + name)
        total += size
        if total > MAX_TOTAL_SIZE:
            raise InstallError("Package exceeds total size limit")
    for name in seen:
        parts = name.split("/")
        if any("/".join(parts[:index]) in seen for index in range(1, len(parts))):
            raise InstallError("A manifest file is also used as a directory")
    if not {"VERSION", "SKILL.md"}.issubset(files):
        raise InstallError("VERSION and SKILL.md must be listed in the manifest")
    return manifest, raw


def _verified_bytes(source, name, entry):
    data = _read_regular(source / name, entry["size"])
    if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise InstallError("Size or SHA-256 mismatch: " + name)
    return data


def verify_package(source, manifest=None):
    source = _absolute(source)
    _no_symlinks(source)
    if not source.is_dir():
        raise InstallError("Package source is not a directory")
    if manifest is None:
        manifest, unused = read_manifest(source)
    for name, entry in manifest["files"].items():
        data = _verified_bytes(source, name, entry)
        if name == "VERSION":
            try:
                version = data.decode("ascii").strip()
            except UnicodeError as error:
                raise InstallError("VERSION must contain an ASCII semantic version") from error
            if _version(version) != manifest["version"]:
                raise InstallError("VERSION differs from manifest version")
        elif name == "SKILL.md" and not data.strip():
            raise InstallError("SKILL.md must not be empty")
    return manifest


def destination_for(host, destination=None):
    if destination is None:
        if host == "custom":
            raise InstallError("--host custom requires an explicit --destination")
        if host == "codex":
            base = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        elif host == "workbuddy":
            base = Path.home() / ".workbuddy"
        else:
            raise InstallError("Unknown host")
        destination = base / "skills" / SKILL_NAME
    target = _absolute(destination)
    if host not in ("codex", "workbuddy", "custom"):
        raise InstallError("Unknown host")
    if host != "custom" and target.name != SKILL_NAME:
        raise InstallError("The final destination directory must be named " + SKILL_NAME)
    return target


def _validate_target(source, target):
    _no_symlinks(target)
    if target == Path(target.anchor) or target == source or target in source.parents or source in target.parents:
        raise InstallError("Destination cannot be a filesystem root, source, source ancestor, or source descendant")
    if target.exists() and not target.is_dir():
        raise InstallError("Destination exists and is not a directory")


def _tree_files(target):
    found = set()
    for directory, directories, files in os.walk(str(target), followlinks=False):
        for name in directories + files:
            path = Path(directory) / name
            if _link_like(path):
                raise InstallError("Symbolic links or reparse points are not allowed in the destination: " + str(path))
        for name in files:
            path = Path(directory) / name
            if not path.is_file():
                raise InstallError("Destination contains a non-regular file")
            relative = path.relative_to(target).as_posix()
            if _python_cache(relative):
                continue
            found.add(relative)
            if len(found) > MAX_FILES + 1024:
                raise InstallError("Destination contains too many files for a Skill package")
    return found


def _identical(target, manifest, raw):
    if not target.exists():
        return False
    files = _tree_files(target)
    expected = set(manifest["files"]) | {MANIFEST_NAME}
    if files != expected:
        return False
    try:
        if _read_regular(target / MANIFEST_NAME, MAX_MANIFEST_SIZE) != raw:
            return False
        verify_package(target, manifest)
    except InstallError:
        return False
    return True


def backup_directory(source, target, create=False):
    """Keep old SKILL.md files outside the host's active skill-discovery root."""
    if target.parent.name == "skills":
        directory = target.parent.parent / "skill-backups"
    else:
        directory = target.parent / ".skill-backups"
    _no_symlinks(directory)
    if directory == source or source in directory.parents or directory == target or target in directory.parents:
        raise InstallError("Backup directory overlaps the source or destination")
    if directory.exists() and not directory.is_dir():
        raise InstallError("Backup path is not a directory")
    if create:
        _private_directory(directory)
        if directory.stat().st_dev != target.parent.stat().st_dev:
            raise InstallError("Backup and destination must be on the same filesystem for atomic rename")
        # Fail before moving the original package if the backup directory is unwritable.
        descriptor, probe = tempfile.mkstemp(prefix=".write-check-", dir=str(directory))
        os.close(descriptor)
        os.unlink(probe)
    return directory


def _require_expected_target(target, expected_manifest_sha256):
    """Cancel a guarded migration if local content changed while preparing it."""
    if expected_manifest_sha256 is None:
        return
    if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256):
        raise InstallError("Invalid expected installation digest")
    try:
        manifest, raw = read_manifest(target)
        if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256 or not _identical(target, manifest, raw):
            raise InstallError("Existing installation changed; migration cancelled")
    except (InstallError, OSError) as error:
        raise InstallError("Existing installation changed; migration cancelled") from error


def install_package(source, target, dry_run=False, expected_manifest_sha256=None):
    source, target = _absolute(source), _absolute(target)
    manifest, raw = read_manifest(source)
    verify_package(source, manifest)
    _validate_target(source, target)
    _require_expected_target(target, expected_manifest_sha256)
    result = {"version": manifest["version"], "target": str(target), "backup": None,
              "file_count": len(manifest["files"]) + 1, "dry_run": bool(dry_run)}
    if _identical(target, manifest, raw):
        return dict(result, status="unchanged")
    if target.exists():
        backup_directory(source, target)
    if dry_run:
        return dict(result, status="would_update" if target.exists() else "would_install")

    target.parent.mkdir(parents=True, exist_ok=True)
    _no_symlinks(target.parent)
    lock = target.parent / ("." + target.name + ".install.lock")
    try:
        descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise InstallError("Another install is active; lock: " + str(lock)) from error
    os.close(descriptor)
    stage = None
    backup = None
    try:
        _validate_target(source, target)
        _require_expected_target(target, expected_manifest_sha256)
        if _identical(target, manifest, raw):
            return dict(result, status="unchanged")
        existed = target.exists()
        original_identity = (target.stat().st_dev, target.stat().st_ino) if existed else None
        backup_root = backup_directory(source, target, create=True) if existed else None
        stage = Path(tempfile.mkdtemp(prefix="." + target.name + ".staging-", dir=str(target.parent)))
        _grant_windows_user_modify(stage)
        for name, entry in manifest["files"].items():
            data = _verified_bytes(source, name, entry)
            output = stage / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        (stage / MANIFEST_NAME).write_bytes(raw)
        verify_package(stage)
        if not _identical(stage, manifest, raw):
            raise InstallError("Staging verification failed")
        _validate_target(source, target)
        current_identity = (target.stat().st_dev, target.stat().st_ino) if target.exists() else None
        if current_identity != original_identity:
            raise InstallError("Destination changed during staging; installation cancelled")
        if target.exists():
            _tree_files(target)
            _require_expected_target(target, expected_manifest_sha256)
            _no_symlinks(backup_root)
            if backup_root.stat().st_dev != target.parent.stat().st_dev:
                raise InstallError("Backup filesystem changed during staging")
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = backup_root / (target.name + ".backup-" + stamp + "-" + uuid.uuid4().hex[:8])
            os.rename(str(target), str(backup))
        try:
            os.replace(str(stage), str(target))
            stage = None
        except OSError as error:
            if backup is not None:
                try:
                    if target.exists() or target.is_symlink():
                        raise OSError("Destination was recreated during replacement")
                    os.replace(str(backup), str(target))
                    backup = None
                except OSError as rollback_error:
                    raise InstallError("Replacement failed; original package remains at " + str(backup),
                                       backup=str(backup)) from rollback_error
            raise InstallError("Replacement failed; original package was restored" if existed
                               else "Replacement failed; no package was installed") from error
        return dict(result, status="updated" if existed else "installed",
                    backup=str(backup) if backup else None)
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(str(stage))
        lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=("codex", "workbuddy", "custom"), required=True)
    parser.add_argument("--destination", help="Final Skill directory; required for custom")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    target = None
    try:
        target = destination_for(args.host, args.destination)
        source = Path(__file__).absolute().parent.parent
        result = install_package(source, target, args.dry_run)
        code = 0
    except (InstallError, OSError) as error:
        result = {"status": "error", "error": str(error), "target": str(target) if target else None,
                  "backup": getattr(error, "backup", None), "dry_run": args.dry_run}
        code = 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
