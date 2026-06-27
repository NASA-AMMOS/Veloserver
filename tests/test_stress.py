#!/usr/bin/env python3
"""Stress / concurrency test for a *running* Veloserver.

Proves three things against a live server under load:

  A. Concurrency correctness -- many concurrent IDENTICAL uncached requests all
     return a valid, non-corrupt, byte-identical body (proves the atomic
     temp+rename writes and the download lock: no partial file is ever cached
     or served).
  B. Slam stability -- a large mixed batch at high concurrency returns zero
     errors and zero malformed bodies.
  C. LRU eviction -- after pushing well past the cache budget, a recently-used
     key kept in use during the fill survives while an untouched early key is
     evicted. When STRESS_CACHE_DIR points at the server's cache it asserts this
     deterministically by file presence (and that the on-disk total stays within
     the byte budget); otherwise it falls back to a response-latency heuristic
     (cache hit fast vs. rebuilt slow).

Env:
  VELOSERVER_URL        base URL (default http://localhost:8105)
  STRESS_BUDGET_BYTES   the server's CACHE_MAX_BYTES
  STRESS_CONCURRENCY    concurrent workers for the slam
  STRESS_CACHE_DIR      host path to the server's cache dir (optional, enables
                        the on-disk size assertion)
  STRESS_CACHED_MAX     seconds under which a response counts as served from cache
  STRESS_REBUILT_MIN    seconds over which a response counts as rebuilt (uncached)
"""

import os
import sys
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import (
    Results, server_up, BASE, HRRR_PRODUCTS, fetch,
    validate_gribjson, validate_tiff, validate_png,
)
from datetime import datetime, timedelta, timezone

BUDGET = int(os.environ.get("STRESS_BUDGET_BYTES", 1024 * 1024 * 1024))
CONCURRENCY = int(os.environ.get("STRESS_CONCURRENCY", 16))
CACHE_DIR = os.environ.get("STRESS_CACHE_DIR") or os.environ.get("VELOSERVER_CACHE_DIR")
CACHED_MAX = float(os.environ.get("STRESS_CACHED_MAX", 0.6))
REBUILT_MIN = float(os.environ.get("STRESS_REBUILT_MIN", 1.0))


def hours_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00")


def timed_get(path, timeout=300):
    t = time.time()
    status, body, err = fetch(path, timeout=timeout)
    return status, body, err, time.time() - t


def cog(product, h):
    return f"/cog/{product}/{hours_ago(h)}Z"


def _validate(fmt, body, product=None):
    if fmt in ("cog", "geotiff"):
        return validate_tiff(body)
    if fmt == "gribjson":
        return validate_gribjson(body, product)
    if fmt == "png":
        return validate_png(body)
    return (len(body) > 0, f"bytes={len(body)}")


# ---------------------------------------------------------------- A. race

def test_concurrent_identical(r):
    """N concurrent identical uncached requests -> all valid and byte-identical."""
    n = max(8, CONCURRENCY)
    cases = [
        ("cog winds", cog("winds", 7), "cog", "winds"),
        ("cog temp_2m", cog("temp_2m", 7), "cog", "temp_2m"),
        ("hrrr winds gribjson", f"/hrrr/winds/gribjson/{hours_ago(7)}", "gribjson", "winds"),
        ("hrrr temp_2m geotiff", f"/hrrr/temp_2m/geotiff/{hours_ago(7)}", "geotiff", "temp_2m"),
        ("hrrr winds png", f"/hrrr/winds/png/{hours_ago(7)}", "png", "winds"),
    ]
    for name, path, fmt, product in cases:
        with ThreadPoolExecutor(max_workers=n) as ex:
            results = list(ex.map(lambda _, path=path: timed_get(path), range(n)))
        statuses = [s for s, _, _, _ in results]
        bodies = [b for _, b, _, _ in results]
        oks = [_validate(fmt, b, product)[0] for b in bodies if b]
        hashes = {hashlib.sha256(b).hexdigest() for b in bodies if b}
        all_200 = all(s == 200 for s in statuses)
        all_valid = len(oks) == n and all(oks)
        identical = len(hashes) == 1
        r.check(f"race x{n}: {name} all 200 + valid (no corrupt/partial)",
                all_200 and all_valid,
                f"statuses={set(statuses)} valid={sum(oks)}/{n} distinct_bodies={len(hashes)}")
        # Identical body is the strong signal that no partial write leaked; report
        # but don't hard-fail if the toolchain is non-deterministic byte-for-byte.
        r.check(f"race x{n}: {name} bodies byte-identical", identical,
                f"distinct_hashes={len(hashes)}")


# -------------------------------------------------- A2. concurrent PNG renders

