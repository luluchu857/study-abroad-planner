#!/usr/bin/env python3
"""Read the official static update index and verify an immutable source package.

No GitHub API, account token, external mirror, or downloaded Python is used.
The caller owns scheduling and selecting a newer compatible release. This
module never writes into an installed Skill or changes the active-version file.
"""

import datetime
import email.utils
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile


REPOSITORY = "luluchu857/study-abroad-planner"
INDEX_URL = "https://raw.githubusercontent.com/" + REPOSITORY + "/main/updates.json"
ARCHIVE_PREFIX = "https://codeload.github.com/" + REPOSITORY + "/zip/"
MAX_INDEX_SIZE = 64 * 1024
MAX_ARCHIVE_SIZE = 20 * 1024 * 1024
MAX_EXTRACTED_SIZE = 100 * 1024 * 1024
MAX_ARCHIVE_FILES = 4096
NETWORK_TIMEOUT = 3
TOTAL_TIMEOUT = 30


def _trusted_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = _trusted_module("skill_feed_trusted_installer", "install.py")


class FeedError(Exception):
    """A sanitized failure suitable for the updater's local status record."""

    def __init__(self, code, retry_at=None, status=None):
        super().__init__(code)
        self.code = code
        self.retry_at = retry_at
        self.status = status


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def validate_entry(value, channel=None):
    required = {"version", "commit", "manifest_sha256"}
    optional = {"archive_sha256", "archive_size"}
    if not isinstance(value, dict) or not required.issubset(value) or set(value) - required - optional:
        raise FeedError("invalid_index")
    try:
        install._version(value["version"])
    except install.InstallError as error:
        raise FeedError("invalid_index") from error
    match = install.VERSION_RE.fullmatch(value["version"])
    prerelease = match.group(4)
    if channel == "stable" and prerelease:
        raise FeedError("invalid_index")
    if channel == "pilot" and (not prerelease or not prerelease.startswith("pilot.")):
        raise FeedError("invalid_index")
    if not isinstance(value["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", value["commit"]):
        raise FeedError("invalid_index")
    if not isinstance(value["manifest_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["manifest_sha256"]):
        raise FeedError("invalid_index")
    if "archive_sha256" in value and (not isinstance(value["archive_sha256"], str)
                                      or not re.fullmatch(r"[0-9a-f]{64}", value["archive_sha256"])):
        raise FeedError("invalid_index")
    if "archive_size" in value and (type(value["archive_size"]) is not int
                                    or not 1 <= value["archive_size"] <= MAX_ARCHIVE_SIZE):
        raise FeedError("invalid_index")
    return dict(value)


def validate_index(value):
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "repository", "channels"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["repository"] != REPOSITORY
            or not isinstance(value["channels"], dict)
            or set(value["channels"]) != {"stable", "pilot"}):
        raise FeedError("invalid_index")
    channels = {name: validate_entry(entry, name) if entry is not None else None
                for name, entry in value["channels"].items()}
    return {"schema_version": 1, "repository": REPOSITORY, "channels": channels}


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise FeedError("unexpected_redirect", status=code)


def _retry_at(headers, now):
    """Respect both standard Retry-After and GitHub's UTC reset timestamp."""
    values = []
    raw = headers.get("Retry-After") if headers is not None else None
    if isinstance(raw, str):
        try:
            seconds = float(raw)
            if math.isfinite(seconds) and seconds >= 0:
                values.append(now + seconds)
        except (ValueError, OverflowError):
            try:
                stamp = email.utils.parsedate_to_datetime(raw)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=datetime.timezone.utc)
                values.append(stamp.timestamp())
            except (TypeError, ValueError, OverflowError):
                pass
    raw = headers.get("X-RateLimit-Reset") if headers is not None else None
    if isinstance(raw, str):
        try:
            reset = float(raw)
            if math.isfinite(reset) and reset >= 0:
                values.append(reset)
        except (ValueError, OverflowError):
            pass
    # Without a usable header, defer rather than immediately hammering the host.
    return max([now + 60] + values)


def _bounded_body(response, limit, deadline):
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            size = int(declared)
        except (ValueError, TypeError) as error:
            raise FeedError("invalid_response") from error
        if size < 0 or size > limit:
            raise FeedError("response_too_large")
    chunks = []
    length = 0
    # HTTPResponse.read1 makes at most one underlying read per iteration. The
    # socket timeout bounds that read; the deadline is checked between reads.
    reader = getattr(response, "read1", response.read)
    while True:
        if time.monotonic() >= deadline:
            raise FeedError("download_timeout")
        chunk = reader(min(64 * 1024, limit - length + 1))
        if time.monotonic() >= deadline:
            raise FeedError("download_timeout")
        if not chunk:
            break
        length += len(chunk)
        if length > limit:
            raise FeedError("response_too_large")
        chunks.append(chunk)
    if declared is not None and length != int(declared):
        raise FeedError("incomplete_response")
    return b"".join(chunks)


def _request(url, limit, timeout, total_timeout, etag=None, now=None):
    if (url != INDEX_URL and not re.fullmatch(re.escape(ARCHIVE_PREFIX) + r"[0-9a-f]{40}", url)):
        raise FeedError("untrusted_url")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
            or not 0 < timeout <= 30
            or isinstance(total_timeout, bool) or not isinstance(total_timeout, (int, float))
            or not math.isfinite(total_timeout) or not 0 < total_timeout <= 120):
        raise FeedError("invalid_timeout")
    headers = {"User-Agent": "study-abroad-planner-updater", "Accept-Encoding": "identity"}
    if etag is not None:
        if not isinstance(etag, str) or len(etag) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in etag):
            raise FeedError("invalid_etag")
        headers["If-None-Match"] = etag
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_NoRedirects())
    deadline = time.monotonic() + total_timeout
    try:
        with opener.open(request, timeout=min(timeout, total_timeout)) as response:
            if response.geturl() != url:
                raise FeedError("unexpected_redirect")
            if getattr(response, "status", 200) != 200:
                raise FeedError("invalid_response", status=getattr(response, "status", None))
            result_etag = response.headers.get("ETag")
            if result_etag is not None and (len(result_etag) > 512
                                           or any(ord(char) < 32 or ord(char) == 127 for char in result_etag)):
                result_etag = None
            return _bounded_body(response, limit, deadline), result_etag
    except urllib.error.HTTPError as error:
        try:
            if error.code == 304 and etag is not None and url == INDEX_URL:
                return None, etag
            if error.code in (403, 429):
                raise FeedError("rate_limited", retry_at=_retry_at(error.headers, time.time() if now is None else now),
                                status=error.code) from error
            if 300 <= error.code < 400:
                raise FeedError("unexpected_redirect", status=error.code) from error
            raise FeedError("http_error", status=error.code) from error
        finally:
            if error.fp is not None:
                error.close()
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise FeedError("network_error") from error


