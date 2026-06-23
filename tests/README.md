# Veloserver integration tests

End-to-end tests that exercise every route against a **running** server and
validate the real responses (velocity JSON, GeoTIFF, PNG, COG). Stdlib only —
no pytest or other dependencies required.

## Prerequisites

The server must be up with network access to NOAA HRRR (S3) and NOMADS GFS:

```bash
docker compose up -d
```

`gdalinfo` (from `gdal-bin`) is optional; if present, TIFF responses are
additionally checked for band count and EPSG:3857 projection.

## Run

```bash
python3 tests/run_all.py            # full suite, combined summary
python3 tests/test_endpoints.py     # content validity only
python3 tests/test_status_codes.py  # status codes only
```

Exit code is `0` when all pass, `1` on failure, `2` if the server is unreachable.

## What's covered

`test_endpoints.py` — content validity:
- HRRR velocity (`gribjson`) → JSON with U and V records; default route; `+projwin`
- HRRR rasters: all 8 products × {`geotiff`, `png`}; default route; `+projwin`
- COG route `/cog/<product>/<time>` for all 8 products (winds = 3-band u/v/speed)
- GFS `gribjson` (global and `+projwin`)
- ECMWF (skipped unless `VELOSERVER_ECMWF=1`, since it needs credentials)

`test_status_codes.py` — error handling:
- `400` for scalar `gribjson`, unknown product, unknown model, malformed projwin
- `200` for valid winds/scalar/GFS requests

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `VELOSERVER_URL` | `http://localhost:8104` | server base URL |
| `VELOSERVER_ECMWF` | unset | set to `1` to test ECMWF (requires `.ecmwfapirc`) |
| `VELOSERVER_TEST_OUT` | `/tmp/velotest` | scratch dir for TIFF probing |

The test timestamp is computed automatically as a top-of-hour UTC time ~4h ago
to stay within HRRR/GFS publish latency.
