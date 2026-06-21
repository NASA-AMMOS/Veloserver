import os
from datetime import datetime, timezone
from bottle import Bottle, run, request, response, static_file
from app import App
from process_rasters import _ensure_3857_geotiff
import config

bottle_app = Bottle()
dataApp = App()


def _serve_cog(product, time_param):
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
