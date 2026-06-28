import os
from bottle import Bottle, run, request, response, static_file, abort
from app import App, text_error
from modules.parse import is_allowed_path_info

bottle_app = Bottle()
dataApp = App()


@bottle_app.route('/cog/<product>/<time_param:path>')
def cog_path(product, time_param):
    return dataApp.serve_cog(product, time_param)


def enable_cors(fn):
    def _enable_cors(*args, **kwargs):
        # set CORS headers
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Origin, Accept, Content-Type, ' \
                                                           + 'X-Requested-With, X-CSRF-Token'

        if request.method != 'OPTIONS':
            # actual request; reply with the actual response
            return fn(*args, **kwargs)

    return _enable_cors


@bottle_app.hook('before_request')
def strip_path():
    path = request.environ['PATH_INFO']
    # Reject anything outside the allowlist or containing traversal before the
    # validated value is written back for routing (path-injection guard, S2083).
    if not is_allowed_path_info(path):
        abort(400, 'Invalid request path')
    request.environ['PATH_INFO'] = path.rstrip('/')


@bottle_app.hook('after_request')
def enable_cors_after_request_hook():
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET'
    response.headers['Access-Control-Allow-Headers'] = 'Origin, Accept, Content-Type, X-Requested-With, X-CSRF-Token'


@bottle_app.route('/')
def server_static(filename='index.html'):
    # root = os.path.dirname(__file__)
    return static_file(filename, root='.')


@bottle_app.route('/swagger.yaml')
def server_swagger_yaml(filename='swagger.yaml'):
    return static_file(filename, root='.')


@bottle_app.route('/swagger/<filepath:path>')
def server_swagger(filepath):
    return static_file(filepath, root='./swagger/')


@bottle_app.route('/<model>/<format>/<datetime>')
@enable_cors
def get_data(model, format, datetime):
    (output_format, data) = dataApp.get_data(model,
                                             format,
                                             datetime,
                                             None)
    response.content_type = output_format
    return data


@bottle_app.route('/<model>/<seg2>/<seg3>/<seg4>')
@enable_cors
def get_data_four_segment(model, seg2, seg3, seg4):
    """Two different 4-part URLs land here. A bbox shape (model/format/datetime/projwin)
    and a product shape (model/product/format/datetime) look the same to the router, so
    we tell them apart by content, since only a bbox ever has a comma."""
    if ',' in seg4:
        fmt, dt, projwin = seg2, seg3, seg4.split(',')
        if len(projwin) != 4:
            return text_error(400, 'Invalid projwin. Must be in format: ulx,uly,lrx,lry')
        (output_format, data) = dataApp.get_data(model, fmt, dt, projwin)
    else:
        product, fmt, dt = seg2, seg3, seg4
        (output_format, data) = dataApp.get_data(model, fmt, dt, None, product)
    response.content_type = output_format
    return data


@bottle_app.route('/<model>/<product>/<format>/<datetime>/<projwin>')
@enable_cors
def get_data_product_projwin(model, product, format, datetime, projwin):
    projwin = projwin.split(',')
    if len(projwin) == 4:
        (output_format, data) = dataApp.get_data(model, format, datetime, projwin, product)
        response.content_type = output_format
        return data
    return text_error(400, 'Invalid projwin. Must be in format: ulx,uly,lrx,lry')


# Run several worker processes, each with a few threads. A fresh request ties up
# its slot for a few seconds, mostly waiting on a download or a wgrib2/gdal
# subprocess. Workers give us cores and crash isolation, so a native crash kills
# one worker that gunicorn restarts rather than the whole server. Threads let a
# worker keep serving other requests during those waits and share memory across
# them. The PNG rendering was moved off matplotlib's global state so it is safe to
# run on several threads at once. Set VELOSERVER_THREADS=1 to go back to pure
# workers. gunicorn switches to its threaded worker automatically when threads > 1.
_WORKERS = int(os.environ.get('VELOSERVER_WORKERS', 4))
_THREADS = int(os.environ.get('VELOSERVER_THREADS', 4))
_TIMEOUT = int(os.environ.get('VELOSERVER_TIMEOUT', 60))


def main():
    # production
    if (os.path.exists('/certs/key.pem') and os.path.exists('/certs/cert.pem')):
        run(bottle_app,
            host='0.0.0.0',
            port=8104,
            server='gunicorn',
            workers=_WORKERS,
            threads=_THREADS,
            timeout=_TIMEOUT,
            keyfile='/certs/key.pem',
            certfile='/certs/cert.pem')
    else:
        # dev mode
        run(bottle_app,
            host='0.0.0.0',
            port=8104,
            server='gunicorn',
            workers=_WORKERS,
            threads=_THREADS,
            timeout=_TIMEOUT,
            debug=True,
            reloader=True)


if __name__ == '__main__':
    main()
