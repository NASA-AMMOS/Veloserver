import os

# Defines the server configuration

APP_CONFIG = {
    'CACHE_DIR': os.path.relpath('./cache'),
    'CACHE_FILES': True,
    'AVAILABLE_FORMATS': {
        "json": "application/json",
        "tiff": "image/tiff"
    },
    'DEFAULT_FORMAT': "application/json"
}
