#!/usr/bin/env python3
"""Unit tests for the pure (non-GIS) helpers in process_data.py: longitude
conversion and the COG cache-filename scheme.

process_data imports the heavy stack (herbie / ecmwfapi / convert -> matplotlib,
rasterio) at module load, so it can only be imported where that stack exists (the
container). The import is wrapped: locally these tests SKIP; in the container they
run. The filename assertions double as a guard that the COG cache scheme matches
what test_stress hand-mirrors.

Run standalone:  python3 tests/test_process_data.py
Or via the suite: python3 tests/run_all.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers import Results  # noqa: E402

try:
    from process_data import lon360, _cog_name_prefix, _cog_filename
    _IMPORT_ERR = None
except Exception as e:  # heavy GIS/web deps absent (e.g. running outside the container)
    _IMPORT_ERR = e


def test_lon360(r):
    r.section("process_data.lon360 (-180..180 -> 0..360)")
    cases = [(-105, 255), (105, 105), (0, 0), (-180, 180), (180, 180),
             (-1, 359), (-0.5, 359.5), (-360, 0)]
    for lon, want in cases:
        r.check(f"lon360({lon}) == {want}", lon360(lon) == want, f"got {lon360(lon)}")
    r.check("lon360 accepts a numeric string", lon360("-105") == 255, f"got {lon360('-105')}")


def test_cog_filename(r):
    r.section("process_data COG cache-filename scheme")
    prefix = _cog_name_prefix("winds", "2024-03-05", "19:00:00", 0)
    r.check("name prefix strips colons from hour, appends zero-padded fxx",
            prefix == "hrrr-winds-2024-03-05T190000-f00", f"got {prefix!r}")
    fname = _cog_filename("winds", "2024-03-05", "19:00:00", 0)
    r.check("cog filename = prefix + -3857-cog.tif",
            fname == "hrrr-winds-2024-03-05T190000-f00-3857-cog.tif", f"got {fname!r}")
    r.check("filename has no path separators (single cache file)",
            "/" not in fname and os.sep not in fname, f"got {fname!r}")
    r.check("scalar product flows through the same scheme",
            _cog_filename("temp_2m", "2024-03-05", "00:00:00", 0)
            == "hrrr-temp_2m-2024-03-05T000000-f00-3857-cog.tif", "")
    r.check("distinct forecast hours get distinct filenames",
            _cog_filename("winds", "2024-03-05", "00:00:00", 6)
            == "hrrr-winds-2024-03-05T000000-f06-3857-cog.tif", "")
    r.check("two-digit fxx keeps zero-padding width",
            _cog_name_prefix("winds", "2024-03-05", "00:00:00", 48)
            == "hrrr-winds-2024-03-05T000000-f48", "")


def run(r):
    if _IMPORT_ERR is not None:
        r.skipped("process_data unit tests",
                  f"GIS/web deps not importable here ({type(_IMPORT_ERR).__name__}); runs in container")
        return
    test_lon360(r)
    test_cog_filename(r)


if __name__ == "__main__":
    r = Results()
    run(r)
    sys.exit(0 if r.summary() else 1)
