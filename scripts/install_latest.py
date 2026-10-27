#!/usr/bin/env python3
"""Install the newest published official Skill package, without a pinned tag.

By default both public channels are considered. --stable-only is an explicit
choice. Existing versioned installations keep their bootstrap directory and
receive a new immutable runtime; no downloaded installer is executed.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import install
import update_feed as feed
import update_runtime as runtime


class LatestInstallError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def select_latest(index, stable_only=False):
    index = feed.validate_index(index)
    selected = None
    for channel in (("stable",) if stable_only else ("stable", "pilot")):
        entry = index["channels"][channel]
        if entry is not None and (selected is None or runtime.newer_than(entry["version"], selected[1]["version"])):
            selected = (channel, entry)
    if selected is None:
        raise LatestInstallError("no_published_version")
    return selected


def _clean_package(path):
    try:
        return runtime._package(path, check_python=True)
    except Exception as error:
        raise LatestInstallError("existing_installation_modified_or_invalid") from error


def _legacy_compatible(old_path, old_manifest, candidate_path, new_manifest):
    required = {name for name in old_manifest["files"] if not name.startswith("tests/")}
    if not required.issubset(new_manifest["files"]):
        raise LatestInstallError("required_file_would_be_removed")
    old_cards = runtime._cards(old_path, old_manifest)
    new_cards = runtime._cards(candidate_path, new_manifest)
    if any(key not in new_cards or new_cards[key] != value for key, value in old_cards.items()):
        raise LatestInstallError("knowledge_would_change_or_be_removed")


def install_latest(host, destination=None, stable_only=False, state=None, fetch=None, download=None):
    """Resolve fresh metadata, protect an existing install, then install or stage.

    state is an isolated-state injection for tests/administration, not a network
    override. The CLI always uses the normal per-installation state directory.
    """
    fetch = fetch or feed.fetch_index
    download = download or feed.download_package
    target = install.destination_for(host, destination)
    install._no_symlinks(target)
    if target == Path(target.anchor) or (target.exists() and not target.is_dir()):
        raise LatestInstallError("invalid_installation_target")

    old = None
    selected_runtime = None
    runtime_state = None
    if target.exists():
        old = _clean_package(target)
        if runtime.POLICY_NAME in old[0]["files"]:
            runtime_state = runtime._state_path(target, state, create=False)
            selected_runtime = runtime.resolve_runtime(target, runtime_state)
            if not selected_runtime.get("updates_allowed"):
                raise LatestInstallError(selected_runtime.get("error") or "existing_runtime_not_ready")
            current_manifest, current_raw, current_digest = _clean_package(Path(selected_runtime["runtime_path"]))
        else:
            # A legacy package must not hide an existing automatic-update state.
            runtime_state = runtime._state_path(target, state, create=False)
            if (runtime_state / runtime.POINTER_NAME).exists() or (runtime_state / runtime.POINTER_NAME).is_symlink():
                raise LatestInstallError("legacy_installation_has_runtime_state")
            current_manifest, current_raw, current_digest = old
    else:
        current_manifest = current_raw = current_digest = None

    response = fetch()
    if not isinstance(response, dict) or response.get("not_modified") or response.get("index") is None:
        raise LatestInstallError("fresh_update_index_required")
    channel, entry = select_latest(response["index"], stable_only)
    result = {"target": str(target), "channel": channel,
              "latest_available_version": entry["version"], "source_commit": entry["commit"],
              "manifest_sha256": entry["manifest_sha256"], "backup": None}
    if current_manifest:
        current_version = current_manifest["version"]
        if runtime.newer_than(current_version, entry["version"]):
            return dict(result, status="kept_newer", version=current_version,
                        effective_version=current_version,
                        runtime_path=selected_runtime["runtime_path"] if selected_runtime else str(target))
        if current_version == entry["version"]:
            if current_digest != entry["manifest_sha256"]:
                raise LatestInstallError("same_version_different_package")
            return dict(result, status="unchanged", version=current_version,
                        effective_version=current_version,
                        runtime_path=selected_runtime["runtime_path"] if selected_runtime else str(target))

    with tempfile.TemporaryDirectory(prefix="study-abroad-latest-") as temporary:
        source = download(entry, Path(temporary).resolve() / "package")
        manifest, raw, digest = runtime._package(source, check_python=True)
        if manifest["version"] != entry["version"] or digest != entry["manifest_sha256"]:
            raise LatestInstallError("download_identity_mismatch")
        if selected_runtime:
            # publish_candidate rechecks both baseline and effective runtime while
            # holding its lock, including version, local changes and knowledge.
            published = runtime.publish_candidate(source, runtime_state, target)
            return dict(result, status=published["status"], version=published["version"],
                        effective_version=published["version"], runtime_path=published["runtime_path"],
                        bootstrap_version=old[0]["version"])
        if old:
            # Do not replace a legacy installation edited while downloading.
            again = _clean_package(target)
            if again[1] != old[1]:
                raise LatestInstallError("existing_installation_changed")
            _legacy_compatible(target, old[0], source, manifest)
        elif target.exists() or target.is_symlink():
            raise LatestInstallError("installation_appeared_during_download")
        installed = install.install_package(source, target, expected_manifest_sha256=old[2] if old else None)
        return dict(result, status=installed["status"], version=installed["version"],
                    effective_version=installed["version"], runtime_path=str(target),
                    backup=installed["backup"], file_count=installed["file_count"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, choices=("codex", "workbuddy", "custom"))
    parser.add_argument("--destination", help="Final Skill directory, not its parent")
    parser.add_argument("--stable-only", action="store_true", help="Only if the user requested stable releases")
    args = parser.parse_args(argv)
    try:
        result = install_latest(args.host, args.destination, args.stable_only)
        code = 0
    except Exception as error:
        reason = getattr(error, "code", None)
        if not isinstance(reason, str) or len(reason) > 120:
            reason = "latest_install_failed"
        result = {"status": "error", "error": reason}
        if isinstance(error, install.InstallError):
            result["error"] = "package_install_failed"
            result["detail"] = str(error)
            result["backup"] = getattr(error, "backup", None)
        retry_at = getattr(error, "retry_at", None)
        if isinstance(retry_at, (int, float)):
            result["retry_at"] = retry_at
        code = 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
