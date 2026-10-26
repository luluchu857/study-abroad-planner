#!/usr/bin/env python3
"""Check the official release in the background; never download or replace Skill files.

No network is used by --status. --background reuses a six-hour cache and starts a
detached worker only when needed. --check performs an explicit foreground check.
All update failures produce JSON and exit successfully so the Skill can continue.
"""

import argparse
import errno
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


REPOSITORY = "luluchu857/study-abroad-planner"
API_URL = "https://api.github.com/repos/" + REPOSITORY + "/releases/latest"
RELEASE_PREFIX = "https://github.com/" + REPOSITORY + "/releases/tag/v"
CACHE_TTL = 6 * 60 * 60
NETWORK_TIMEOUT = 3
MAX_RESPONSE_SIZE = 64 * 1024
CACHE_NAME = "update.json"
LOCK_NAME = "update.lock"
VERSION_RE = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)


def parse_version(value):
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("Invalid semantic version")
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise ValueError("Invalid semantic version")
    pre = tuple(match.group(4).split(".")) if match.group(4) else ()
    if any(item.isdigit() and len(item) > 1 and item.startswith("0") for item in pre):
        raise ValueError("Invalid numeric prerelease identifier")
    return tuple(int(match.group(index)) for index in (1, 2, 3)), pre


def newer_than(latest, current):
    left, left_pre = parse_version(latest)
    right, right_pre = parse_version(current)
    if left != right:
        return left > right
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


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def default_cache_dir():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "study-abroad-planner"
    return Path.home() / ".cache" / "study-abroad-planner"


def _safe_cache_dir(directory, create=False):
    path = Path(os.path.abspath(os.path.expanduser(str(directory))))
    if path == Path(path.anchor):
        raise OSError("Cache cannot be a filesystem root")
    for component in [path] + list(path.parents):
        if component.is_symlink():
            raise OSError("Cache symbolic links are not allowed")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() and not path.is_dir():
        raise OSError("Cache path is not a directory")
    return path


class CheckLock:
    """An OS file lock; crashes release it automatically, without stale leases.

    The small lock file intentionally remains on disk. Existence alone never
    means a check is running. No waiting or polling occurs when it is held.
    """

    def __init__(self, directory):
        self.directory = directory
        self.stream = None

    def acquire(self):
        directory = _safe_cache_dir(self.directory, create=True)
        path = directory / LOCK_NAME
        if path.is_symlink():
            raise OSError("Lock symbolic links are not allowed")
        descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        stream = os.fdopen(descriptor, "r+b")
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            stream.close()
            raise OSError("Lock is not a regular file")
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


def _timestamp(value):
    try:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _validate_cache(value, now):
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1 or value.get("repository") != REPOSITORY):
        raise ValueError("Invalid cache schema")
    checked = value.get("checked_at")
    if not _timestamp(checked) or checked > now + 300:
        raise ValueError("Invalid cache timestamp")
    latest, url = value.get("latest_version"), value.get("release_url")
    if latest is not None:
        unused, prerelease = parse_version(latest)
        if prerelease:
            raise ValueError("Cached latest release is not stable")
        if url != RELEASE_PREFIX + latest:
            raise ValueError("Invalid cached release URL")
    elif url is not None:
        raise ValueError("Cached URL has no version")
    success = value.get("last_success_at")
    if success is not None and (not _timestamp(success) or success > checked):
        raise ValueError("Invalid successful-check timestamp")
    if value.get("error") not in (None, "network_error", "invalid_release", "check_failed"):
        raise ValueError("Invalid cache error")
    if latest is None and value.get("error") is None:
        raise ValueError("Empty successful cache")
    return value


def load_cache(directory, now):
    try:
        path = _safe_cache_dir(directory) / CACHE_NAME
        if path.is_symlink():
            return None
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            data = stream.read(MAX_RESPONSE_SIZE + 1)
        if len(data) > MAX_RESPONSE_SIZE:
            return None
        return _validate_cache(json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object), now)
    except (OSError, ValueError, UnicodeError, RecursionError, OverflowError):
        return None


def _save_cache(directory, value):
    directory = _safe_cache_dir(directory, create=True)
    path = directory / CACHE_NAME
    if path.is_symlink():
        raise OSError("Cache symbolic links are not allowed")
    descriptor, temporary = tempfile.mkstemp(prefix=".update-", suffix=".json", dir=str(directory))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fresh(cache, now):
    return cache is not None and 0 <= now - cache["checked_at"] < CACHE_TTL


