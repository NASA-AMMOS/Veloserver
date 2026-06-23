import os
import config
import shutil
from bottle import response as bottle_response
from process_data import process_hrrr, process_ecmwf, process_gfs
from datetime import datetime


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