def test_concurrent_png_renders(r):
    """Every product's PNG rendered CONCURRENTLY -> all return a valid PNG.

    Direct check on the thread-safe (object-oriented) matplotlib rendering: under
    the threaded worker several _create_png calls run at once in one process, and
    with pyplot's process-global state they could raise or corrupt each other.
    Distinct products force genuinely different renders to overlap, and repeating
    the batch keeps several in flight at once. Catches the crash/exception failure
    mode of thread-unsafe rendering (a 500 or an empty/invalid body)."""
    T = hours_ago(6)
    specs = [(p, f"/hrrr/{p}/png/{T}") for p in HRRR_PRODUCTS]
    batch = specs * 3  # overlap renders even as some get cached
    with ThreadPoolExecutor(max_workers=max(8, CONCURRENCY)) as ex:
        results = list(ex.map(lambda s: (s[0], timed_get(s[1])), batch))

    bad = []
    valid = 0
    for product, (status, body, _err, _lat) in results:
        ok = status == 200 and body and validate_png(body)[0]
        if ok:
            valid += 1
        else:
            why = validate_png(body)[1] if body else f"status={status}"
            bad.append(f"{product}: {why}")
    r.check(f"concurrent PNG: {len(batch)} reqs / {len(HRRR_PRODUCTS)} products, all valid PNG",
            not bad, f"valid={valid}/{len(batch)} failures={len(bad)}")
    for b in bad[:5]:
        r.record("  png failure", "FAIL", b)


# ---------------------------------------------------------------- B. slam

def _slam_specs():
    specs = []
    for h in range(5, 13):
        for p in HRRR_PRODUCTS:
            specs.append((cog(p, h), "cog", p))
    for h in range(5, 13):
        specs.append((f"/hrrr/winds/gribjson/{hours_ago(h)}", "gribjson", "winds"))
        specs.append((f"/gfs/gribjson/{hours_ago(h)}", "gribjson", "winds"))
    # duplicate the list so the same keys collide under concurrency (contention)
    return specs + specs


