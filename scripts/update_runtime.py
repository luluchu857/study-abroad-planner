#!/usr/bin/env python3
"""Versioned Skill runtimes. Never replace an installed or in-use package.

A successful update publishes a verified, separate directory and then atomically
changes active.json. An invocation pins runtime_path; existing directories are
never garbage-collected here. No downloaded program is executed for validation.
"""

import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "study_abroad_trusted_installer", Path(__file__).with_name("install.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = _load_installer()
POLICY_NAME = "UPDATE-POLICY.json"
POINTER_NAME = "active.json"
MAX_POINTER_SIZE = 64 * 1024
MAX_POLICY_SIZE = 64 * 1024
DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class RuntimeUpdateError(Exception):
    """An update was not committed; callers continue their pinned runtime."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _absolute(path):
    return installer._absolute(path)


def installation_key(base):
    return hashlib.sha256(os.path.normcase(str(_absolute(base))).encode("utf-8")).hexdigest()


def state_directory(base, cache_root=None):
    base = _absolute(base)
    if cache_root is None:
        if os.name == "nt":
            cache_root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        else:
            cache_root = Path.home() / ".cache"
        cache_root = cache_root / "study-abroad-planner"
    return _absolute(cache_root) / "runtimes" / installation_key(base)


def _state_path(base, state=None, create=False):
    base = _absolute(base)
    state = _absolute(state) if state is not None else state_directory(base)
    installer._no_symlinks(base)
    installer._no_symlinks(state)
    if state == Path(state.anchor) or state == base or base in state.parents or state in base.parents:
        raise RuntimeUpdateError("runtime_state_overlaps_installation")
    if create:
        installer._private_directory(state)
    if state.exists() and not state.is_dir():
        raise RuntimeUpdateError("runtime_state_not_directory")
    return state


class RuntimeLock:
    """Non-blocking OS lock. A process crash does not leave a stale lease."""

    def __init__(self, state):
        self.state = _absolute(state)
        self.stream = None

    def acquire(self):
        installer._no_symlinks(self.state)
        installer._private_directory(self.state)
        path = self.state / "runtime.lock"
        installer._no_symlinks(path)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(path), flags, 0o600)
        stream = os.fdopen(descriptor, "r+b")
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            stream.close()
            raise RuntimeUpdateError("runtime_lock_not_regular")
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            stream.close()
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise
        self.stream = stream
        return True

    def release(self):
        if self.stream is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.stream.seek(0)
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None


def _json_bytes(path, limit):
    raw = installer._read_regular(path, limit)
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=installer._unique_object), raw
    except (ValueError, UnicodeError, RecursionError) as error:
        raise RuntimeUpdateError("invalid_json") from error


def _version(path):
    try:
        return installer._version(installer._read_regular(path / "VERSION", 130).decode("ascii").strip())
    except (installer.InstallError, UnicodeError, OSError):
        return None


def _semver(value):
    installer._version(value)
    match = installer.VERSION_RE.fullmatch(value)
    return tuple(int(match.group(i)) for i in (1, 2, 3)), tuple((match.group(4) or "").split(".")) if match.group(4) else ()


def newer_than(left, right):
    left_core, left_pre = _semver(left)
    right_core, right_pre = _semver(right)
    if left_core != right_core:
        return left_core > right_core
    if not left_pre or not right_pre:
        return bool(right_pre) and not left_pre
    for one, two in zip(left_pre, right_pre):
        if one == two:
            continue
        if one.isdigit() and two.isdigit():
            return int(one) > int(two)
        if one.isdigit() != two.isdigit():
            return not one.isdigit()
        return one > two
    return len(left_pre) > len(right_pre)


def _package(path, check_python=False):
    manifest, raw = installer.read_manifest(path)
    installer.verify_package(path, manifest)
    if installer._tree_files(path) != set(manifest["files"]) | {installer.MANIFEST_NAME}:
        raise RuntimeUpdateError("local_unlisted_files")
    if check_python:
        for name, entry in manifest["files"].items():
            if name.endswith(".py"):
                # compile() parses only; it does not import or run the candidate.
                compile(installer._verified_bytes(path, name, entry), name, "exec", dont_inherit=True)
    return manifest, raw, hashlib.sha256(raw).hexdigest()


def _policy(path, manifest):
    if POLICY_NAME not in manifest["files"]:
        raise RuntimeUpdateError("compatibility_policy_missing")
    value, unused = _json_bytes(path / POLICY_NAME, MAX_POLICY_SIZE)
    if not isinstance(value, dict) or set(value) != {"schema_version", "capabilities"} or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise RuntimeUpdateError("compatibility_policy_invalid")
    caps = value.get("capabilities")
    if not isinstance(caps, list) or not caps or len(caps) > 128 or any(not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", item) for item in caps) or len(set(caps)) != len(caps):
        raise RuntimeUpdateError("compatibility_policy_invalid")
    return set(caps)


def _cards(path, manifest):
    name = "references/knowledge-cards.json"
    if name not in manifest["files"]:
        return {}
    value, unused = _json_bytes(path / name, installer.MAX_FILE_SIZE)
    cards = value.get("cards") if isinstance(value, dict) else None
    if not isinstance(cards, list):
        raise RuntimeUpdateError("knowledge_snapshot_invalid")
    result = {}
    for card in cards:
        if not isinstance(card, dict) or not isinstance(card.get("id"), str) or not card["id"] or card["id"] in result:
            raise RuntimeUpdateError("knowledge_snapshot_invalid")
        result[card["id"]] = card
    return result


def _compatible(old_path, old_manifest, source, new_manifest):
    if not _policy(old_path, old_manifest).issubset(_policy(source, new_manifest)):
        raise RuntimeUpdateError("capability_would_be_removed")
    # Conservatively preserve every shipped runtime/document path. Test files may
    # evolve independently. A migration can later explicitly replace this gate.
    required = {name for name in old_manifest["files"] if not name.startswith("tests/")}
    if not required.issubset(new_manifest["files"]):
        raise RuntimeUpdateError("required_file_would_be_removed")
    old_cards, new_cards = _cards(old_path, old_manifest), _cards(source, new_manifest)
    if any(key not in new_cards or new_cards[key] != card for key, card in old_cards.items()):
        raise RuntimeUpdateError("knowledge_would_change_or_be_removed")


def _read_pointer(state, base):
    pointer_path = state / POINTER_NAME
    if not pointer_path.exists() and not pointer_path.is_symlink():
        return None
    value, unused = _json_bytes(pointer_path, MAX_POINTER_SIZE)
    if not isinstance(value, dict) or set(value) != {"schema_version", "installation", "baseline_manifest", "active", "previous"}:
        raise RuntimeUpdateError("invalid_runtime_pointer")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["installation"] != installation_key(base):
        raise RuntimeUpdateError("invalid_runtime_pointer")
    if any(not isinstance(value[key], str) or not DIGEST_RE.fullmatch(value[key]) for key in ("baseline_manifest", "active")):
        raise RuntimeUpdateError("invalid_runtime_pointer")
    previous = value["previous"]
    if not isinstance(previous, list) or len(previous) > 128 or any(not isinstance(x, str) or not DIGEST_RE.fullmatch(x) for x in previous) or len(previous) != len(set(previous)) or value["active"] in previous:
        raise RuntimeUpdateError("invalid_runtime_pointer")
    return value


def _write_pointer(state, value):
    path = state / POINTER_NAME
    installer._no_symlinks(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".active-", suffix=".json", dir=str(state))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
        if os.name != "nt":
            # Best-effort directory flush. No failure is raised after the commit.
            try:
                descriptor = os.open(str(state), os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _snapshot(source, state, manifest, raw, digest):
    versions = state / "versions"
    installer._no_symlinks(versions)
    installer._private_directory(versions)
    target = versions / digest
    if target.exists() or target.is_symlink():
        saved, saved_raw, saved_digest = _package(target)
        if saved_raw != raw or saved_digest != digest:
            raise RuntimeUpdateError("snapshot_collision")
        return target
    stage = Path(tempfile.mkdtemp(prefix=".prepare-", dir=str(versions)))
    try:
        installer._grant_windows_user_modify(stage)
        for name, entry in manifest["files"].items():
            data = installer._verified_bytes(source, name, entry)
            output = stage / name
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        with (stage / installer.MANIFEST_NAME).open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        unused, staged_raw, staged_digest = _package(stage)
        if staged_raw != raw or staged_digest != digest:
            raise RuntimeUpdateError("snapshot_verification_failed")
        os.rename(str(stage), str(target))
        stage = None
        return target
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(str(stage))


def _answer(path, status, error=None, allowed=False, manifest=None):
    version = manifest["version"] if manifest else _version(path)
    return {"runtime_path": str(path), "version": version, "effective_version": version,
            "status": status, "error": error, "updates_allowed": allowed}


def _usable_local_runtime(path):
    """Distinguish editable content from an unusable or escaped runtime."""
    try:
        installer._no_symlinks(path)
        if not path.is_dir() or _version(path) is None:
            return False
        if not installer._read_regular(path / "SKILL.md", installer.MAX_FILE_SIZE).decode("utf-8").strip():
            return False
        manifest, unused = installer.read_manifest(path)
        installer._tree_files(path)  # Reject links and special files anywhere.
        for name in manifest["files"]:
            if name.startswith("tests/"):
                continue
            data = installer._read_regular(path / name, installer.MAX_FILE_SIZE)
            if name.endswith(".py"):
                compile(data, name, "exec", dont_inherit=True)
        return True
    except Exception:
        return False


def _snapshot_identity_matches(path, expected_digest, minimum_version):
    """Local content edits never authorize rewriting a selected version identity."""
    try:
        manifest, raw = installer.read_manifest(path)
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            return False
        data = installer._verified_bytes(path, "VERSION", manifest["files"]["VERSION"])
        actual_version = installer._version(data.decode("ascii").strip())
        return actual_version == manifest["version"] and not newer_than(minimum_version, actual_version)
    except Exception:
        return False


def _resolve(base, state, initialize=False):
    try:
        baseline, base_raw, base_digest = _package(base)
        _policy(base, baseline)
    except Exception:
        # Readable local edits remain in use. If the bootstrap package itself is
        # unusable, a previously verified snapshot is the safe consultation path.
        if _usable_local_runtime(base):
            return _answer(base, "local_modified", "baseline_modified_or_invalid")
        try:
            old_pointer = _read_pointer(state, base)
            if old_pointer:
                for saved in [old_pointer["active"]] + old_pointer["previous"]:
                    path = state / "versions" / saved
                    try:
                        manifest, unused, digest = _package(path)
                        if digest != saved:
                            continue
                        _policy(path, manifest)
                        return _answer(path, "fallback", "baseline_runtime_unavailable", manifest=manifest)
                    except Exception:
                        continue
        except Exception:
            pass
        return _answer(base, "unavailable", "baseline_runtime_unavailable")
    try:
        pointer = _read_pointer(state, base)
    except Exception:
        return _answer(base, "fallback", "runtime_pointer_invalid", manifest=baseline)
    if pointer is None:
        if not initialize:
            return _answer(base, "base", allowed=False, manifest=baseline)
        target = _snapshot(base, state, baseline, base_raw, base_digest)
        pointer = {"schema_version": 1, "installation": installation_key(base),
                   "baseline_manifest": base_digest, "active": base_digest, "previous": []}
        _write_pointer(state, pointer)
        return _answer(target, "ready", allowed=True, manifest=baseline)
    if pointer["baseline_manifest"] != base_digest:
        return _answer(base, "local_modified", "baseline_manifest_changed", manifest=baseline)
    active = state / "versions" / pointer["active"]
    if active.exists() or active.is_symlink():
        try:
            manifest, unused, digest = _package(active)
            if digest != pointer["active"]:
                raise RuntimeUpdateError("snapshot_digest_changed")
            _policy(active, manifest)
            if newer_than(baseline["version"], manifest["version"]):
                raise RuntimeUpdateError("active_version_below_baseline")
            return _answer(active, "ready", allowed=True, manifest=manifest)
        except Exception:
            # Keep readable user edits selected. Missing entry files, broken Python,
            # or links are retained on disk but must not become an execution path.
            if (_snapshot_identity_matches(active, pointer["active"], baseline["version"])
                    and _usable_local_runtime(active)):
                return _answer(active, "local_modified", "selected_runtime_modified")
    for previous in pointer["previous"]:
        path = state / "versions" / previous
        try:
            manifest, unused, digest = _package(path)
            if digest != previous or newer_than(baseline["version"], manifest["version"]):
                continue
            _policy(path, manifest)
            return _answer(path, "fallback", "active_runtime_unavailable", manifest=manifest)
        except Exception:
            continue
    return _answer(base, "fallback", "active_runtime_unavailable", manifest=baseline)


def resolve_runtime(base, state=None):
    """Pin one runtime for an invocation. Never perform any network work."""
    base = _absolute(base)
    try:
        state = _state_path(base, state, create=True)
        lock = RuntimeLock(state)
        if not lock.acquire():
            # Reading old/new atomic pointers is safe while another process stages.
            return _resolve(base, state, initialize=False)
        try:
            return _resolve(base, state, initialize=True)
        finally:
            lock.release()
    except installer.WindowsDirectoryAccessError:
        return _answer(base, "fallback", "windows_user_acl_failed")
    except Exception:
        return _answer(base, "fallback", "runtime_state_unavailable")


def publish_candidate(source, state, base):
    """Stage and atomically select a strictly newer compatible package.

    This is the only mutating update entry point. It never moves or writes base,
    a selected version directory, credentials, or another installation's state.
    """
    source, base = _absolute(source), _absolute(base)
    state = _state_path(base, state, create=True)
    if source == state or source in state.parents:
        raise RuntimeUpdateError("candidate_overlaps_state")
    lock = RuntimeLock(state)
    if not lock.acquire():
        raise RuntimeUpdateError("runtime_busy")
    try:
        current = _resolve(base, state, initialize=True)
        if not current["updates_allowed"]:
            raise RuntimeUpdateError(current["error"] or "runtime_not_ready")
        current_path = Path(current["runtime_path"])
        old_manifest, old_raw, old_digest = _package(current_path)
        base_manifest, base_raw, base_digest = _package(base)
        candidate, raw, digest = _package(source, check_python=True)
        if not newer_than(candidate["version"], old_manifest["version"]):
            if candidate["version"] == old_manifest["version"] and raw == old_raw:
                return dict(current, status="unchanged")
            raise RuntimeUpdateError("version_not_newer")
        _compatible(current_path, old_manifest, source, candidate)
        _compatible(base, base_manifest, source, candidate)
        target = _snapshot(source, state, candidate, raw, digest)
        # Detect modification of baseline/selected package during preparation.
        if _package(base)[1] != base_raw or _package(current_path)[1] != old_raw:
            raise RuntimeUpdateError("local_package_changed_during_update")
        pointer = _read_pointer(state, base)
        if not pointer or pointer["active"] != old_digest or pointer["baseline_manifest"] != base_digest:
            raise RuntimeUpdateError("runtime_pointer_changed")
        previous = [old_digest] + [item for item in pointer["previous"] if item not in (old_digest, digest)]
        updated = dict(pointer, active=digest, previous=previous[:128])
        _write_pointer(state, updated)
        return _answer(target, "updated", allowed=True, manifest=candidate)
    except installer.WindowsDirectoryAccessError as error:
        raise RuntimeUpdateError("windows_user_acl_failed") from error
    except RuntimeUpdateError:
        raise
    except (installer.InstallError, OSError, ValueError, SyntaxError, UnicodeError) as error:
        raise RuntimeUpdateError("candidate_verification_failed") from error
    finally:
        lock.release()
