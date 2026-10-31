"""Portable test fixtures; never create probes or artifacts in an installed Skill."""

import errno
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("skill_safe_temp", ROOT / "scripts" / "safe_temp.py")
safe_temp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(safe_temp)


def TemporaryDirectory(*, prefix="study-abroad-test-", suffix=""):
    return safe_temp.temporary_directory(prefix=prefix, suffix=suffix, excluded_roots=(ROOT,),
                                         preferred=os.environ.get("STUDY_ABROAD_TEST_TMPDIR"))


def symlink_or_skip(test, target, link, *, target_is_directory=None):
    """Skip only demonstrated lack of symlink support/permission, not failures."""
    target = Path(target)
    link = Path(link)
    try:
        link.symlink_to(target, target_is_directory=target.is_dir() if target_is_directory is None else target_is_directory)
    except NotImplementedError:
        test.skipTest("This Python/filesystem cannot create symbolic links")
    except OSError as error:
        if error.errno in (errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP) or getattr(error, "winerror", None) == 1314:
            test.skipTest("Symbolic link creation unavailable: errno=%s winerror=%s" % (error.errno, getattr(error, "winerror", None)))
        raise
    test.assertTrue(link.is_symlink(), "Fixture must actually be a symbolic link")
