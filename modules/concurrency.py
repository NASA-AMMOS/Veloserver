import os
import fcntl
import threading
import contextlib

from modules.parse import _safe_path


@contextlib.contextmanager
def _atomic_output(final_path):
    """Write to a unique temp file, then atomically rename it into place.

    wgrib2/gdal/grib2json write incrementally to their output path, so two
    concurrent identical requests -- or a reader hitting the os.path.exists cache
    check mid-write -- could otherwise see a half-written file. Renaming a
    per-process temp into the final path makes the cached file appear only once
    complete (os.replace is atomic on POSIX). Temp confined under the same dir
    (path-injection hardening, Sonar S8707)."""
    uid = f'{os.getpid()}-{threading.get_ident()}'
    tmp = _safe_path(os.path.dirname(final_path),
                     f'{os.path.basename(final_path)}.{uid}.tmp')
    try:
        yield tmp
        if os.path.exists(tmp):
            os.replace(tmp, final_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


@contextlib.contextmanager
def _download_lock(cache_dir, key):
    """Cross-process lock so concurrent identical requests don't fetch the same
    source file at once (duplicate downloads / partial-file reads). An in-memory
    threading.Lock would not span gunicorn worker processes, hence fcntl. The key
    is built only from allowlisted/numeric/date tokens (S2083)."""
    lock_file = open(_safe_path(cache_dir, f'{key}.lock'), 'w')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
