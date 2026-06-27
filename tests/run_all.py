#!/usr/bin/env python3
"""Run the full Veloserver integration suite (content validity + status codes)
against a running server, and report one combined pass/fail summary.

Usage:
    docker compose up -d            # server must be running first
    python3 tests/run_all.py

Env:
    VELOSERVER_URL        override base URL (default http://localhost:8104)
    VELOSERVER_CACHE_DIR  path to the server's cache dir (as seen from here); when
                          set, the suite empties it first so every run starts from
                          an empty cache. Unset -> the cache is left as-is. Point
                          only at a disposable cache.
    STRESS_BUDGET_BYTES   the server's CACHE_MAX_BYTES, for the LRU eviction test
                          (default 1GB; must match the instance under test)
    VELOSERVER_ECMWF      set to 1 to include ECMWF (needs .ecmwfapirc credentials)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers import Results, server_up, BASE, clear_cache  # noqa: E402
import test_manage_cache  # noqa: E402
import test_endpoints  # noqa: E402
import test_status_codes  # noqa: E402
import test_stress  # noqa: E402


def main():
    r = Results()
    # Empty the server's cache so eviction and cache-hit timing are measured from
    # an empty cache. No-op (warns) if VELOSERVER_CACHE_DIR isn't set.
    print("=" * 64)
    print("CLEAR CACHE (start empty)")
    print("=" * 64)
    clear_cache()
    # Unit tests need no server/network and always run.
    print("\n" + "=" * 64)
    print("UNIT (cache LRU)")
    print("=" * 64)
    test_manage_cache.run(r)

    if not server_up():
        print(f"\nServer not reachable at {BASE}; ran unit tests only.")
        print("Start it for integration tests:  docker compose up -d")
        r.summary()
        return 2

    print("\n" + "=" * 64)
    print("CONTENT VALIDITY")
    print("=" * 64)
    test_endpoints.run(r)
    print("\n" + "=" * 64)
    print("STATUS CODES")
    print("=" * 64)
    test_status_codes.run(r)

    # Concurrency correctness. Only the SAFE checks (identical-request races and
    # concurrent PNG renders) run here -- they exercise the threaded worker and the
    # thread-safe matplotlib rendering without filling the cache. The slam/LRU
    # stress tests deliberately overfill the cache, so they stay opt-in via
    # test_stress.py against a disposable instance.
    print("\n" + "=" * 64)
    print("CONCURRENCY (thread-safety)")
    print("=" * 64)
    r.section("identical-request races (atomic write + download lock)")
    test_stress.test_concurrent_identical(r)
    r.section("concurrent PNG renders (matplotlib thread-safety)")
    test_stress.test_concurrent_png_renders(r)

    # Slam + LRU-eviction-under-pressure. These overfill the cache on purpose, so
    # the LRU assertions assume a disposable instance whose CACHE_MAX_BYTES matches
    # STRESS_BUDGET_BYTES (mismatch -> the eviction checks just fail, telling you).
    print("\n" + "=" * 64)
    print("STRESS (slam + LRU eviction under pressure)")
    print("=" * 64)
    r.section("slam stability (mixed load)")
    test_stress.test_slam(r)
    r.section("LRU eviction under pressure (cache full)")
    test_stress.test_lru_eviction(r)
    return 0 if r.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
