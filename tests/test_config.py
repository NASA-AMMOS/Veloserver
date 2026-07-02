#!/usr/bin/env python3
"""Unit tests for config.py -- integrity of the product catalog and app config.

These guard the merged HRRR_PRODUCTS catalog (one row per product carrying its
GRIB `search` selector AND its colormap/label) plus the 3-band winds-COG colormap
table and APP_CONFIG. Stdlib only (config imports only os).

Run standalone:  python3 tests/test_config.py
Or via the suite: python3 tests/run_all.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers import Results  # noqa: E402
import config  # noqa: E402


def test_hrrr_products(r):
    r.section("config.HRRR_PRODUCTS (merged catalog: search + colormap)")
    prods = config.HRRR_PRODUCTS
    r.check("is a non-empty dict", isinstance(prods, dict) and len(prods) > 0, f"n={len(prods)}")
    for name, row in prods.items():
        r.check(f"{name}: has 'search'", isinstance(row.get("search"), str) and row["search"], "")
        r.check(f"{name}: has 'cmap'", isinstance(row.get("cmap"), str) and row["cmap"], "")
        r.check(f"{name}: has 'label'", isinstance(row.get("label"), str) and row["label"], "")
    # smoke carries the unit-scaling render hints
    smoke = prods.get("smoke_massden", {})
    r.check("smoke_massden has scale/vmin/vmax",
            "scale" in smoke and "vmin" in smoke and "vmax" in smoke, f"{ {k: smoke.get(k) for k in ('scale','vmin','vmax')} }")
    r.check("winds is present (default product)", "winds" in prods, "")


def test_winds_band_colormaps(r):
    r.section("config.WINDS_BAND_COLORMAPS (3-band winds COG)")
    bands = config.WINDS_BAND_COLORMAPS
    # band names + order are the COG contract checked by validate_cog_winds
    r.check("band keys are exactly [u, v, speed]", list(bands) == ["u", "v", "speed"], f"{list(bands)}")
    for name, row in bands.items():
        r.check(f"band {name}: has cmap + label",
                isinstance(row.get("cmap"), str) and isinstance(row.get("label"), str), "")


def test_app_config(r):
    r.section("config.APP_CONFIG")
    cfg = config.APP_CONFIG
    for key in ("CACHE_DIR", "CACHE_FILES", "CACHE_MAX_BYTES", "CACHE_TTL_HOURS",
                "CACHE_TARGET_RATIO", "AVAILABLE_FORMATS", "DEFAULT_FORMAT"):
        r.check(f"has key {key}", key in cfg, "")
    r.check("CACHE_MAX_BYTES is a positive int",
            isinstance(cfg["CACHE_MAX_BYTES"], int) and cfg["CACHE_MAX_BYTES"] > 0, f"{cfg['CACHE_MAX_BYTES']}")
    r.check("CACHE_TARGET_RATIO in (0, 1]",
            isinstance(cfg["CACHE_TARGET_RATIO"], float) and 0 < cfg["CACHE_TARGET_RATIO"] <= 1,
            f"{cfg['CACHE_TARGET_RATIO']}")
    r.check("CACHE_TTL_HOURS is a non-negative int",
            isinstance(cfg["CACHE_TTL_HOURS"], int) and cfg["CACHE_TTL_HOURS"] >= 0, f"{cfg['CACHE_TTL_HOURS']}")
    fmts = cfg["AVAILABLE_FORMATS"]
    r.check("AVAILABLE_FORMATS has json/png/tiff",
            all(k in fmts for k in ("json", "png", "tiff")), f"{list(fmts)}")
    r.check("AVAILABLE_FORMATS values look like mimetypes",
            all("/" in v for v in fmts.values()), f"{fmts}")


def run(r):
    test_hrrr_products(r)
    test_winds_band_colormaps(r)
    test_app_config(r)


if __name__ == "__main__":
    r = Results()
    run(r)
    sys.exit(0 if r.summary() else 1)
