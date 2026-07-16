import os
import config
import shutil
from bottle import static_file, response
from modules import manage_cache
from modules.parse import (_safe_path, validate_request, canonical_product,
                           parse_cog_time, parse_request_time, parse_fxx)
from process_data import process_hrrr, process_ecmwf, process_gfs, ensure_cog

# HRRR output format -> (AVAILABLE_FORMATS key, file open mode). Drives reading
# the processed file without a per-format if/elif chain.
_HRRR_FORMAT_IO = {
    'geotiff': ('tiff', 'rb'),
    'png': ('png', 'rb'),
    'gribjson': ('json', 'r'),
}

# Content type used for error bodies returned through the (content_type, data)
# contract the data routes expect.
_JSON = config.APP_CONFIG["AVAILABLE_FORMATS"]["json"]


def json_error(status, message):
    """Set the HTTP status and return a (content_type, body) pair, matching the
    (content_type, data) contract this module's dispatch methods return. Shared
    with server.py so the status-set + body pattern lives in one place."""
    response.status = status
    return (_JSON, message)


def text_error(status, message):
    """Set the HTTP status and return a plain-text body, for handlers/routes that
    return the response body directly rather than a (content_type, data) pair."""
    response.status = status
    return message


class App():
    def __init__(self):
        # clear out any pre-existing cache on server startup
        if os.path.exists(config.APP_CONFIG["CACHE_DIR"]):
            if config.APP_CONFIG["CACHE_FILES"] is False:
                shutil.rmtree(config.APP_CONFIG["CACHE_DIR"])
        # create cache directory
        if not os.path.exists(config.APP_CONFIG["CACHE_DIR"]):
            os.makedirs(config.APP_CONFIG["CACHE_DIR"])

    def get_data(self, model, format, iso_string, projwin=None, product='winds', fxx_raw=None):
        # Validate all user-supplied tokens before they reach any path/subprocess.
        projwin, error = validate_request(model, format, projwin, product)
        if error is not None:
            return json_error(400, error)

        # a bad datetime is a client error (400), not a server error (500)
        try:
            date, time = parse_request_time(iso_string)
        except ValueError:
            return json_error(400, f'Invalid datetime {iso_string!r}; expected ISO 8601 (e.g. 2024-03-05T19:00:00)')

        # forecast hour only applies to HRRR; ecmwf/gfs handle their own steps
        fxx = 0
        if model == 'hrrr':
            try:
                fxx = parse_fxx(fxx_raw, time)
            except ValueError as e:
                return json_error(400, str(e))

        # if fetching or processing the data fails, return a 502 instead of
        # letting the error become a 500 page
        try:
            if model == 'hrrr':
                result = self._serve_hrrr(product, projwin, date, time, format, fxx)
            elif model == 'ecmwf':
                result = self._serve_json(process_ecmwf(
                    projwin, date, config.APP_CONFIG["CACHE_DIR"]))
            else:
                # Last case would be gfs here.
                result = self._serve_json(process_gfs(
                    projwin, date, time, config.APP_CONFIG["CACHE_DIR"]))
        except Exception as e:
            # flush so the reason is written to the log right away
            print(f'[get_data] upstream {model} fetch failed: {e}', flush=True)
            return json_error(502, f'Upstream data fetch failed for {model}')

        # Keep the cache under its byte budget. Runs after the response is read
        # into memory (and the served file's mtime is freshened), so the file we
        # just returned is the most-recently-used and never the first evicted.
        manage_cache.enforce_configured(config.APP_CONFIG)
        return result

    def serve_cog(self, product, time_param, fxx_raw=None):
        """Serve the EPSG:3857 COG for a product/run/forecast-hour, building it on
        a cache miss. ``fxx_raw`` is the ?fxx= query value (None -> F00)."""
        try:
            product = canonical_product(product)
            date, hour = parse_cog_time(time_param)
            fxx = parse_fxx(fxx_raw, hour)
        except ValueError as e:
            return text_error(400, str(e))
        print(f'[COG] parsed → date={date!r}, hour={hour!r}, fxx={fxx}')
        cache_dir = config.APP_CONFIG['CACHE_DIR']

        try:
            # ensure_cog returns the existing path on a cache hit (before taking any
            # lock) and builds it on a miss, so no separate existence pre-check.
            cog_path = ensure_cog(product, date, hour, fxx, cache_dir)
            # Freshen mtime then evict *before* streaming, so the COG we are about to
            # serve is the most-recently-used file and cannot be deleted mid-stream.
            manage_cache.mark_used(cog_path)
            manage_cache.enforce_configured(config.APP_CONFIG)
            return static_file(os.path.basename(cog_path), root=cache_dir, mimetype='image/tiff')
        except (FileNotFoundError, ValueError):
            return text_error(404, f'No HRRR data available for {product} at the requested time')
        except Exception as e:
            print(f'[COG] error serving {product}: {e}')
            return text_error(500, 'Error serving COG')

    def _serve_hrrr(self, product, projwin, date, time, format, fxx=0):
        output = process_hrrr(product,
                              projwin,
                              date,
                              time,
                              config.APP_CONFIG["CACHE_DIR"],
                              format,
                              fxx)
        # process_hrrr returns an output file path on success, or a plain
        # error message (e.g. unsupported product/format) on failure.
        if not os.path.isfile(output):
            return json_error(400, output)
        ct_key, mode = _HRRR_FORMAT_IO.get(format, ('json', 'r'))
        return self._read_output(output, ct_key, mode)

    def _serve_json(self, output):
        """ecmwf/gfs produce a JSON file path on success, or an error string."""
        if not os.path.isfile(output):
            return json_error(400, output)
        return self._read_output(output, 'json', 'r')

    @staticmethod
    def _read_output(output, ct_key, mode):
        # Re-confine the returned path under CACHE_DIR before reading it, so the
        # file open can't be steered outside the cache. _safe_path is the
        # project's path sanitizer (path-injection hardening, Sonar S2083).
        output = _safe_path(config.APP_CONFIG["CACHE_DIR"], os.path.basename(output))
        # Mark as recently used so LRU eviction keeps recently-used files (manage_cache.py keys on
        # mtime); do this before the read so it counts even on a cache hit.
        manage_cache.mark_used(output)
        with open(output, mode) as f:
            return (config.APP_CONFIG["AVAILABLE_FORMATS"][ct_key], f.read())