def test_slam(r):
    specs = _slam_specs()
    start = time.time()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        out = list(ex.map(lambda s: (s, timed_get(s[0])), specs))
    wall = time.time() - start

    bad = []
    lats = []
    for (path, fmt, product), (status, body, err, lat) in out:
        lats.append(lat)
        ok = status == 200 and body and _validate(fmt, body, product)[0]
        if not ok:
            bad.append(f"{path} -> status={status} err={err}")
    lats.sort()
    p50 = lats[len(lats) // 2]
    p95 = lats[int(len(lats) * 0.95)]
    r.check(f"slam: {len(specs)} reqs @ conc={CONCURRENCY}, zero failures",
            not bad,
            f"throughput={len(specs)/wall:.1f} req/s p50={p50:.2f}s p95={p95:.2f}s "
            f"failures={len(bad)}")
    for b in bad[:5]:
        r.record("  slam failure", "FAIL", b)


# ---------------------------------------------------------------- C. LRU

def _cache_bytes():
    """Sum non-lock file sizes under STRESS_CACHE_DIR, or None if unavailable."""
    if not CACHE_DIR or not os.path.isdir(CACHE_DIR):
        return None
    total = 0
    for root, _d, files in os.walk(CACHE_DIR):
        for name in files:
            if name.endswith(".lock"):
                continue
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _cog_cache_basename(product, ts):
    """The on-disk COG filename the server writes for ``/cog/<product>/<ts>``.

    Mirrors ``process_data._cog_filename`` (kept in sync by hand rather than
    imported, since the stress test may run from a host without the server's
    GIS deps) so eviction can be checked by file presence instead of by latency.
    ``ts`` is a 'YYYY-MM-DDTHH:00:00' timestamp as produced by ``hours_ago``."""
    date, _, hms = ts.partition("T")
    hour = hms.replace(":", "")
    return f"hrrr-{product}-{date}T{hour}-3857-cog.tif"


def _in_cache(basename):
    """True/False if ``basename`` is present under CACHE_DIR; None when the cache
    dir can't be inspected (e.g. testing a remote server)."""
    if not CACHE_DIR or not os.path.isdir(CACHE_DIR):
        return None
    for _root, _d, files in os.walk(CACHE_DIR):
        if basename in files:
            return True
    return False


def test_lru_eviction(r):
    # Pin the timestamps ONCE. hours_ago() recomputes "now" on every call, so
    # re-deriving these mid-test could cross an hour boundary and silently retarget
    # a different cache key than the one we seeded.
    victim_ts = hours_ago(46)   # requested once, never touched again -> oldest
    recent_ts = hours_ago(4)    # kept in use throughout the fill -> survives
    victim = f"/cog/temp_2m/{victim_ts}Z"
    recent = f"/cog/winds/{recent_ts}Z"
    victim_name = _cog_cache_basename("temp_2m", victim_ts)
    recent_name = _cog_cache_basename("winds", recent_ts)

    # Seed both into the cache. The victim must actually land on disk, or there is
    # nothing for eviction to remove and the assertion would be vacuous.
    vstat, vbody, _ve, _vl = timed_get(victim)
    timed_get(recent)
    victim_seeded = vstat == 200 and bool(vbody)

    # Fill: distinct uncached COGs until we've pushed well past the budget,
    # re-requesting the recent key every few rounds so it stays most-recently-used.
    pushed = 0
    target = int(BUDGET * 1.6)
    fill = [(p, h) for h in range(6, 46) for p in HRRR_PRODUCTS]
    n = 0
    for p, h in fill:
        if (p, h) in (("temp_2m", 46), ("winds", 4)):
            continue
        status, body, _err, _lat = timed_get(cog(p, h))
        if status == 200 and body:
            pushed += len(body)
            n += 1
        if n % 4 == 0:
            timed_get(recent)  # refresh recency
        if pushed >= target:
            break

    on_disk = _cache_bytes()
    detail_disk = f" cache_on_disk={on_disk/1e6:.0f}MB budget={BUDGET/1e6:.0f}MB" if on_disk else ""
    r.record("lru: fill", "PASS",
             f"pushed={pushed/1e6:.0f}MB across {n} uncached COGs (target>{target/1e6:.0f}MB){detail_disk}")

    # Only when the fill created more than a budget's worth of data did eviction
    # actually have to run; below that, a surviving victim says nothing.
    pressure_ok = pushed >= BUDGET

    def _victim_skip_reason():
        if not victim_seeded:
            return f"victim not cached at seed (status={vstat}); no HRRR data ~46h back to evict"
        if not pressure_ok:
            return (f"fill pushed only {pushed/1e6:.0f}MB, under the {BUDGET/1e6:.0f}MB "
                    f"budget; not enough pressure to force eviction")
        return None

    # Inspect the cache directory FIRST -- this is the deterministic eviction
    # signal. It must be read before any further request to the victim, since
    # requesting an evicted key rebuilds it straight back into the cache. Response
    # latency can't tell "served from cache" from "rebuilt fast from a still-cached
    # GRIB" from "failed fast", so it is only a fallback when the cache dir is not
    # visible (e.g. testing a remote server).
    victim_present = _in_cache(victim_name)
    recent_present = _in_cache(recent_name)
    skip_reason = _victim_skip_reason()

    if recent_present is None:
        # Cache dir not visible: fall back to the (less reliable) timing heuristic.
        _s, _b, _e, recent_lat = timed_get(recent)
        r.check("lru: recently-used key survived eviction (still fast)",
                recent_lat < CACHED_MAX,
                f"latency={recent_lat:.3f}s (< {CACHED_MAX}s) [timing-based; cache dir not visible]")
        if skip_reason:
            r.skipped("lru: untouched (oldest) key was evicted (rebuilt on request)", skip_reason)
        else:
            _s2, _b2, _e2, vic_lat = timed_get(victim)
            r.check("lru: untouched (oldest) key was evicted (rebuilt on request)",
                    vic_lat > REBUILT_MIN,
                    f"victim_latency={vic_lat:.3f}s (> {REBUILT_MIN}s) [timing-based; cache dir not visible]")
    else:
        # Recent key should still be on disk; confirm it also serves fast.
        _s, _b, _e, recent_lat = timed_get(recent)
        r.check("lru: recently-used key survived eviction (present + fast)",
                recent_present and recent_lat < CACHED_MAX,
                f"in_cache={recent_present} latency={recent_lat:.3f}s (< {CACHED_MAX}s)")
        if skip_reason:
            r.skipped("lru: untouched (oldest) key was evicted (gone from cache)", skip_reason)
        else:
            r.check("lru: untouched (oldest) key was evicted (gone from cache)",
                    not victim_present, f"victim_in_cache={victim_present}")

    if on_disk is not None:
        r.check("lru: on-disk cache stays within budget",
                on_disk <= BUDGET * 1.15,
                f"on_disk={on_disk/1e6:.0f}MB <= {BUDGET*1.15/1e6:.0f}MB")
    else:
        r.skipped("lru: on-disk cache stays within budget",
                  "set STRESS_CACHE_DIR to the server's cache dir to enable")


def run(r):
    r.section("A. concurrency correctness (identical uncached requests)")
    test_concurrent_identical(r)
    r.section("A2. concurrent PNG renders (matplotlib thread-safety)")
    test_concurrent_png_renders(r)
    r.section("B. slam stability (mixed load)")
    test_slam(r)
    r.section("C. LRU eviction under pressure")
    test_lru_eviction(r)


if __name__ == "__main__":
    from helpers import clear_cache
    if not server_up():
        print(f"Server not reachable at {BASE}. Start the test instance first.")
        sys.exit(2)
    print(f"# base={BASE} budget={BUDGET/1e6:.0f}MB concurrency={CONCURRENCY} "
          f"cached<{CACHED_MAX}s rebuilt>{REBUILT_MIN}s")
    clear_cache()
    r = Results()
    run(r)
    sys.exit(0 if r.summary() else 1)
