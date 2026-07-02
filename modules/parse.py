import os
import re
import math
from datetime import datetime

from config import HRRR_PRODUCTS

# Allowlists for user-supplied URL tokens. Every value that ends up in a
# filesystem path or subprocess argument must come from one of these closed
# sets (or be a parsed number/date), so untrusted input can't steer file I/O
# (path-injection hardening, Sonar S2083).
ALLOWED_MODELS = {'hrrr', 'ecmwf', 'gfs'}
ALLOWED_FORMATS = {'gribjson', 'geotiff', 'png'}

# Allowlist of the characters our routes actually use (word chars plus the
# separators in dates, bboxes and products).
_ALLOWED_PATH_INFO = re.compile(r'\A[\w./:,+-]*\Z')


def is_allowed_path_info(path):
    """True if a request PATH_INFO is safe to route: no traversal and only
    allowlisted characters (path-injection guard, S2083). Callers reject the
    request when this returns False."""
    return '..' not in path and bool(_ALLOWED_PATH_INFO.match(path))


def _safe_path(base_dir, filename):
    """Join filename under base_dir and refuse anything that escapes it.

    Cache/output filenames are built from values that can come from CLI args
    (agentic use) or HTTP requests. Confining the constructed path inside its
    base directory stops a '..'-laden component from reading or writing
    arbitrary files (path-injection hardening, Sonar S2083 / S8707).
    """
    base = os.path.abspath(base_dir)
    resolved = os.path.abspath(os.path.join(base, filename))
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ValueError(f'Refusing path outside {base_dir!r}: {filename!r}')
    return resolved


def projwin_to_string(projwin):
    """Convert a projwin into the string used in cache filenames, e.g.
    '-105.0_41.0_-104.0_40.0'. Forcing each value through float keeps the result to
    numbers and separators only -- no path characters that could steer file I/O
    (S2083)."""
    return '_'.join(str(float(v)) for v in projwin)


def validate_request(model, format, projwin, product):
    """Validate user tokens. Returns (parsed_projwin, error_msg); error_msg
    is None when everything is valid."""
    if model not in ALLOWED_MODELS:
        return projwin, f'Unsupported model: {model}'
    if format not in ALLOWED_FORMATS:
        return projwin, f'Unsupported format: {format}'
    if model == 'hrrr' and not is_valid_product(product):
        return projwin, f'Unknown product: {product}'
    if projwin is not None:
        try:
            projwin = [float(v) for v in projwin]
        except (TypeError, ValueError):
            projwin = None
        if not projwin or len(projwin) != 4 or not all(map(math.isfinite, projwin)):
            return projwin, 'Invalid projwin: expected 4 numbers ulx,uly,lrx,lry'
    return projwin, None


def is_valid_product(product):
    """True if ``product`` is a known HRRR product."""
    return product in HRRR_PRODUCTS


def canonical_product(product):
    """Validate ``product`` against the allowlist and return the matching key.

    Returns a value taken from our own HRRR_PRODUCTS set rather than the raw
    request string, so the result is safe to interpolate into file paths and
    subprocess args (path-injection hardening, Sonar S2083). Raises ValueError
    for an unknown product; callers translate that into their own error response.
    """
    if not is_valid_product(product):
        raise ValueError(f'Unknown product: {product}. '
                         f'Valid products: {", ".join(sorted(HRRR_PRODUCTS))}')
    return next(p for p in HRRR_PRODUCTS if p == product)


def normalize_date(date):
    """Validate a 'YYYY-MM-DD' date and return it normalized. Raises ValueError on
    a malformed date; used as a defense-in-depth re-check before the value is
    interpolated into cache/file paths. (Hour handling stays with each model,
    since the rounding rule -- top-of-hour, 00z, 6-hourly -- is model-specific.)"""
    return datetime.strptime(date, '%Y-%m-%d').strftime('%Y-%m-%d')


def parse_request_time(iso_string):
    """Parse a data route's <datetime> into normalized (date, time) strings:
    ('YYYY-MM-DD', 'HH:MM:SS'). Raises ValueError on a non-ISO value; callers
    turn that into a 400. Each model rounds the hour its own way, so that stays
    in process_data. Companion to parse_cog_time, which rounds to the top of the
    hour for the COG route."""
    datetime_object = datetime.fromisoformat(iso_string)
    return datetime_object.strftime('%Y-%m-%d'), datetime_object.strftime('%H:%M:%S')


def parse_cog_time(time_param):
    """Parse the COG route's <time_param> into normalized (date, hour) strings.

    The route is declared <time_param:path>, so anything after the trailing 'Z'
    is noise and is dropped first. Raises ValueError on a missing or non-ISO
    value; callers turn that into a 400."""
    if 'Z' in time_param:
        time_param = time_param[:time_param.index('Z') + 1]
    if not time_param:
        raise ValueError('time parameter is required')
    datetime_object = datetime.fromisoformat(time_param.replace('Z', '+00:00'))
    return datetime_object.strftime('%Y-%m-%d'), datetime_object.strftime('%H:00:00')


def hrrr_format_error(product, format):
    """Return an error message if this product/format combination is unsupported,
    else None. gribjson is only produced for winds; other products must use a
    raster format or the COG route."""
    if format == 'gribjson' and product != 'winds':
        return (f"gribjson is only available for the 'winds' product; "
                f"use geotiff, png, or the /cog route for '{product}'.")
    return None
