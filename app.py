import os
import config
import shutil
from process_winds import process_hrrr, process_ecmwf
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

    def get_data(self, request, model, format, iso_string, projwin=None):
        response = ''
        datetime_object = datetime.fromisoformat(iso_string)

        if model == 'hrrr':
            output = process_hrrr(projwin,
                                  datetime_object.strftime("%Y-%m-%d"),
                                  datetime_object.strftime("%H:%M:%S"),
                                  config.APP_CONFIG["CACHE_DIR"],
                                  format)
            with open(output, 'r') as f:
                response = f.read()

        elif model == 'ecmwf':
            output = process_ecmwf(projwin,
                                   datetime_object.strftime("%Y-%m-%d"),
                                   datetime_object.strftime("%H:%M:%S"),
                                   config.APP_CONFIG["CACHE_DIR"],
                                   format)
            with open(output, 'r') as f:
                response = f.read()
        else:
            response = 'Model is not supported.'

        return (config.APP_CONFIG["AVAILABLE_FORMATS"]["json"], response)
