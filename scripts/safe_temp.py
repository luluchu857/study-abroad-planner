"""Temporary work outside package trees, without tempfile's cwd fallback."""

import os
from pathlib import Path
import tempfile


def temporary_directory(*, prefix="study-abroad-", suffix="", excluded_roots=(), preferred=None):
    """Try explicit OS temp locations; never probe or fall back to cwd."""
    roots = [Path(path).expanduser().resolve() for path in excluded_roots]
    candidates = ([preferred] if preferred else []) + [os.environ.get(key) for key in ("TMPDIR", "TEMP", "TMP")]
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(str(Path(local) / "Temp"))
    else:
        candidates.extend(("/tmp", "/var/tmp", "/usr/tmp"))
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            continue
        path = path.resolve()
        if path in seen or any(path == root or root in path.parents for root in roots):
            continue
        seen.add(path)
        if not path.is_dir():
            continue
        try:
            return tempfile.TemporaryDirectory(prefix=prefix, suffix=suffix, dir=str(path))
        except OSError:
            continue
    raise OSError("No writable temporary directory outside the Skill; configure TMPDIR/TEMP or a test temp directory.")
