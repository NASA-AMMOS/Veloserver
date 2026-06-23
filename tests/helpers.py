"""Shared helpers for the Veloserver integration tests.

Stdlib only. These tests hit a *running* server (default http://localhost:8104)
and validate real responses, so the server must be up with network access to
NOAA HRRR / NOMADS GFS. Bring it up with `docker compose up -d` first.
"""

import os
import re
import math
import json
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("VELOSERVER_URL", "http://localhost:8104")
OUT = os.environ.get("VELOSERVER_TEST_OUT", "/tmp/velotest")
PROJWIN = "-105,41,-104,40"  # small Colorado box: ulx,uly,lrx,lry

HRRR_PRODUCTS = ["winds", "temp_2m", "pbl_height", "smoke_massden",
                 "precip_rate", "rh_2m", "wind_gust", "dewpoint_2m"]
HRRR_FORMATS = ["gribjson", "geotiff", "png"]

_HAS_GDALINFO = subprocess.run(["which", "gdalinfo"],
                               capture_output=True).returncode == 0


def recent_time():
    """A top-of-hour UTC timestamp ~4h ago (HRRR/GFS publish latency)."""
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%dT%H:00:00")


def server_up():
    try:
        urllib.request.urlopen(BASE, timeout=5)
        return True
    except Exception:
        return False


def fetch(path, timeout=240):
    """GET BASE+path. Returns (status, body_bytes, error_str_or_None)."""
    url = BASE + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read(), None
    except urllib.error.HTTPError as e:
        return e.code, e.read(), None
    except Exception as e:
        return None, b"", str(e)


# ---- content validators: return (ok, detail) ----

def validate_gribjson(body, product=None):
    try:
        d = json.loads(body)
    except Exception as e:
        return False, f"not JSON: {e}"
    if not isinstance(d, list):
        return False, "JSON is not a list"
    n = len(d)
    params = [r.get("header", {}).get("parameterNumberName", "?") for r in d]
    expected = 2 if product == "winds" else 1  # winds carries U and V
    return n >= expected, f"records={n} params={params}"


def validate_tiff(body):
    if body[:4] not in (b"II*\x00", b"MM\x00*"):
        return False, f"not a TIFF (magic={body[:4]!r})"
    detail = f"tiff bytes={len(body)}"
    if _HAS_GDALINFO:
        os.makedirs(OUT, exist_ok=True)
        p = os.path.join(OUT, "probe.tif")
        with open(p, "wb") as f:
            f.write(body)
        gi = subprocess.run(["gdalinfo", p], capture_output=True, text=True)
        if gi.returncode != 0:
            return False, "gdalinfo failed to read TIFF"
        bands = gi.stdout.count("Band ")
        srs = "EPSG:3857" if "3857" in gi.stdout else "?"
        detail += f" bands={bands} srs={srs}"
    return True, detail


def validate_png(body):
    if body[:8] != b"\x89PNG\r\n\x1a\n":
        return False, f"not PNG (magic={body[:8]!r})"
    return True, f"png bytes={len(body)}"


def validate_cog_winds(body):
    """The winds COG must be a 3-band raster with band1=u, band2=v, band3=speed,
    where speed == sqrt(u^2 + v^2). Verified by sampling interior pixels."""
    ok, detail = validate_tiff(body)
    if not ok:
        return False, detail
    if not _HAS_GDALINFO:
        return True, detail + " (u/v/speed check skipped: no gdal tools)"

    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "cogwinds.tif")
    with open(p, "wb") as f:
        f.write(body)

    info = subprocess.run(["gdalinfo", p], capture_output=True, text=True).stdout
    bands = info.count("Band ")
    if bands != 3:
        return False, f"expected 3 bands (u, v, speed), got {bands}"
    descs = re.findall(r"Description = (.+)", info)
    if descs != ["u", "v", "speed"]:
        return False, f"band descriptions {descs}, expected ['u', 'v', 'speed']"
    m = re.search(r"Size is (\d+), (\d+)", info)
    if not m:
        return False, "could not parse raster size"
    w, h = int(m.group(1)), int(m.group(2))

    NODATA = 9999.0
    checked = 0
    for fx in (0.25, 0.4, 0.5, 0.6, 0.75):
        for fy in (0.3, 0.5, 0.7):
            x, y = int(w * fx), int(h * fy)
            res = subprocess.run(["gdallocationinfo", "-valonly", p, str(x), str(y)],
                                 capture_output=True, text=True)
            vals = res.stdout.split()
            if len(vals) != 3:
                continue
            try:
                u, v, speed = (float(val) for val in vals)
            except ValueError:
                continue
            if NODATA in (u, v, speed):
                continue
            expected = math.hypot(u, v)
            if abs(speed - expected) > 0.05 + 1e-3 * expected:
                return False, (f"band3 != sqrt(u^2+v^2) at pixel ({x},{y}): "
                               f"u={u:.3f} v={v:.3f} speed={speed:.3f} "
                               f"expected={expected:.3f}")
            checked += 1

    if checked == 0:
        return False, "no valid (non-nodata) pixels found to verify u/v/speed"
    return True, (f"bands [u, v, speed]; speed==hypot(u,v) verified at "
                  f"{checked} pixels")


def validate_cog_scalar(body, product):
    """A scalar COG must be a single band labeled with the product name."""
    ok, detail = validate_tiff(body)
    if not ok:
        return False, detail
    if not _HAS_GDALINFO:
        return True, detail + " (band description check skipped: no gdal tools)"
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "cogscalar.tif")
    with open(p, "wb") as f:
        f.write(body)
    info = subprocess.run(["gdalinfo", p], capture_output=True, text=True).stdout
    descs = re.findall(r"Description = (.+)", info)
    if descs != [product]:
        return False, f"band descriptions {descs}, expected ['{product}']"
    return True, detail + f" desc={descs}"


def validate_nonempty(body):
    return len(body) > 0, f"bytes={len(body)}"


class Results:
    """Minimal PASS/FAIL/SKIP recorder with a print-as-you-go report."""

    def __init__(self):
        self.rows = []  # (name, outcome, detail)

    def record(self, name, outcome, detail=""):
        self.rows.append((name, outcome, detail))
        print(f"[{outcome:4}] {name:46} -> {detail}")

    def passed(self, name, detail=""):
        self.record(name, "PASS", detail)

    def failed(self, name, detail=""):
        self.record(name, "FAIL", detail)

    def skipped(self, name, detail=""):
        self.record(name, "SKIP", detail)

    def check(self, name, ok, detail=""):
        (self.passed if ok else self.failed)(name, detail)
        return ok

    def section(self, title):
        print(f"\n## {title}")

    def summary(self):
        n_pass = sum(1 for _, o, _ in self.rows if o == "PASS")
        n_fail = sum(1 for _, o, _ in self.rows if o == "FAIL")
        n_skip = sum(1 for _, o, _ in self.rows if o == "SKIP")
        print("\n" + "=" * 64)
        print(f"SUMMARY: {n_pass} passed, {n_fail} failed, {n_skip} skipped "
              f"({len(self.rows)} total)")
        if n_fail:
            print("FAILURES:")
            for name, o, detail in self.rows:
                if o == "FAIL":
                    print(f"  - {name}: {detail}")
        return n_fail == 0
