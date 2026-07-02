import os
import stat
import time
import fcntl

# Lock file eviction holds while it runs
_EVICT_LOCK_NAME = '.evict.lock'

# These guard in-flight downloads and must never be deleted by eviction.
_LOCK_SUFFIX = '.lock'


def mark_used(path):
    """Bump a cached file's mtime so LRU eviction treats it as recently used.

    Best-effort: a file that vanished in a concurrent eviction is ignored rather
    than raised, since marking usage must never fail a request.
    """
    try:
        os.utime(path, None)
    except OSError:
        pass


def _safe_remove(path):
    """Delete ``path``, tolerating a concurrent removal. Returns True if this
    call removed it, False if it was already gone."""
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


def _entry_if_evictable(path, base):
    """Return ``(path, size, mtime)`` if ``path`` is a deletable regular file
    confined under ``base``, else ``None``. Uses ``lstat`` so a symlink is never
    followed out of the cache, and confines the path first (S2083 / S8707)."""
    if path != base and not os.path.abspath(path).startswith(base + os.sep):
        return None
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return path, st.st_size, st.st_mtime


def _evictable_entries(cache_dir):
    """Yield ``(path, size, mtime)`` for every regular file under ``cache_dir``
    that eviction is allowed to delete. Lock files (in-flight downloads) are
    skipped; per-file vetting is in :func:`_entry_if_evictable`."""
    base = os.path.abspath(cache_dir)
    for root, _dirs, files in os.walk(base):
        for name in files:
            if name == _EVICT_LOCK_NAME or name.endswith(_LOCK_SUFFIX):
                continue
            entry = _entry_if_evictable(os.path.join(root, name), base)
            if entry is not None:
                yield entry


def _prune_empty_dirs(cache_dir):
    """Remove now-empty subdirectories left behind by eviction (e.g. Herbie's
    per-date folders). The cache directory itself is never removed. Best-effort."""
    base = os.path.abspath(cache_dir)
    for root, _dirs, _files in os.walk(base, topdown=False):
        if root == base:
            continue
        try:
            os.rmdir(root)
        except OSError:
            pass


def _ttl_pass(entries, ttl_seconds):
    """Delete entries whose mtime is older than ``ttl_seconds`` ago (when > 0).
    Returns ``(kept_entries, num_deleted)``."""
    if ttl_seconds <= 0:
        return entries, 0
    cutoff = time.time() - ttl_seconds
    kept, deleted = [], 0
    for path, size, mtime in entries:
        if mtime < cutoff and _safe_remove(path):
            deleted += 1
        else:
            kept.append((path, size, mtime))
    return kept, deleted


def _size_pass(entries, max_bytes, target_ratio):
    """Delete oldest files by last modified time (mtime) first until the total is at or below
    ``max_bytes * target_ratio`` (a low-water mark). Returns the number deleted."""
    total = sum(size for _, size, _ in entries)
    if total <= max_bytes:
        return 0
    target = int(max_bytes * target_ratio)
    deleted = 0
    for path, size, _mtime in sorted(entries, key=lambda e: e[2]):  # oldest first
        if total <= target:
            break
        if _safe_remove(path):
            deleted += 1
            total -= size
    return deleted


def enforce_budget(cache_dir, max_bytes, ttl_seconds=0, target_ratio=0.85):
    """Evict files until the cache fits its budget; return the number deleted.

    Files older than ``ttl_seconds`` (when > 0) go first regardless of size, then
    oldest-mtime files are deleted down to ``max_bytes * target_ratio`` if the
    total still exceeds ``max_bytes``. A budget of ``<= 0`` disables eviction.
    """
    if max_bytes <= 0 or not os.path.isdir(cache_dir):
        return 0

    lock_file = open(os.path.join(cache_dir, _EVICT_LOCK_NAME), 'w')
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # another worker is already evicting; skip this pass

        entries, deleted = _ttl_pass(list(_evictable_entries(cache_dir)), ttl_seconds)
        deleted += _size_pass(entries, max_bytes, target_ratio)
        if deleted:
            _prune_empty_dirs(cache_dir)
        return deleted
    finally:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()


def enforce_configured(app_config):
    """Run :func:`enforce_budget` from ``APP_CONFIG`` values, containing any
    error so cache maintenance can never fail a data response.

    Returns the number of files deleted (0 if eviction was skipped or failed).
    """
    try:
        return enforce_budget(
            app_config['CACHE_DIR'],
            app_config['CACHE_MAX_BYTES'],
            app_config.get('CACHE_TTL_HOURS', 0) * 3600,
            app_config.get('CACHE_TARGET_RATIO', 0.85),
        )
    except Exception as exc:
        print(f'[cache] eviction skipped: {exc}')
        return 0
