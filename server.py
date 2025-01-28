import os
from bottle import Bottle, run, request, response, static_file
from app import App

bottle_app = Bottle()
dataApp = App()


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


def main():
    # production
    if (os.path.exists('/certs/key.pem') and os.path.exists('/certs/cert.pem')):
        run(bottle_app,
            host='0.0.0.0',
            port=8104,
            server='gunicorn',
            keyfile='/certs/key.pem',
            certfile='/certs/cert.pem')
    else:
        # dev mode
        run(bottle_app,
            host='0.0.0.0',
            port=8104,
            server='gunicorn',
            debug=True,
            reloader=True)


if __name__ == '__main__':
    main()
