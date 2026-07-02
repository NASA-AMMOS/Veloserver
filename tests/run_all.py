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
    VELOSERVER_STRESS     set to 1 to run the slow stress tests (off by default)
    VELOSERVER_ECMWF      set to 1 to include ECMWF (needs .ecmwfapirc credentials)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers import Results, server_up, BASE, clear_cache  # noqa: E402
import test_parse  # noqa: E402
import test_concurrency  # noqa: E402
import test_config  # noqa: E402
import test_manage_cache  # noqa: E402
import test_process_data  # noqa: E402
import test_endpoints  # noqa: E402
import test_status_codes  # noqa: E402
import test_stress  # noqa: E402


def _module(name):
    """Print a banner naming the test file that is about to run, so in the
    combined output it's clear which module each result belongs to."""
    print("\n" + "=" * 64)
    print(f"MODULE: {name}")
    print("=" * 64)


def main():
    r = Results()
    # Empty the server's cache so eviction and cache-hit timing are measured from
    # an empty cache. No-op (warns) if VELOSERVER_CACHE_DIR isn't set.
    print("=" * 64)
    print("CLEAR CACHE (start empty)")
    print("=" * 64)
    clear_cache()
    # Unit tests need no server/network and always run, one module at a time.
    # test_process_data self-skips when the GIS stack isn't importable (i.e.
    # outside the container).
    _module("test_parse")
    test_parse.run(r)
    _module("test_concurrency")
    test_concurrency.run(r)
    _module("test_config")
    test_config.run(r)
    _module("test_manage_cache")
    test_manage_cache.run(r)
    _module("test_process_data")
    test_process_data.run(r)

    if not server_up():
        print(f"\nServer not reachable at {BASE}; ran unit tests only.")
        print("Start it for integration tests:  docker compose up -d")
        r.summary()
        return 2

    _module("test_endpoints")
    test_endpoints.run(r)
    _module("test_status_codes")
    test_status_codes.run(r)

    # test_stress runs in two parts. The fast concurrency checks (identical-request
    # races and concurrent PNG renders) always run. The slam/LRU-eviction checks
    # overfill the cache on purpose, so they're slow and only run when
    # VELOSERVER_STRESS=1 is set; they assume a disposable instance whose
    # CACHE_MAX_BYTES matches STRESS_BUDGET_BYTES.
    _module("test_stress")
    r.section("identical-request races (atomic write + download lock)")
    test_stress.test_concurrent_identical(r)
    r.section("concurrent PNG renders (matplotlib thread-safety)")
    test_stress.test_concurrent_png_renders(r)
    if os.environ.get("VELOSERVER_STRESS") == "1":
        r.section("slam stability (mixed load)")
        test_stress.test_slam(r)
        r.section("LRU eviction under pressure (cache full)")
        test_stress.test_lru_eviction(r)
    else:
        r.skipped("slam stability (mixed load)", "set VELOSERVER_STRESS=1 to run (slow)")
        r.skipped("LRU eviction under pressure (cache full)", "set VELOSERVER_STRESS=1 to run (slow)")
    return 0 if r.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
