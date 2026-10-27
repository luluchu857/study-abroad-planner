#!/usr/bin/env python3
"""Nonblocking Skill activation; immutable runtimes keep in-flight work intact.

Only the detached worker uses the network. The installed entry directory is not
replaced. Agents pin the returned runtime_path for the whole consultation.
"""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_update as versions
import update_feed as feed
import update_runtime as runtime

SUCCESS_INTERVAL = 6 * 60 * 60
FAILURE_INTERVAL = 5 * 60
MAX_FAILURE_INTERVAL = 60 * 60
STATE_LIMIT = 256 * 1024


def read_status(state):
    try:
        path = state / "network.json"
        versions._safe_cache_dir(state)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > STATE_LIMIT:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=versions._unique_object)
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            return {}
        for key in ("checked_at", "next_check_at"):
            number = value.get(key)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
                return {}
        if "index" in value:
            value["index"] = feed.validate_index(value["index"])
        if not isinstance(value.get("failure_count", 0), int) or not 0 <= value.get("failure_count", 0) <= 100:
            return {}
        if value.get("etag") is not None and (not isinstance(value["etag"], str) or len(value["etag"]) > 1024):
            return {}
        return value
    except (OSError, ValueError, TypeError, RecursionError, feed.FeedError):
        return {}


def save_status(state, value):
    versions._safe_cache_dir(state, create=True)
    path = state / "network.json"
    if path.is_symlink():
        raise OSError("Unsafe update state")
    descriptor, name = tempfile.mkstemp(prefix=".network-", dir=str(state))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def due(value, now):
    # A backwards clock must not suppress checks indefinitely.
    return not value or now < value["checked_at"] - 300 or now >= value["next_check_at"]


def channel_for(base):
    unused, prerelease = versions.parse_version((base / "VERSION").read_text(encoding="ascii").strip())
    return "pilot" if prerelease else "stable"


def choose_release(index, channel, current):
    candidates = [index["channels"].get("stable")]
    if channel == "pilot":
        candidates.append(index["channels"].get("pilot"))
    selected = None
    for entry in candidates:
        if entry and versions.newer_than(entry["version"], current):
            if selected is None or versions.newer_than(entry["version"], selected["version"]):
                selected = entry
    return selected


def worker(base, state, force=False, clock=time.time):
    state = runtime._state_path(base, state, create=True)
    lock = versions.CheckLock(state / "network-lock")
    if not lock.acquire():
        return {"status": "checking"}
    try:
        now = clock()
        old = read_status(state)
        # Force bypasses normal freshness, never server Retry-After/rate limits.
        if not due(old, now) and (not force or old.get("error")):
            return old
        selected = runtime.resolve_runtime(base, state)
        if not selected.get("updates_allowed", False):
            return {"status": "preserved", "error": selected.get("error")}
        result = dict(old, schema_version=1, checked_at=now,
                      next_check_at=now + SUCCESS_INTERVAL, failure_count=0,
                      status="up_to_date", error=None)
        try:
            response = feed.fetch_index(etag=old.get("etag") if "index" in old else None)
            if response["not_modified"]:
                if "index" not in old:
                    raise feed.FeedError("missing_cached_index")
                index = old["index"]
            else:
                index = feed.validate_index(response["index"])
            result.update(index=index, etag=response.get("etag"))
            entry = choose_release(index, channel_for(base), selected["version"])
            if entry:
                with tempfile.TemporaryDirectory(prefix=".download-", dir=str(state)) as directory:
                    package = feed.download_package(entry, Path(directory) / "package")
                    applied = runtime.publish_candidate(package, state, base)
                result.update(status="updated", installed_version=entry["version"],
                              runtime_path=applied.get("runtime_path"))
            result["effective_version"] = runtime.resolve_runtime(base, state)["version"]
        except Exception as error:
            count = min(old.get("failure_count", 0) + 1, 100)
            retry = now + min(FAILURE_INTERVAL * (2 ** min(count - 1, 8)), MAX_FAILURE_INTERVAL)
            server_retry = getattr(error, "retry_at", None)
            if isinstance(server_retry, (int, float)) and math.isfinite(server_retry):
                retry = max(retry, server_retry)
            # Public error codes only: never persist arbitrary HTTP bodies or credentials.
            code = getattr(error, "code", None)
            if not isinstance(code, str) or len(code) > 120:
                code = "update_failed"
            result.update(status="kept_current", error=code,
                          failure_count=count, next_check_at=retry)
        save_status(state, result)
        return result
    finally:
        lock.release()


def spawn_worker(base, state):
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker",
               "--base", str(base), "--state", str(state)]
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "close_fds": True, "cwd": str(state)}
    if os.name == "nt":
        options["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                    | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)


def activate(base, state=None, background=True, spawn=spawn_worker, clock=time.time):
    base = Path(os.path.abspath(str(base)))
    state = Path(state) if state is not None else runtime.state_directory(base)
    selected = runtime.resolve_runtime(base, state)
    value = read_status(state)
    result = dict(selected, entrypoint=str(base / "scripts/check_update.py"),
                  runtime_skill_path=str(Path(selected["runtime_path"]) / "SKILL.md"),
                  bootstrap_version=(base / "VERSION").read_text(encoding="ascii").strip(),
                  effective_version=selected["version"], update_state_directory=str(state),
                  update_status=value.get("status", "not_checked"),
                  update_error=value.get("error"), next_check_at=value.get("next_check_at"))
    if background and selected.get("updates_allowed") and due(value, clock()):
        try:
            versions._safe_cache_dir(state, create=True)
            spawn(base, state)
            result["update_status"] = "checking"
        except Exception:
            result.update(update_status="kept_current", update_error="background_start_failed")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--background", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--check", action="store_true", help="Run update worker in the foreground")
    mode.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--base", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--state", type=Path, help="Isolated state for testing or explicit administration")
    args = parser.parse_args(argv)
    try:
        state = args.state or runtime.state_directory(args.base)
        if args.worker or args.check:
            worker(args.base, state, force=args.check)
        result = activate(args.base, state, background=not (args.status or args.worker or args.check))
    except Exception:
        result = {"status": "kept_current", "error": "update_unavailable",
                  "runtime_path": str(args.base), "runtime_skill_path": str(args.base / "SKILL.md")}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
