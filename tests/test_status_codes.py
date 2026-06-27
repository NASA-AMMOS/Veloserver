#!/usr/bin/env python3
"""Status-code tests: bad requests must return 400 (not 200-with-body or 500),
and valid requests must return 200.

Run standalone:  python3 tests/test_status_codes.py
Or via the suite: python3 tests/run_all.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Results, fetch, recent_time, PROJWIN


def _expect_status(r, name, path, expected):
    status, body, err = fetch(path)
    if err:
        return r.failed(name, f"ERROR {err}")
    msg = body[:80].decode("utf-8", "replace").strip()
    r.check(name, status == expected, f"got {status} (want {expected}) {msg!r}")


def run(r):
    T = recent_time()

    r.section("Bad requests should return 400")
    # gribjson is velocity-only; scalar products must be rejected, not return []
    _expect_status(r, "scalar gribjson -> 400", f"/hrrr/temp_2m/gribjson/{T}", 400)
    _expect_status(r, "unknown product -> 400", f"/hrrr/bogus/png/{T}", 400)
    _expect_status(r, "unknown model -> 400", f"/mars/gribjson/{T}", 400)
    _expect_status(r, "malformed projwin (3 vals) -> 400",
                   f"/hrrr/winds/gribjson/{T}/1,2,3", 400)

    r.section("Path-injection / malformed input should return 400 (Sonar S2083)")
    _expect_status(r, "COG unknown product -> 400", f"/cog/bogus/{T}Z", 400)
    _expect_status(r, "COG product traversal -> 400", f"/cog/..%2f..%2fetc/{T}Z", 400)
    _expect_status(r, "projwin non-numeric -> 400", f"/gfs/gribjson/{T}/a,b,c,d", 400)
    _expect_status(r, "projwin dotdot -> 400", f"/gfs/gribjson/{T}/..,..,..,..", 400)
    _expect_status(r, "unknown format -> 400", f"/gfs/xml/{T}", 400)

    r.section("Valid requests should return 200")
    _expect_status(r, "winds gribjson -> 200", f"/hrrr/winds/gribjson/{T}", 200)
    _expect_status(r, "scalar geotiff -> 200", f"/hrrr/temp_2m/geotiff/{T}", 200)
    _expect_status(r, "gfs gribjson +projwin -> 200",
                   f"/gfs/gribjson/{T}/{PROJWIN}", 200)


if __name__ == "__main__":
    from helpers import server_up, clear_cache
    if not server_up():
        print("Server not reachable at helpers.BASE — start it (docker compose up -d).")
        sys.exit(2)
    clear_cache()
    r = Results()
    run(r)
    sys.exit(0 if r.summary() else 1)