def fetch_index(etag=None, timeout=NETWORK_TIMEOUT, now=None):
    body, result_etag = _request(INDEX_URL, MAX_INDEX_SIZE, timeout, 10, etag, now)
    if body is None:
        return {"not_modified": True, "etag": result_etag, "index": None}
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise FeedError("invalid_index") from error
    return {"not_modified": False, "etag": result_etag, "index": validate_index(value)}


def _archive_entries(archive, commit):
    """Validate every member, even those not included in the package manifest."""
    expected_root = "study-abroad-planner-" + commit
    members = archive.infolist()
    if not 1 <= len(members) <= MAX_ARCHIVE_FILES:
        raise FeedError("invalid_archive")
    seen = {}
    files = {}
    total = 0
    for member in members:
        name = member.filename
        # ZipInfo truncates filename at NUL but retains the original spelling.
        if (not isinstance(name, str) or len(name) > 1024 or "\x00" in member.orig_filename
                or member.orig_filename != name or "\\" in name):
            raise FeedError("unsafe_archive_path")
        directory = member.is_dir()
        canonical_name = name[:-1] if directory else name
        parts = canonical_name.split("/")
        if parts[0] != expected_root or any(part in ("", ".", "..") for part in parts):
            raise FeedError("unsafe_archive_path")
        if len(parts) == 1:
            if not directory:
                raise FeedError("unsafe_archive_path")
        else:
            relative = "/".join(parts[1:])
            # The manifest's reserved filename is allowed as an archive member.
            try:
                install._manifest_path(relative if relative != install.MANIFEST_NAME else "manifest.json")
            except install.InstallError as error:
                raise FeedError("unsafe_archive_path") from error
        normalized = unicodedata.normalize("NFC", canonical_name).casefold()
        if normalized in seen:
            raise FeedError("duplicate_archive_path")
        seen[normalized] = directory
        mode = member.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (kind == stat.S_IFDIR) != directory and kind != 0:
            raise FeedError("unsafe_archive_type")
        if member.flag_bits & 1 or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise FeedError("unsupported_archive")
        if directory:
            if member.file_size != 0:
                raise FeedError("invalid_archive")
            continue
        total += member.file_size
        if (member.file_size > install.MAX_FILE_SIZE or total > MAX_EXTRACTED_SIZE
                or member.file_size > max(1, member.compress_size) * 1000):
            raise FeedError("archive_too_large")
        files["/".join(parts[1:])] = member
    for name in seen:
        parts = name.split("/")
        for index in range(1, len(parts)):
            if seen.get("/".join(parts[:index])) is False:
                raise FeedError("unsafe_archive_path")
    return files


