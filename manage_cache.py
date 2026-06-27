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


def _evictable_entries(cache_dir):
    """Yield ``(path, size, mtime)`` for every regular file under ``cache_dir``
    that eviction is allowed to delete.

    Lock files are skipped (they guard in-flight downloads). Symlinks and other
    non-regular entries are skipped via ``lstat`` so eviction can never follow a
    link out of the cache when accounting for or deleting files (S2083 / S8707).
    """
    base = os.path.abspath(cache_dir)
    for root, _dirs, files in os.walk(base):
        for name in files:
            if name == _EVICT_LOCK_NAME or name.endswith(_LOCK_SUFFIX):
                continue
            path = os.path.join(root, name)
            # Confine under the cache dir before any stat/remove (defensive even
            # though os.walk stays within base unless symlinks are followed).
            if path != base and not os.path.abspath(path).startswith(base + os.sep):
                continue
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            yield path, st.st_size, st.st_mtime


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


def enforce_budget(cache_dir, max_bytes, ttl_seconds=0, target_ratio=0.85):
    """Evict least-recently-used files until the cache fits its budget.

    Files older than ``ttl_seconds`` (when > 0) are removed first regardless of
    size. If the total still exceeds ``max_bytes``, files are deleted
    oldest-mtime-first down to ``max_bytes * target_ratio`` -- a low-water mark,
    so a full eviction does not re-run on every subsequent request.

    Returns the number of files deleted. A budget of ``<= 0`` disables eviction.
    """
    if max_bytes <= 0:
        return 0
    if not os.path.isdir(cache_dir):
        return 0

    lock_path = os.path.join(cache_dir, _EVICT_LOCK_NAME)
    lock_file = open(lock_path, 'w')
    deleted = 0
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0  # another worker is already evicting; skip this pass

        entries = list(_evictable_entries(cache_dir))

        if ttl_seconds > 0:
            cutoff = time.time() - ttl_seconds
            kept = []
            for path, size, mtime in entries:
                if mtime < cutoff and _safe_remove(path):
                    deleted += 1
                else:
                    kept.append((path, size, mtime))
            entries = kept

        total = sum(size for _, size, _ in entries)
        if total > max_bytes:
            target = int(max_bytes * target_ratio)
            entries.sort(key=lambda e: e[2])  # oldest mtime first
            for path, size, _mtime in entries:
                if total <= target:
                    break
                if _safe_remove(path):
                    deleted += 1
                    total -= size

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
