#!/usr/bin/env python3
"""Platform-issued student code for the joint admissions officer page.

`link` prints the page to open: https://hi.xwoork.cn/s/<ID>#<PIN> once the platform has
issued a code, otherwise the plain /student page. `ensure` prints only the 8-character
ID (for start_intake's student_code). Failures never block the consultation.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request

ORIGIN = "https://hi.xwoork.cn"
ISSUE_PATH = "/api/assist/v1/student-codes"
ID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{8}")
PIN_RE = re.compile(r"[0-9]{6}")
RETRY_AFTER_FAILURE_S = 3600
MAX_RETRY_S = 24 * 3600


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def origin():
    value = os.environ.get("LIUXUE_PLATFORM_ORIGIN", "").rstrip("/")
    # Override exists for local tests only; production always uses the fixed HTTPS origin.
    if value and re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}", value):
        return value
    return ORIGIN


def home():
    override = os.environ.get("LIUXUE_HOME")
    if override:
        return Path(override)
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "liuxue"
    return Path.home() / ".liuxue"


def _read_private_json(path):
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                return None
            if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                return None
            value = json.loads(handle.read())
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".id-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load():
    value = _read_private_json(home() / "id")
    if value and ID_RE.fullmatch(str(value.get("id", ""))) and PIN_RE.fullmatch(str(value.get("pin", ""))):
        return value
    return None


def _blocked_until():
    value = _read_private_json(home() / "id.retry")
    try:
        return float(value.get("not_before", 0)) if value else 0.0
    except (TypeError, ValueError):
        return 0.0


def _remember_failure(seconds):
    try:
        _write_private_json(home() / "id.retry", {"not_before": time.time() + min(max(seconds, 60), MAX_RETRY_S)})
    except OSError:
        pass


def issue(timeout=5, opener=None):
    """Ask the platform for a code once. Returns the stored record or None."""
    if time.time() < _blocked_until():
        return None
    request = urllib.request.Request(origin() + ISSUE_PATH, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
    opener = opener or urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read(4096).decode("utf-8"))
    except urllib.error.HTTPError as error:
        wait = RETRY_AFTER_FAILURE_S
        if error.code == 429:
            try:
                wait = int(error.headers.get("Retry-After") or wait)
            except ValueError:
                pass
        _remember_failure(wait)
        return None
    except (OSError, ValueError, UnicodeError):
        _remember_failure(RETRY_AFTER_FAILURE_S)
        return None
    if not isinstance(payload, dict):
        payload = {}
    code, pin = str(payload.get("id", "")).upper(), str(payload.get("pin", ""))
    if not ID_RE.fullmatch(code) or not PIN_RE.fullmatch(pin):
        _remember_failure(RETRY_AFTER_FAILURE_S)
        return None
    record = {"id": code, "pin": pin, "issued_at": int(time.time())}
    try:
        _write_private_json(home() / "id", record)
    except OSError:
        return None
    return record


def ensure(**kwargs):
    return load() or issue(**kwargs)


def page_url(record, landing="consult"):
    if not record:
        return ORIGIN + ("/student/upload" if landing == "upload" else "/student")
    suffix = "/upload" if landing == "upload" else ""
    return f"{ORIGIN}/s/{record['id']}{suffix}#{record['pin']}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    link = sub.add_parser("link", help="print the joint admissions officer page to open")
    link.add_argument("--upload", action="store_true")
    sub.add_parser("ensure", help="print the student code (no PIN)")
    sub.add_parser("status", help="offline: is a code stored?")
    args = parser.parse_args(argv)
    if args.command == "status":
        record = load()
        print(json.dumps({"status": "ready" if record else "missing", "student_code": record["id"] if record else None}))
        return 0
    record = ensure()
    if args.command == "ensure":
        print(json.dumps({"status": "ready" if record else "unavailable", "student_code": record["id"] if record else None}))
    else:
        landing = "upload" if args.upload else "consult"
        print(json.dumps({"status": "ready" if record else "fallback", "url": page_url(record, landing)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