def _extract_verified(body, entry, target, deadline):
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            members = _archive_entries(archive, entry["commit"])
            manifest_member = members.get(install.MANIFEST_NAME)
            if manifest_member is None or manifest_member.file_size > install.MAX_MANIFEST_SIZE:
                raise FeedError("missing_manifest")
            raw = archive.read(manifest_member)
            if hashlib.sha256(raw).hexdigest() != entry["manifest_sha256"]:
                raise FeedError("manifest_hash_mismatch")
            (target / install.MANIFEST_NAME).write_bytes(raw)
            manifest, unused = install.read_manifest(target)
            if manifest["version"] != entry["version"]:
                raise FeedError("package_version_mismatch")
            for name, metadata in manifest["files"].items():
                if time.monotonic() >= deadline:
                    raise FeedError("download_timeout")
                member = members.get(name)
                if member is None or member.file_size != metadata["size"]:
                    raise FeedError("package_file_mismatch")
                data = archive.read(member)
                if len(data) != metadata["size"] or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
                    raise FeedError("package_file_mismatch")
                output = target / name
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as stream:
                    stream.write(data)
            install.verify_package(target, manifest)
            if time.monotonic() >= deadline:
                raise FeedError("download_timeout")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, install.InstallError, ValueError) as error:
        raise FeedError("invalid_package") from error


def download_package(entry, destination, timeout=NETWORK_TIMEOUT, total_timeout=TOTAL_TIMEOUT):
    """Create a verified package at a *new* directory, cleaning it on failure.

    Files not listed by the pinned manifest are never extracted. Previous
    versions, the caller's cache state and installed package are not touched.
    """
    entry = validate_entry(entry)
    target = Path(os.path.abspath(os.path.expanduser(str(destination))))
    try:
        install._no_symlinks(target)
        if target.exists() or target == Path(target.anchor) or not target.parent.is_dir():
            raise FeedError("invalid_destination")
    except install.InstallError as error:
        raise FeedError("invalid_destination") from error
    start = time.monotonic()
    deadline = start + total_timeout
    body, unused = _request(ARCHIVE_PREFIX + entry["commit"], MAX_ARCHIVE_SIZE, timeout, total_timeout)
    if "archive_size" in entry and len(body) != entry["archive_size"]:
        raise FeedError("archive_size_mismatch")
    if "archive_sha256" in entry and hashlib.sha256(body).hexdigest() != entry["archive_sha256"]:
        raise FeedError("archive_hash_mismatch")
    created = False
    success = False
    identity = None
    try:
        install._no_symlinks(target)
        target.mkdir(mode=0o700)
        created = True
        identity = (target.stat().st_dev, target.stat().st_ino)
        _extract_verified(body, entry, target, deadline)
        success = True
        return target
    except FeedError:
        raise
    except (OSError, install.InstallError) as error:
        raise FeedError("package_write_error") from error
    finally:
        if created and not success and not target.is_symlink():
            try:
                current = target.stat()
                if (current.st_dev, current.st_ino) == identity:
                    shutil.rmtree(str(target))
            except OSError:
                pass  # A failed package remains inactive; never remove another directory.
