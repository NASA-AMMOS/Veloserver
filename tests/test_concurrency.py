#!/usr/bin/env python3
"""Unit tests for modules/concurrency.py -- atomic publish + the cross-process
download lock. Stdlib only (os/fcntl/threading/contextlib + parse._safe_path).

Run standalone:  python3 tests/test_concurrency.py
Or via the suite: python3 tests/run_all.py
"""

import os
import sys
import fcntl
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers import Results  # noqa: E402
from modules import concurrency  # noqa: E402


def _flock_is_free(path):
    """True if LOCK_EX can be taken on a fresh fd of path (i.e. nobody holds it)."""
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        f.close()


def test_atomic_output(r):
    r.section("_atomic_output (atomic publish + temp confinement, S8707)")
    d = tempfile.mkdtemp(prefix="velo-atomic-")
    try:
        final = os.path.join(d, "out.txt")
        with concurrency._atomic_output(final) as tmp:
            r.check("temp confined under final's dir",
                    os.path.dirname(os.path.abspath(tmp)) == os.path.abspath(d), f"tmp={tmp}")
            r.check("final absent mid-write", not os.path.exists(final), "")
            with open(tmp, "w") as f:
                f.write("hello")
        r.check("final exists after success", os.path.exists(final), "")
        r.check("final has the written content", open(final).read() == "hello", "")
        r.check("no temp left after success",
                [n for n in os.listdir(d) if n != "out.txt"] == [], "")

        # failure path: exception inside the block -> final NOT published, temp cleaned
        final2 = os.path.join(d, "out2.txt")
        raised = False
        try:
            with concurrency._atomic_output(final2) as tmp2:
                with open(tmp2, "w") as f:
                    f.write("partial")
                raise RuntimeError("boom")
        except RuntimeError:
            raised = True
        r.check("exception propagates out of the context", raised, "")
        r.check("final NOT created on failure", not os.path.exists(final2), "")
        r.check("temp cleaned on failure",
                [n for n in os.listdir(d) if n != "out.txt"] == [], "")

        # atomic replace overwrites an existing final
        with concurrency._atomic_output(final) as tmp3:
            with open(tmp3, "w") as f:
                f.write("world")
        r.check("atomic replace overwrites existing", open(final).read() == "world", "")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_download_lock(r):
    r.section("_download_lock (cross-process flock exclusivity)")
    d = tempfile.mkdtemp(prefix="velo-lock-")
    try:
        key = "hrrr-winds-2024-03-05T190000"
        lock_path = os.path.join(d, key + ".lock")
        r.check("lock free before acquire", _flock_is_free(lock_path), "")
        with concurrency._download_lock(d, key):
            r.check("lock file created", os.path.exists(lock_path), "")
            # a separate open-file-description of the same file must be blocked
            r.check("lock is exclusive while held", not _flock_is_free(lock_path),
                    "a second flock should block")
        r.check("lock released after context exit", _flock_is_free(lock_path), "")
        # re-acquire after release works (no leaked lock / fd)
        with concurrency._download_lock(d, key):
            r.check("lock re-acquired after release", not _flock_is_free(lock_path),
                    "the second acquire should hold the lock")
        r.check("re-acquire after release works", _flock_is_free(lock_path), "")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run(r):
    test_atomic_output(r)
    test_download_lock(r)


if __name__ == "__main__":
    r = Results()
    run(r)
    sys.exit(0 if r.summary() else 1)
