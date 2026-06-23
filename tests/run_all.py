#!/usr/bin/env python3
"""Run the full Veloserver integration suite (content validity + status codes)
against a running server, and report one combined pass/fail summary.

Usage:
    docker compose up -d            # server must be running first
    python3 tests/run_all.py

Env:
    VELOSERVER_URL    override base URL (default http://localhost:8104)
    VELOSERVER_ECMWF  set to 1 to include ECMWF (needs .ecmwfapirc credentials)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import Results, server_up, BASE  # noqa: E402
import test_endpoints  # noqa: E402
import test_status_codes  # noqa: E402


def main():
    if not server_up():
        print(f"Server not reachable at {BASE}.")
        print("Start it first:  docker compose up -d")
        return 2

    r = Results()
    print("=" * 64)
    print("CONTENT VALIDITY")
    print("=" * 64)
    test_endpoints.run(r)
    print("\n" + "=" * 64)
    print("STATUS CODES")
    print("=" * 64)
    test_status_codes.run(r)
    return 0 if r.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
