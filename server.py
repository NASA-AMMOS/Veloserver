import os
import re
from datetime import datetime
from bottle import Bottle, run, request, response, static_file, abort
from app import App
from process_data import _ensure_3857_geotiff, HRRR_PRODUCTS, _safe_path
import config

bottle_app = Bottle()
dataApp = App()

# Allowlist of the characters our routes actually use (word chars plus the
# separators in dates, bboxes and products). PATH_INFO is request-controlled, so
# it is validated against this before being normalized for routing (S2083).
_ALLOWED_PATH_INFO = re.compile(r'\A[\w./:,+-]*\Z')


def _serve_cog(product, time_param):
    # product is interpolated into the COG file path, so restrict it to the known
    # set before any I/O (path-injection hardening, Sonar S2083). Error responses
    # must not echo raw request input back to the client (reflected XSS, S5131):
    # report the allowlisted product names instead of the bad value, and never
    # reflect the raw time_param or exception text.
    if product not in HRRR_PRODUCTS:
        response.status = 400
        return 'Unknown product. Valid products: ' + ', '.join(sorted(HRRR_PRODUCTS))
    # Bind to the matching allowlist key, so the value reused below is a constant
    # from our own set rather than raw request input.
    product = next(p for p in HRRR_PRODUCTS if p == product)
    if not time_param:
        response.status = 400
        return 'time parameter is required'
    print(f'[COG] raw time_param: {time_param!r}')
    datetime_object = datetime.fromisoformat(time_param.replace('Z', '+00:00'))
    date = datetime_object.strftime('%Y-%m-%d')
    hour = datetime_object.strftime('%H:00:00')
    hour_safe = hour.replace(':', '')
    print(f'[COG] parsed → date={date!r}, hour={hour!r}, hour_safe={hour_safe!r}')
    cache_dir = config.APP_CONFIG['CACHE_DIR']
    cog_path = _safe_path(cache_dir, f'hrrr-{product}-{date}T{hour_safe}-3857-cog.tif')

    try:
        if not os.path.exists(cog_path):
            cog_path = _ensure_3857_geotiff(product, date, hour, cache_dir)
        return static_file(os.path.basename(cog_path), root=cache_dir, mimetype='image/tiff')
    except (FileNotFoundError, ValueError):
        response.status = 404
        return f'No HRRR data available for {product} at the requested time'
    except Exception as e:
        print(f'[COG] error serving {product}: {e}')
        response.status = 500
        return 'Error serving COG'

@bottle_app.route('/cog/<product>/<time_param:path>')
def cog_path(product, time_param):
    if 'Z' in time_param:
        time_param = time_param[:time_param.index('Z') + 1]
    return _serve_cog(product, time_param)


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
    # (validated) value is written back for routing (path-injection guard, S2083).
    if '..' in path or not _ALLOWED_PATH_INFO.match(path):
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
    """
    The router matches a URL by counting the slash-separated parts and filling
    each named slot left-to-right. Adding the product route gave the older projwin route a same-length twin.
    With both routes registered, two different 4-part shapes existed:

        /<model>/<format>/<datetime>/<projwin>   (bbox, no product)
        /<model>/<product>/<format>/<datetime>   (product, no bbox)

    Bottle has no way to know which one a 4-part URL means, and it matched the
    product shape for both. So a bbox request:

        GET /gfs/gribjson/2024-03-05T00:00:00/-105,41,-104,40

    was read as product=gribjson, format=2024-03-05T00:00:00,
    datetime=-105,41,-104,40 -- the bbox landed in the <datetime> slot and 500'd
    when app.py called datetime.fromisoformat() on it. This hit every no-product
    bbox request (GFS/ECMWF always, since they have no product; and the bare
    HRRR /<model>/<format>/<datetime>/<projwin> form). The 5-part product+bbox
    route below was never ambiguous and stayed separate.

    Folding both 4-part shapes into this one handler removes the guess: a bbox is
    the only part that ever contains commas, so the comma in the final segment
    decides which shape it is.
    """
    if ',' in seg4:
        fmt, dt, projwin = seg2, seg3, seg4.split(',')
        if len(projwin) != 4:
            response.status = 400
            return 'Invalid projwin. Must be in format: ulx,uly,lrx,lry'
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
    else:
        response.status = 400
        data = 'Invalid projwin. Must be in format: ulx,uly,lrx,lry'
    return data


def main():
    # production
    if (os.path.exists('/certs/key.pem') and os.path.exists('/certs/cert.pem')):
        run(bottle_app,
            host='0.0.0.0',
            port=8104,
            server='gunicorn',
            timeout=60,
            keyfile='/certs/key.pem',
            certfile='/certs/cert.pem')
    else:
        # dev mode
        run(bottle_app,
            host='0.0.0.0',
            port=8104,
            server='gunicorn',
            workers=4,
            timeout=60,
            debug=True,
            reloader=True)


if __name__ == '__main__':
    main()
