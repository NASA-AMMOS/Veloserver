import os

# Defines the server configuration

APP_CONFIG = {
    'CACHE_DIR': os.path.relpath('./cache'),
    'CACHE_FILES': True,
    'CACHE_MAX_BYTES': int(os.environ.get('CACHE_MAX_BYTES', 10 * 1024 ** 3)),  # 10 GB
    'CACHE_TTL_HOURS': int(os.environ.get('CACHE_TTL_HOURS', 0)),
    'CACHE_TARGET_RATIO': float(os.environ.get('CACHE_TARGET_RATIO', 0.85)),
    'AVAILABLE_FORMATS': {
        "json": "application/json",
        "png": "image/png",
        "tiff": "image/tiff"
    },
    'DEFAULT_FORMAT': "application/json"
}
