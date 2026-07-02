#!/usr/bin/env python3
# Unit tests for manage_cache.py LRU eviction.

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers import Results
from modules import manage_cache


def _mkfile(path, size, mtime):
    """Create a file of `size` bytes with a fixed mtime (and matching atime)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'\0' * size)
    os.utime(path, (mtime, mtime))
    return path


def _exists(d, name):
    return os.path.exists(os.path.join(d, name))


def test_under_budget(r):
    with tempfile.TemporaryDirectory() as d:
        _mkfile(os.path.join(d, 'a'), 100, 1000)
        _mkfile(os.path.join(d, 'b'), 100, 2000)
        deleted = manage_cache.enforce_budget(d, max_bytes=10_000)
        r.check('under budget: nothing deleted', deleted == 0 and _exists(d, 'a') and _exists(d, 'b'),
                f'deleted={deleted}')


def test_evicts_oldest_first(r):
    with tempfile.TemporaryDirectory() as d:
        for i in range(5):  # f0 oldest .. f4 newest, 100 bytes each (total 500)
            _mkfile(os.path.join(d, f'f{i}'), 100, 1000 + i)
        deleted = manage_cache.enforce_budget(d, max_bytes=300, target_ratio=0.85)
        survived = [i for i in range(5) if _exists(d, f'f{i}')]
        r.check('evicts oldest-first down to target', deleted == 3 and survived == [3, 4],
                f'deleted={deleted} survived={survived}')


def test_mark_used_protects(r):
    with tempfile.TemporaryDirectory() as d:
        for i in range(5):
            _mkfile(os.path.join(d, f'f{i}'), 100, 1000 + i)
        manage_cache.mark_used(os.path.join(d, 'f0'))  # make the oldest the newest
        manage_cache.enforce_budget(d, max_bytes=300, target_ratio=0.85)
        r.check('mark_used keeps a recently-used file from eviction',
                _exists(d, 'f0') and _exists(d, 'f4') and not _exists(d, 'f1'),
                f"f0={_exists(d,'f0')} f1={_exists(d,'f1')} f4={_exists(d,'f4')}")


def test_never_evicts_locks(r):
    with tempfile.TemporaryDirectory() as d:
        _mkfile(os.path.join(d, 'big'), 1000, 1000)
        _mkfile(os.path.join(d, 'hrrr-winds.lock'), 1000, 1)      # in-flight download lock
        _mkfile(os.path.join(d, manage_cache._EVICT_LOCK_NAME), 1000, 1)  # eviction's own lock
        manage_cache.enforce_budget(d, max_bytes=100, target_ratio=0.5)  # force heavy eviction
        r.check('lock files are never evicted',
                _exists(d, 'hrrr-winds.lock') and _exists(d, manage_cache._EVICT_LOCK_NAME),
                f"download_lock={_exists(d,'hrrr-winds.lock')} evict_lock={_exists(d, manage_cache._EVICT_LOCK_NAME)}")


def test_recursive_and_prunes_dirs(r):
    with tempfile.TemporaryDirectory() as d:
        # Herbie-style nested download (the heavy files live in subdirs).
        nested = os.path.join(d, 'hrrr', '20260101', 'subset.grib2')
        _mkfile(nested, 5000, 1000)
        _mkfile(os.path.join(d, 'keep'), 100, 5000)  # newest, small -> survives
        deleted = manage_cache.enforce_budget(d, max_bytes=1000, target_ratio=0.85)
        r.check('recursively evicts subdir files and prunes empty dirs',
                deleted == 1 and not os.path.exists(nested)
                and not os.path.isdir(os.path.join(d, 'hrrr')) and _exists(d, 'keep'),
                f'deleted={deleted} nested_gone={not os.path.exists(nested)}')


def test_ttl(r):
    with tempfile.TemporaryDirectory() as d:
        import time
        now = time.time()
        _mkfile(os.path.join(d, 'old'), 100, now - 100_000)  # well past the TTL
        _mkfile(os.path.join(d, 'new'), 100, now)
        # Large byte budget, so only the TTL pass can remove anything.
        deleted = manage_cache.enforce_budget(d, max_bytes=10**9, ttl_seconds=3600)
        r.check('TTL removes stale files regardless of size',
                deleted == 1 and not _exists(d, 'old') and _exists(d, 'new'),
                f'deleted={deleted}')


def test_disabled(r):
    with tempfile.TemporaryDirectory() as d:
        _mkfile(os.path.join(d, 'a'), 1000, 1000)
        deleted = manage_cache.enforce_budget(d, max_bytes=0)  # <=0 disables eviction
        r.check('max_bytes<=0 disables eviction', deleted == 0 and _exists(d, 'a'),
                f'deleted={deleted}')


def test_enforce_configured(r):
    # The wrapper app.py/server.py actually call: reads APP_CONFIG values and must
    # never raise (cache upkeep can't be allowed to fail a data response).
    with tempfile.TemporaryDirectory() as d:
        for i in range(5):
            _mkfile(os.path.join(d, f'f{i}'), 100, 1000 + i)
        cfg = {'CACHE_DIR': d, 'CACHE_MAX_BYTES': 300,
               'CACHE_TTL_HOURS': 0, 'CACHE_TARGET_RATIO': 0.85}
        deleted = manage_cache.enforce_configured(cfg)
        r.check('enforce_configured delegates to enforce_budget', deleted == 3, f'deleted={deleted}')
    # A broken config (missing CACHE_DIR -> KeyError) is swallowed, not raised.
    r.check('enforce_configured swallows config errors -> 0',
            manage_cache.enforce_configured({}) == 0, '')


def run(r):
    r.section('manage_cache.py LRU eviction (unit)')
    for test in (test_under_budget, test_evicts_oldest_first, test_mark_used_protects,
                 test_never_evicts_locks, test_recursive_and_prunes_dirs, test_ttl, test_disabled,
                 test_enforce_configured):
        test(r)


if __name__ == '__main__':
    r = Results()
    run(r)
    sys.exit(0 if r.summary() else 1)
