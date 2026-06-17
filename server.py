import os
import io
import threading
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.crs import CRS
from datetime import datetime, timezone, timedelta
from bottle import Bottle, run, request, response, static_file
from app import App
from process_data import render_wms_tile, _ensure_3857_geotiff
import config

bottle_app = Bottle()
dataApp = App()

def _create_placeholder_cog(cache_dir):
    path = os.path.join(cache_dir, 'placeholder.tif')
    if os.path.exists(path):
        return path
    data = np.full((16, 16), 9999, dtype=np.float32)
    transform = from_bounds(-20037508.34, -20037508.34, 20037508.34, 20037508.34, 16, 16)
    with rasterio.open(path, 'w', driver='GTiff', height=16, width=16,
                       count=1, dtype='float32', crs=CRS.from_epsg(3857),
                       transform=transform, nodata=9999) as dst:
        dst.write(data, 1)
    return path

_placeholder_path = None



def _serve_cog(product, time_param):
    default_time = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%dT00:00:00Z')
    time_param = time_param or default_time
    datetime_object = datetime.fromisoformat(time_param.replace('Z', '+00:00'))
    date = datetime_object.strftime('%Y-%m-%d')
    hour = datetime_object.strftime('%H:00:00')
    cache_dir = config.APP_CONFIG['CACHE_DIR']
    hour_safe = hour.replace(':', '')
    cog_path = cache_dir + f'/hrrr-{product}-{date}T{hour_safe}-3857-cog.tif'

    try:
        if not os.path.exists(cog_path):
            cog_path = _ensure_3857_geotiff(product, date, hour, cache_dir)
        return static_file(os.path.basename(cog_path), root=cache_dir, mimetype='image/tiff')
    except (FileNotFoundError, ValueError) as e:
        response.status = 404
        return f'No HRRR data available for {product} at {time_param}'
    except Exception as e:
        response.status = 500
        return f'Error serving COG: {e}'

@bottle_app.route('/cog')
def cog():
    product = request.query.get('product', 'temp_2m')
    time_param = request.query.get('time', None)
    return _serve_cog(product, time_param)

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
    request.environ['PATH_INFO'] = request.environ['PATH_INFO'].rstrip('/')


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
    (output_format, data) = dataApp.get_data(request,
                                             model,
                                             format,
                                             datetime,
                                             None)
    response.content_type = output_format
    return data


@bottle_app.route('/<model>/<format>/<datetime>/<projwin>')
@enable_cors
def get_data_projwin(model, format, datetime, projwin):
    projwin = projwin.split(',')
    if len(projwin) == 4:
        (output_format, data) = dataApp.get_data(request,
                                                 model,
                                                 format,
                                                 datetime,
                                                 projwin)
        response.content_type = output_format
    else:
        data = 'Invalid projwin. Must be in format: ulx,uly,lrx,lry'
    return data
@bottle_app.route('/<model>/<product>/<format>/<datetime>')
@enable_cors
def get_data_product(model, product, format, datetime):
    (output_format, data) = dataApp.get_data(request, model, format, datetime, None, product)
    response.content_type = output_format
    return data


@bottle_app.route('/<model>/<product>/<format>/<datetime>/<projwin>')
@enable_cors
def get_data_product_projwin(model, product, format, datetime, projwin):
    projwin = projwin.split(',')
    if len(projwin) == 4:
        (output_format, data) = dataApp.get_data(request, model, format, datetime, projwin, product)
        response.content_type = output_format
    else:
        data = 'Invalid projwin. Must be in format: ulx,uly,lrx,lry'
    return data

@bottle_app.route('/wms')
@enable_cors
def wms():
    req_type = request.query.get('REQUEST', request.query.get('request', ''))
    if req_type.lower() == 'getcapabilities':
        response.content_type = 'application/xml'
        return '''<?xml version="1.0"?>
<WMS_Capabilities version="1.1.0">
  <Service><Name>Veloserver WMS</Name></Service>
  <Capability><Request><GetMap><Format>image/png</Format></GetMap></Request></Capability>
</WMS_Capabilities>'''

    if req_type.lower() != 'getmap':
        response.status = 400
        return 'Only GetMap and GetCapabilities are supported'

    bbox  = request.query.get('BBOX', request.query.get('bbox', ''))
    width  = int(request.query.get('WIDTH', request.query.get('width', 256)))
    height = int(request.query.get('HEIGHT', request.query.get('height', 256)))
    layers = request.query.get('LAYERS', request.query.get('layers', 'hrrr:temp_2m'))
    time   = request.query.get('TIME', request.query.get('time', ''))
    srs    = request.query.get('SRS', request.query.get('srs',
             request.query.get('CRS', request.query.get('crs', 'EPSG:4326'))))
    transparent = request.query.get('TRANSPARENT', 'TRUE').upper() == 'TRUE'

    if not bbox:
        response.status = 400
        return 'BBOX parameter is required'

    try:
        minx, miny, maxx, maxy = [float(v) for v in bbox.split(',')]
    except ValueError:
        response.status = 400
        return 'Invalid BBOX format. Expected minx,miny,maxx,maxy'

    parts = layers.split(':')
    model = parts[0] if len(parts) > 0 else 'hrrr'
    product = parts[1] if len(parts) > 1 else 'temp_2m'

    if not time:
        time = datetime.now(timezone.utc).strftime('%Y-%m-%dT00:00:00Z')

    try:
        png_bytes = render_wms_tile(model, product, time, [minx, miny, maxx, maxy], width, height, srs, transparent)
        response.content_type = 'image/png'
        return png_bytes
    except Exception as e:
        response.status = 500
        return f'Error rendering tile: {e}'


def main():
    # production
    if (os.path.exists('/certs/key.pem') and os.path.exists('/certs/cert.pem')):
        run(bottle_app,
            host='0.0.0.0',
            port=8104,
            server='gunicorn',
            timeout=300,
            keyfile='/certs/key.pem',
            certfile='/certs/cert.pem')
    else:
        # dev mode
        run(bottle_app,
            host='0.0.0.0',
            port=8104,
            server='gunicorn',
            workers=4,
            timeout=300,
            debug=True,
            reloader=True)


if __name__ == '__main__':
    main()