def _result(cache, current, status, now, error=None):
    latest = cache.get("latest_version") if cache else None
    available = bool(latest and newer_than(latest, current))
    return {
        "status": status,
        "current_version": current,
        "latest_version": latest,
        "update_available": available,
        "release_url": cache.get("release_url") if available else None,
        "checked_at": cache.get("checked_at") if cache else None,
        "cache_age_seconds": max(0, now - cache["checked_at"]) if cache else None,
        "stale": not _fresh(cache, now),
        "error": error or (cache.get("error") if cache else None),
    }


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise urllib.error.HTTPError(request.full_url, code, "Release redirects are disabled", headers, response)


def fetch_release():
    request = urllib.request.Request(API_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "study-abroad-planner-update-check",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    opener = urllib.request.build_opener(_NoRedirects())
    with opener.open(request, timeout=NETWORK_TIMEOUT) as response:
        if response.geturl() != API_URL:
            raise ValueError("Unexpected API response URL")
        data = response.read(MAX_RESPONSE_SIZE + 1)
    if len(data) > MAX_RESPONSE_SIZE:
        raise ValueError("Release response exceeds size limit")
    return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)


def validate_release(value):
    if not isinstance(value, dict) or value.get("draft") is not False or value.get("prerelease") is not False:
        raise ValueError("Not a stable published release")
    tag = value.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError("Release tag must start with v")
    version = tag[1:]
    unused, prerelease = parse_version(version)
    if prerelease:
        raise ValueError("A prerelease version is not a stable release")
    if value.get("html_url") != RELEASE_PREFIX + version:
        raise ValueError("Release URL must belong to the official repository and tag")
    return version, value["html_url"]


def check_update(directory, current, clock=time.time, fetch=fetch_release, force=True):
    parse_version(current)
    now = clock()
    old = load_cache(directory, now)
    lock = CheckLock(directory)
    try:
        if not lock.acquire():
            return _result(old, current, "checking", now)
        # A different worker may have completed between parent launch and lock acquisition.
        old = load_cache(directory, now)
        if not force and _fresh(old, now):
            return _result(old, current, "cached", now)
        record = {"schema_version": 1, "repository": REPOSITORY, "checked_at": now,
                  "latest_version": old.get("latest_version") if old else None,
                  "release_url": old.get("release_url") if old else None,
                  "last_success_at": old.get("last_success_at") if old else None,
                  "error": None}
        try:
            version, url = validate_release(fetch())
            record.update(latest_version=version, release_url=url, last_success_at=now)
        except (ValueError, UnicodeError, TypeError, RecursionError, OverflowError):
            record["error"] = "invalid_release"
        except (OSError, urllib.error.URLError, TimeoutError):
            record["error"] = "network_error"
        except Exception:
            record["error"] = "check_failed"
        try:
            _save_cache(directory, record)
        except OSError:
            return _result(record, current, "error", now, "cache_write_error")
        return _result(record, current, "error" if record["error"] else "checked", now)
    except OSError:
        return _result(old, current, "error", now, "cache_unavailable")
    finally:
        lock.release()


def _spawn_worker(directory):
    command = [sys.executable, str(Path(__file__).resolve()), "--check", "--cache-dir", str(directory), "--_worker"]
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "close_fds": True, "cwd": str(directory)}
    if os.name == "nt":
        options["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                    | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)


def background_check(directory, current, clock=time.time, spawn=_spawn_worker):
    parse_version(current)
    now = clock()
    cache = load_cache(directory, now)
    if _fresh(cache, now):
        return _result(cache, current, "cached", now)
    lock = CheckLock(directory)
    try:
        if not lock.acquire():
            return _result(cache, current, "checking", now)
        # This probe never waits. The child acquires its own OS lock before HTTP.
        lock.release()
        spawn(_safe_cache_dir(directory))
        return _result(cache, current, "checking", now)
    except Exception:
        return _result(cache, current, "error", now, "background_start_failed")
    finally:
        lock.release()


def status(directory, current, clock=time.time):
    parse_version(current)
    now = clock()
    cache = load_cache(directory, now)
    return _result(cache, current, "cached" if cache else "unknown", now)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--background", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=default_cache_dir())
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    current = None
    try:
        version_file = Path(__file__).resolve().parent.parent / "VERSION"
        with version_file.open("r", encoding="ascii") as stream:
            current = stream.read(130).strip()
        parse_version(current)
        if args.status:
            result = status(args.cache_dir, current)
        elif args.check:
            result = check_update(args.cache_dir, current, force=not args._worker)
        else:
            result = background_check(args.cache_dir, current)
    except Exception:
        result = {"status": "error", "error": "update_check_unavailable", "current_version": current,
                  "update_available": False, "release_url": None}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
