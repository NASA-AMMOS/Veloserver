import os
import math
import config
import shutil
from bottle import response as bottle_response
from process_data import process_hrrr, process_ecmwf, process_gfs, HRRR_PRODUCTS
from datetime import datetime

# Allowlists for user-supplied URL tokens. Every value that ends up in a
# filesystem path or subprocess argument must come from one of these closed
# sets (or be a parsed number/date), so untrusted input can't steer file I/O
# (path-injection hardening, Sonar S2083).
ALLOWED_MODELS = {'hrrr', 'ecmwf', 'gfs'}
ALLOWED_FORMATS = {'gribjson', 'geotiff', 'png'}


class App():
    def __init__(self, *args, **kwargs):
        # clear out any pre-existing cache on server startup
        if os.path.exists(config.APP_CONFIG["CACHE_DIR"]):
            if config.APP_CONFIG["CACHE_FILES"] is False:
                shutil.rmtree(config.APP_CONFIG["CACHE_DIR"])
        # create cache directory
        if not os.path.exists(config.APP_CONFIG["CACHE_DIR"]):
            os.makedirs(config.APP_CONFIG["CACHE_DIR"])

    def get_data(self, request, model, format, iso_string, projwin=None, product='winds'):
        response = ''
        json_ct = config.APP_CONFIG["AVAILABLE_FORMATS"]["json"]

        # Validate all user-supplied tokens before they reach any path/subprocess.
        if model not in ALLOWED_MODELS:
            bottle_response.status = 400
            return (json_ct, f'Unsupported model: {model}')
        if format not in ALLOWED_FORMATS:
            bottle_response.status = 400
            return (json_ct, f'Unsupported format: {format}')
        if model == 'hrrr' and product not in HRRR_PRODUCTS:
            bottle_response.status = 400
            return (json_ct, f'Unknown product: {product}')
        if projwin is not None:
            try:
                projwin = [float(v) for v in projwin]
            except (TypeError, ValueError):
                projwin = None
            if not projwin or len(projwin) != 4 or not all(map(math.isfinite, projwin)):
                bottle_response.status = 400
                return (json_ct, 'Invalid projwin: expected 4 numbers ulx,uly,lrx,lry')

        # fromisoformat rejects anything that isn't a real ISO datetime, so the
        # date/hour strings derived from it below are safe to use in paths.
        datetime_object = datetime.fromisoformat(iso_string)

        if model == 'hrrr':
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
            if format == 'geotiff':
                with open(output, 'rb') as f:
                    response = f.read()
                return (config.APP_CONFIG["AVAILABLE_FORMATS"]["tiff"], response)
            elif format == 'png':
                with open(output, 'rb') as f:
                    response = f.read()
                return (config.APP_CONFIG["AVAILABLE_FORMATS"]["png"], response)
            else:
                with open(output, 'r') as f:
                    response = f.read()
                return (config.APP_CONFIG["AVAILABLE_FORMATS"]["json"], response)

        elif model == 'ecmwf':
            output = process_ecmwf(projwin,
                                   datetime_object.strftime("%Y-%m-%d"),
                                   datetime_object.strftime("%H:%M:%S"),
                                   config.APP_CONFIG["CACHE_DIR"],
                                   format)
            if not os.path.isfile(output):
                bottle_response.status = 400
                return (config.APP_CONFIG["AVAILABLE_FORMATS"]["json"], output)
            with open(output, 'r') as f:
                response = f.read()
            return (config.APP_CONFIG["AVAILABLE_FORMATS"]["json"], response)

        elif model == 'gfs':
            output = process_gfs(projwin,
                                 datetime_object.strftime("%Y-%m-%d"),
                                 datetime_object.strftime("%H:%M:%S"),
                                 config.APP_CONFIG["CACHE_DIR"],
                                 format)
            if not os.path.isfile(output):
                bottle_response.status = 400
                return (config.APP_CONFIG["AVAILABLE_FORMATS"]["json"], output)
            with open(output, 'r') as f:
                response = f.read()
            return (config.APP_CONFIG["AVAILABLE_FORMATS"]["json"], response)

        else:
            bottle_response.status = 400
            response = 'Model is not supported.'

        return (config.APP_CONFIG["AVAILABLE_FORMATS"]["json"], response)