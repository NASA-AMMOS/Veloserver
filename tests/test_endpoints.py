#!/usr/bin/env python3
"""Content-validity tests: hit every route x product x format and verify the
response body is the right kind of artifact (valid velocity JSON / GeoTIFF / PNG).

Run standalone:  python3 tests/test_endpoints.py
Or via the suite: python3 tests/run_all.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ( 
    Results, fetch, recent_time, PROJWIN, HRRR_PRODUCTS, HRRR_FORMATS,
    validate_gribjson, validate_tiff, validate_png, validate_nonempty,
    validate_cog_winds, validate_cog_scalar,
)


def _validate(fmt, body, product=None):
    if fmt == "gribjson":
        return validate_gribjson(body, product)
    if fmt == "cog":
        # winds = 3-band u/v/speed (speed==hypot(u,v)); scalars = 1 band named for the product
        return (validate_cog_winds(body) if product == "winds"
                else validate_cog_scalar(body, product))
    if fmt == "geotiff":
        return validate_tiff(body)
    if fmt == "png":
        return validate_png(body)
    return validate_nonempty(body)


def _run_case(r, name, path, fmt, product=None):
    status, body, err = fetch(path)
    if err:
        return r.failed(name, f"ERROR {err}")
    if status != 200:
        return r.failed(name, f"HTTP {status}: {body[:100].decode('utf-8', 'replace')}")
    ok, detail = _validate(fmt, body, product)
    r.check(name, ok, detail)


def run(r):
    T = recent_time()
    TZ = T + "Z"
    print(f"# time={T}  projwin={PROJWIN}  base set in helpers.BASE")

    r.section("Static routes")
    status, body, _ = fetch("/")
    r.check("GET /", status == 200 and len(body) > 0, f"HTTP {status} bytes={len(body)}")

    r.section("HRRR velocity product (gribjson = U/V vectors)")
    _run_case(r, "hrrr/winds/gribjson", f"/hrrr/winds/gribjson/{T}", "gribjson", "winds")
    _run_case(r, "hrrr/gribjson [default=winds]", f"/hrrr/gribjson/{T}", "gribjson", "winds")
    _run_case(r, "hrrr/winds/gribjson +projwin",
              f"/hrrr/winds/gribjson/{T}/{PROJWIN}", "gribjson", "winds")

    r.section("HRRR raster products x {geotiff, png}")
    for prod in HRRR_PRODUCTS:
        for fmt in ("geotiff", "png"):
            _run_case(r, f"hrrr/{prod}/{fmt}", f"/hrrr/{prod}/{fmt}/{T}", fmt, prod)

    r.section("HRRR raster default route (winds)")
    _run_case(r, "hrrr/geotiff [default]", f"/hrrr/geotiff/{T}", "geotiff", "winds")
    _run_case(r, "hrrr/png [default]", f"/hrrr/png/{T}", "png", "winds")

    r.section("HRRR raster + projwin subset")
    _run_case(r, "hrrr/temp_2m/geotiff +projwin",
              f"/hrrr/temp_2m/geotiff/{T}/{PROJWIN}", "geotiff", "temp_2m")

    r.section("COG raster route (/cog/<product>/<time>) — every product")
    for prod in HRRR_PRODUCTS:
        _run_case(r, f"cog/{prod}", f"/cog/{prod}/{TZ}", "cog", prod)

    r.section("GFS (gribjson only)")
    _run_case(r, "gfs/gribjson [global]", f"/gfs/gribjson/{T}", "gribjson", "winds")
    _run_case(r, "gfs/gribjson +projwin", f"/gfs/gribjson/{T}/{PROJWIN}", "gribjson", "winds")

    r.section("ECMWF (requires credentials)")
    if os.environ.get("VELOSERVER_ECMWF") == "1":
        _run_case(r, "ecmwf/gribjson", f"/ecmwf/gribjson/{T}", "gribjson", "winds")
    else:
        r.skipped("ecmwf/gribjson",
                  "set VELOSERVER_ECMWF=1 to test (needs .ecmwfapirc credentials)")


if __name__ == "__main__":
    from helpers import Results, server_up, clear_cache
    if not server_up():
        print("Server not reachable at helpers.BASE — start it (docker compose up -d).")
        sys.exit(2)
    clear_cache()
    r = Results()
    run(r)
    sys.exit(0 if r.summary() else 1)
