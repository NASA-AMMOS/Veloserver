# Veloserver tests

End-to-end tests that exercise every route and check the real responses, plus
concurrency correctness and cache (LRU) eviction under load.

## Run them

One command. `run_tests.sh` spins up a fresh, disposable container with an
**empty cache**, runs the **entire** suite against it (streaming results live),
and tears it down afterward:

```bash
tests/run_tests.sh
```

Exit code is `0` if everything passed, non-zero otherwise. It uses the prebuilt
veloserver image but bind-mounts your working copy, so it always tests **your
current code** — no rebuild needed.

Options:

```bash
tests/run_tests.sh tests/test_stress.py        # run just one file
KEEP=1 tests/run_tests.sh                       # leave the container up afterward
CACHE_MAX_BYTES=536870912 tests/run_tests.sh    # use a different cache budget
```

## What the suite runs

`run_tests.sh` runs `run_all.py`, which empties the cache first and then executes
each group below in order:

**`test_manage_cache.py` — cache eviction logic** (pure filesystem, no server needed)
Unit tests of `manage_cache.py`: oldest files evicted first down to the budget,
recently-used files protected, lock files never deleted, Herbie subdirectories
recursed and pruned, the optional TTL pass, and `CACHE_MAX_BYTES <= 0` disabling
eviction.

**`test_endpoints.py` — every route returns the right artifact**
HRRR velocity (`gribjson` U/V), all 8 products as `geotiff`/`png`, the COG route
for all 8 products (winds = 3-band u/v/speed, scalars = 1 band), GFS `gribjson`,
the default routes, and `+projwin` subsets. Each response is validated as real
JSON / GeoTIFF / PNG / COG. ECMWF is skipped unless `VELOSERVER_ECMWF=1`.

**`test_status_codes.py` — error handling**
Bad input returns `400` (scalar `gribjson`, unknown product/model, malformed or
path-traversal `projwin`); valid requests return `200`.

**`test_stress.py` — concurrency & cache pressure** (live load)
- many identical uncached requests hitting at once → all valid and byte-identical
  (no partial or corrupt file is ever cached or served)
- every product's PNG rendered at once → all valid (matplotlib thread-safety)
- a large mixed batch at high concurrency → zero failures
- fill the cache past its budget → the recently-used key survives, an untouched
  (old) one is evicted, and the on-disk cache stays within budget

## Running against your own server (advanced)

If you already have a server up (`docker compose up -d`) and want to run the
Python files directly instead of the disposable container:

```bash
python3 tests/run_all.py            # full suite
python3 tests/test_manage_cache.py         # cache unit tests only (no server)
python3 tests/test_stress.py        # concurrency + LRU only
```

Set `VELOSERVER_CACHE_DIR` to the server's cache dir to start from an empty
cache. The test time is auto-computed as a top-of-hour UTC time ~4h ago (within
HRRR/GFS publish latency).

## Configuration (rarely needed)

| Env var | Default | Purpose |
|---|---|---|
| `VELOSERVER_URL` | `http://localhost:8104` | server base URL |
| `VELOSERVER_CACHE_DIR` | unset | server's cache dir; when set, the suite empties it before the run (run_tests.sh sets this for you) |
| `STRESS_BUDGET_BYTES` | `1073741824` (1GB) | server's `CACHE_MAX_BYTES`, for the LRU eviction test |
| `VELOSERVER_ECMWF` | unset | `1` to also test ECMWF (needs `.ecmwfapirc`) |
| `VELOSERVER_TEST_OUT` | private temp dir | scratch dir for TIFF probing |
