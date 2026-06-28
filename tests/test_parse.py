#!/usr/bin/env python3
"""Unit tests for modules/parse.py -- the validation / cleaning boundary.

Stdlib only, no server, no GIS deps (parse imports only os/re/math/datetime +
config). Run standalone:  python3 tests/test_parse.py
Or via the suite:         python3 tests/run_all.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers import Results  # noqa: E402
from modules import parse  # noqa: E402


def _raises_valueerror(fn):
    """True iff fn() raises ValueError (and not some other error / no error)."""
    try:
        fn()
    except ValueError:
        return True
    except Exception:
        return False
    return False


def test_safe_path(r):
    r.section("_safe_path (path confinement, S2083)")
    base = "/tmp/velo-cache"
    abase = os.path.abspath(base)
    r.check("plain filename joins under base",
            parse._safe_path(base, "hrrr-winds.tif") == os.path.join(abase, "hrrr-winds.tif"),
            "")
    r.check("nested filename under base allowed",
            parse._safe_path(base, "sub/f.tif") == os.path.join(abase, "sub/f.tif"), "")
    r.check("'../' traversal rejected",
            _raises_valueerror(lambda: parse._safe_path(base, "../etc/passwd")), "want ValueError")
    r.check("deep '../../' traversal rejected",
            _raises_valueerror(lambda: parse._safe_path(base, "../../etc/passwd")), "want ValueError")
    r.check("absolute path escape rejected",
            _raises_valueerror(lambda: parse._safe_path(base, "/etc/passwd")), "want ValueError")
    r.check("sneaky mid-path '..' rejected",
            _raises_valueerror(lambda: parse._safe_path(base, "sub/../../etc/passwd")), "want ValueError")
    r.check("base itself (empty name) allowed",
            parse._safe_path(base, "") == abase, "")


def test_is_allowed_path_info(r):
    r.section("is_allowed_path_info (route allowlist, S2083)")
    ok = [
        "/hrrr/winds/gribjson/2024-03-05T19:00:00",
        "/cog/winds/2024-03-05T19:00:00Z",
        "/hrrr/winds/gribjson/2024-03-05T19:00:00/-105,41,-104,40",  # bbox commas+minus
        "/gfs/gribjson/2024-03-05T19:00:00/+1,+2",                   # plus sign
        "",                                                          # empty matches \A...\Z*
        "/",
    ]
    bad = [
        "/hrrr/../etc",          # traversal
        "/cog/..%2f..%2fetc",    # encoded-ish but contains '..'
        "/hrrr/<script>",        # angle brackets not allowlisted
        "/hrrr/a b",             # space not allowlisted
        "/hrrr/a;rm",            # semicolon not allowlisted
    ]
    for p in ok:
        r.check(f"allow {p!r}", parse.is_allowed_path_info(p) is True, "")
    for p in bad:
        r.check(f"reject {p!r}", parse.is_allowed_path_info(p) is False, "")


def test_validate_request(r):
    r.section("validate_request (model/format/product/projwin)")
    # valid, no projwin
    pj, err = parse.validate_request("hrrr", "gribjson", None, "winds")
    r.check("valid hrrr/gribjson/winds -> no error", err is None and pj is None, f"err={err!r}")
    # valid with projwin -> parsed to floats
    pj, err = parse.validate_request("hrrr", "geotiff", ["1", "2", "3", "4"], "winds")
    r.check("valid projwin parsed to floats", err is None and pj == [1.0, 2.0, 3.0, 4.0], f"pj={pj} err={err!r}")
    # bad model
    _, err = parse.validate_request("mars", "gribjson", None, "winds")
    r.check("unknown model -> error", err is not None and "model" in err.lower(), f"err={err!r}")
    # bad format
    _, err = parse.validate_request("hrrr", "xml", None, "winds")
    r.check("unknown format -> error", err is not None and "format" in err.lower(), f"err={err!r}")
    # bad product (hrrr only)
    _, err = parse.validate_request("hrrr", "gribjson", None, "bogus")
    r.check("unknown hrrr product -> error", err is not None and "product" in err.lower(), f"err={err!r}")
    # product NOT checked for non-hrrr models
    _, err = parse.validate_request("gfs", "gribjson", None, "bogus")
    r.check("product ignored for gfs -> no error", err is None, f"err={err!r}")
    # projwin wrong count
    _, err = parse.validate_request("hrrr", "geotiff", ["1", "2", "3"], "winds")
    r.check("projwin with 3 values -> error", err is not None and "projwin" in err.lower(), f"err={err!r}")
    # projwin non-numeric
    _, err = parse.validate_request("hrrr", "geotiff", ["a", "b", "c", "d"], "winds")
    r.check("projwin non-numeric -> error", err is not None and "projwin" in err.lower(), f"err={err!r}")
    # projwin non-finite (inf)
    _, err = parse.validate_request("hrrr", "geotiff", ["1", "2", "3", "inf"], "winds")
    r.check("projwin with inf -> error", err is not None and "projwin" in err.lower(), f"err={err!r}")
    # projwin with nan
    _, err = parse.validate_request("hrrr", "geotiff", ["1", "2", "3", "nan"], "winds")
    r.check("projwin with nan -> error", err is not None and "projwin" in err.lower(), f"err={err!r}")


def test_product_helpers(r):
    r.section("is_valid_product / canonical_product")
    r.check("is_valid_product('winds') True", parse.is_valid_product("winds") is True, "")
    r.check("is_valid_product('bogus') False", parse.is_valid_product("bogus") is False, "")
    r.check("canonical_product('temp_2m') round-trips", parse.canonical_product("temp_2m") == "temp_2m", "")
    # returns the value FROM the allowlist (identity / laundering), not the raw arg
    raw = "".join(["wi", "nds"])  # equal-by-value but a distinct object
    out = parse.canonical_product(raw)
    r.check("canonical_product returns a known key", out in parse.HRRR_PRODUCTS, f"out={out!r}")
    r.check("canonical_product('bogus') raises ValueError",
            _raises_valueerror(lambda: parse.canonical_product("bogus")), "want ValueError")


def test_normalize_date(r):
    r.section("normalize_date")
    r.check("valid date passes through", parse.normalize_date("2024-03-05") == "2024-03-05", "")
    r.check("non-zero-padded normalized", parse.normalize_date("2024-3-5") == "2024-03-05", "")
    r.check("garbage raises ValueError",
            _raises_valueerror(lambda: parse.normalize_date("not-a-date")), "want ValueError")
    r.check("impossible month raises ValueError",
            _raises_valueerror(lambda: parse.normalize_date("2024-13-01")), "want ValueError")
    r.check("wrong format (slashes) raises ValueError",
            _raises_valueerror(lambda: parse.normalize_date("03/05/2024")), "want ValueError")


def test_projwin_to_string(r):
    r.section("projwin_to_string (numeric-only filename fragment, S2083)")
    r.check("floats joined with underscores",
            parse.projwin_to_string([-105, 41, -104, 40]) == "-105.0_41.0_-104.0_40.0",
            f"got {parse.projwin_to_string([-105, 41, -104, 40])!r}")
    r.check("string inputs coerced to float",
            parse.projwin_to_string(["-105", "41", "-104", "40"]) == "-105.0_41.0_-104.0_40.0", "")
    r.check("non-numeric raises ValueError (no path chars survive)",
            _raises_valueerror(lambda: parse.projwin_to_string(["..", "/etc", "x", "y"])), "want ValueError")


def test_parse_request_time(r):
    r.section("parse_request_time (data route datetime -> date, time)")
    d, t = parse.parse_request_time("2024-03-05T19:00:00")
    r.check("ISO -> (date, time)", (d, t) == ("2024-03-05", "19:00:00"), f"got {(d, t)}")
    d, t = parse.parse_request_time("2024-03-05T19:30:45")
    r.check("minutes/seconds preserved (no rounding)", t == "19:30:45", f"time={t}")
    r.check("empty raises ValueError",
            _raises_valueerror(lambda: parse.parse_request_time("")), "want ValueError")
    r.check("non-ISO raises ValueError",
            _raises_valueerror(lambda: parse.parse_request_time("garbage")), "want ValueError")


def test_parse_cog_time(r):
    r.section("parse_cog_time (COG route time -> date, hour)")
    d, h = parse.parse_cog_time("2024-03-05T19:00:00Z")
    r.check("ISO+Z -> (date, hour)", (d, h) == ("2024-03-05", "19:00:00"), f"got {(d, h)}")
    d, h = parse.parse_cog_time("2024-03-05T19:30:00Z")
    r.check("minutes truncated to top of hour", h == "19:00:00", f"hour={h}")
    d, h = parse.parse_cog_time("2024-03-05T19:00:00Z/extra/junk")
    r.check("path noise after 'Z' dropped", (d, h) == ("2024-03-05", "19:00:00"), f"got {(d, h)}")
    r.check("empty raises ValueError",
            _raises_valueerror(lambda: parse.parse_cog_time("")), "want ValueError")
    r.check("non-ISO raises ValueError",
            _raises_valueerror(lambda: parse.parse_cog_time("garbage")), "want ValueError")


def test_hrrr_format_error(r):
    r.section("hrrr_format_error (gribjson is winds-only)")
    r.check("scalar + gribjson -> message",
            parse.hrrr_format_error("temp_2m", "gribjson") is not None, "")
    r.check("winds + gribjson -> None", parse.hrrr_format_error("winds", "gribjson") is None, "")
    r.check("scalar + geotiff -> None", parse.hrrr_format_error("temp_2m", "geotiff") is None, "")
    r.check("scalar + png -> None", parse.hrrr_format_error("temp_2m", "png") is None, "")


def run(r):
    test_safe_path(r)
    test_is_allowed_path_info(r)
    test_validate_request(r)
    test_product_helpers(r)
    test_normalize_date(r)
    test_projwin_to_string(r)
    test_parse_request_time(r)
    test_parse_cog_time(r)
    test_hrrr_format_error(r)


if __name__ == "__main__":
    r = Results()
    run(r)
    sys.exit(0 if r.summary() else 1)
