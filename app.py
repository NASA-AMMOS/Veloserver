import os
import math
import config
import shutil
from bottle import response as bottle_response
from process_data import process_hrrr, process_ecmwf, process_gfs, HRRR_PRODUCTS, _safe_path
from datetime import datetime

# Allowlists for user-supplied URL tokens. Every value that ends up in a
# filesystem path or subprocess argument must come from one of these closed
# sets (or be a parsed number/date), so untrusted input can't steer file I/O
# (path-injection hardening, Sonar S2083).
ALLOWED_MODELS = {'hrrr', 'ecmwf', 'gfs'}
ALLOWED_FORMATS = {'gribjson', 'geotiff', 'png'}

# HRRR output format -> (AVAILABLE_FORMATS key, file open mode). Drives reading
# the processed file without a per-format if/elif chain.
_HRRR_FORMAT_IO = {
    'geotiff': ('tiff', 'rb'),
    'png': ('png', 'rb'),
    'gribjson': ('json', 'r'),
}


class App():
    def __init__(self):
        # clear out any pre-existing cache on server startup
        if os.path.exists(config.APP_CONFIG["CACHE_DIR"]):
            if config.APP_CONFIG["CACHE_FILES"] is False:
                shutil.rmtree(config.APP_CONFIG["CACHE_DIR"])
        # create cache directory
        if not os.path.exists(config.APP_CONFIG["CACHE_DIR"]):
            os.makedirs(config.APP_CONFIG["CACHE_DIR"])

    def get_data(self, model, format, iso_string, projwin=None, product='winds'):
        json_ct = config.APP_CONFIG["AVAILABLE_FORMATS"]["json"]

        # Validate all user-supplied tokens before they reach any path/subprocess.
        projwin, error = self._validate_request(model, format, projwin, product)
        if error is not None:
            bottle_response.status = 400
            return (json_ct, error)

        # fromisoformat rejects anything that isn't a real ISO datetime, so the
        # date/hour strings derived from it below are safe to use in paths.
        datetime_object = datetime.fromisoformat(iso_string)

        if model == 'hrrr':
            return self._serve_hrrr(product, projwin, datetime_object, format)
        if model == 'ecmwf':
            return self._serve_json(process_ecmwf(
                projwin,
                datetime_object.strftime("%Y-%m-%d"),
                config.APP_CONFIG["CACHE_DIR"]))
        # Last case would be gfs here.
        return self._serve_json(process_gfs(
            projwin,
            datetime_object.strftime("%Y-%m-%d"),
            datetime_object.strftime("%H:%M:%S"),
            config.APP_CONFIG["CACHE_DIR"]))

    @staticmethod
    def _validate_request(model, format, projwin, product):
        """Validate user tokens. Returns (parsed_projwin, error_msg); error_msg
        is None when everything is valid."""
        if model not in ALLOWED_MODELS:
            return projwin, f'Unsupported model: {model}'
        if format not in ALLOWED_FORMATS:
            return projwin, f'Unsupported format: {format}'
        if model == 'hrrr' and product not in HRRR_PRODUCTS:
            return projwin, f'Unknown product: {product}'
        if projwin is not None:
            try:
                projwin = [float(v) for v in projwin]
            except (TypeError, ValueError):
                projwin = None
            if not projwin or len(projwin) != 4 or not all(map(math.isfinite, projwin)):
                return projwin, 'Invalid projwin: expected 4 numbers ulx,uly,lrx,lry'
        return projwin, None

    def _serve_hrrr(self, product, projwin, datetime_object, format):
        output = process_hrrr(product,
                              projwin,
                              datetime_object.strftime("%Y-%m-%d"),
                              datetime_object.strftime("%H:%M:%S"),
                              config.APP_CONFIG["CACHE_DIR"],
                              format)
        # process_hrrr returns an output file path on success, or a plain
        # error message (e.g. unsupported product/format) on failure.
        if not os.path.isfile(output):
            bottle_response.status = 400
            return (config.APP_CONFIG["AVAILABLE_FORMATS"]["json"], output)
        ct_key, mode = _HRRR_FORMAT_IO.get(format, ('json', 'r'))
        return self._read_output(output, ct_key, mode)

    def _serve_json(self, output):
        """ecmwf/gfs produce a JSON file path on success, or an error string."""
        if not os.path.isfile(output):
            bottle_response.status = 400
            return (config.APP_CONFIG["AVAILABLE_FORMATS"]["json"], output)
        return self._read_output(output, 'json', 'r')

    @staticmethod
    def _read_output(output, ct_key, mode):
        # Re-confine the returned path under CACHE_DIR before reading it, so the
        # file open can't be steered outside the cache. _safe_path is the
        # project's path sanitizer (path-injection hardening, Sonar S2083).
        output = _safe_path(config.APP_CONFIG["CACHE_DIR"], os.path.basename(output))
        with open(output, mode) as f:
            return (config.APP_CONFIG["AVAILABLE_FORMATS"][ct_key], f.read())