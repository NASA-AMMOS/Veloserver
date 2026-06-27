#!/usr/bin/env python3

import os
import argparse
import subprocess
import threading
import fcntl
import contextlib
import requests
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.cm
import matplotlib.colors
import matplotlib.ticker
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
import rasterio
from datetime import datetime
from herbie import Herbie
from ecmwfapi import ECMWFDataServer

# File extensions reused when deriving cache/output filenames. Centralized so the
# literals aren't duplicated across the processing functions (Sonar S1192).
EXT_GRIB = '.grib'
EXT_GRIB2 = '.grib2'
EXT_JSON = '.json'

# Whole-globe bounds; a projwin equal to this means "no spatial subset".
GLOBAL_PROJWIN = [-180, 90, 180, -90]

# (connect, read) timeout for outbound HTTP downloads. The read value bounds the
# gap between bytes, not the whole transfer, so a slow-but-progressing download
# still completes while a wedged connection fails fast. Matters under threaded
# workers, where gunicorn's worker timeout no longer reaps a single hung request.
_HTTP_TIMEOUT = (10, 60)


def _safe_path(base_dir, filename):
    """Join filename under base_dir and refuse anything that escapes it.

    Cache/output filenames are built from values that can come from CLI args
    (agentic use) or HTTP requests. Confining the constructed path inside its
    base directory stops a '..'-laden component from reading or writing
    arbitrary files (path-injection hardening, Sonar S2083 / S8707).
    """
    base = os.path.abspath(base_dir)
    resolved = os.path.abspath(os.path.join(base, filename))
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ValueError(f'Refusing path outside {base_dir!r}: {filename!r}')
    return resolved


@contextlib.contextmanager
def _atomic_output(final_path):
    """Write to a unique temp file, then atomically rename it into place.

    wgrib2/gdal/grib2json write incrementally to their output path, so two
    concurrent identical requests -- or a reader hitting the os.path.exists cache
    check mid-write -- could otherwise see a half-written file. Renaming a
    per-process temp into the final path makes the cached file appear only once
    complete (os.replace is atomic on POSIX). Temp confined under the same dir
    (path-injection hardening, Sonar S8707)."""
    uid = f'{os.getpid()}-{threading.get_ident()}'
    tmp = _safe_path(os.path.dirname(final_path),
                     f'{os.path.basename(final_path)}.{uid}.tmp')
    try:
        yield tmp
        if os.path.exists(tmp):
            os.replace(tmp, final_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


@contextlib.contextmanager
def _download_lock(cache_dir, key):
    """Cross-process lock so concurrent identical requests don't fetch the same
    source file at once (duplicate downloads / partial-file reads). An in-memory
    threading.Lock would not span gunicorn worker processes, hence fcntl. The key
    is built only from allowlisted/numeric/date tokens (S2083)."""
    lock_file = open(_safe_path(cache_dir, f'{key}.lock'), 'w')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()



HRRR_PRODUCT_COLORMAPS = {
    # Per-product colormaps, keyed by product name, used by the PNG renderer.
    'winds':         {'cmap': 'viridis',   'label': 'Wind Speed (m/s)'},
    'wind_gust':     {'cmap': 'viridis',   'label': 'Wind Gust (m/s)'},
    'temp_2m':       {'cmap': 'RdYlBu_r',  'label': 'Temperature (K)'},
    'dewpoint_2m':   {'cmap': 'RdYlBu_r',  'label': 'Dewpoint (K)'},
    'rh_2m':         {'cmap': 'YlGnBu',    'label': 'Relative Humidity (%)'},
    'pbl_height':    {'cmap': 'plasma',    'label': 'PBL Height (m)'},
    'smoke_massden': {'cmap': 'YlOrRd',    'label': 'Smoke Mass Density (µg/m³)', 'scale': 1e9, 'vmin': 0, 'vmax': 250},
    'precip_rate':   {'cmap': 'Blues',     'label': 'Precip Rate (kg/m²/s)'},
}

# Colormap for each band of the 3-band winds COG, keyed by the band name written
# into the COG and read back by _generate_cog, so the band names and their colormaps
# live in one place and can't drift. u and v are signed so they use a diverging map,
# speed is magnitude so it uses a sequential one.
WINDS_BAND_COLORMAPS = {
    'u':     {'cmap': 'RdBu_r',  'label': 'Wind U Component (m/s)'},
    'v':     {'cmap': 'RdBu_r',  'label': 'Wind V Component (m/s)'},
    'speed': {'cmap': 'viridis', 'label': 'Wind Speed (m/s)'},
}


def _create_png(grib_file, output_file, product):
    cmap_info = HRRR_PRODUCT_COLORMAPS.get(product, {'cmap': 'viridis', 'label': product})

    # Convert GRIB2 to GeoTIFF first (temp file confined to the output dir, S8707).
    # The uid keeps concurrent renders of the same product from sharing one temp.
    uid = f'{os.getpid()}-{threading.get_ident()}'
    tmp_tif = _safe_path(os.path.dirname(output_file),
                         f'{os.path.basename(output_file)}-{uid}_tmp.tif')
    subprocess.run(['gdal_translate', '-of', 'GTiff', grib_file, tmp_tif], check=True)

    with rasterio.open(tmp_tif) as src:
        nodata = src.nodata
        if product == 'winds' and src.count >= 2:
            u = src.read(1).astype(float)
            v = src.read(2).astype(float)
            if nodata is not None:
                u = np.where(u == nodata, np.nan, u)
                v = np.where(v == nodata, np.nan, v)
            data = np.sqrt(u**2 + v**2)
        else:
            data = src.read(1).astype(float)
            if nodata is not None:
                data = np.where(data == nodata, np.nan, data)

    os.remove(tmp_tif)

    scale = cmap_info.get('scale', 1)
    data = data * scale

    vmin = cmap_info['vmin'] if 'vmin' in cmap_info else np.nanpercentile(data, 2)
    vmax = cmap_info['vmax'] if 'vmax' in cmap_info else np.nanpercentile(data, 98)
    if cmap_info.get('log', False):
        data = np.where(data <= 0, np.nan, data)
        norm = matplotlib.colors.LogNorm(vmin=vmin, vmax=vmax)
    else:
        norm = matplotlib.colors.Normalize(vmin=vmin, vmax=vmax)
    cmap = matplotlib.colormaps[cmap_info['cmap']]
    rgba = cmap(norm(data))
    rgba[np.isnan(data)] = (0, 0, 0, 0)

    # Object-oriented matplotlib (Figure + an explicit Agg canvas) instead of the
    # pyplot global state. pyplot keeps a process-wide figure registry and is not
    # thread-safe, so two renders on two threads of one worker could clobber each
    # other's figure; a standalone Figure has none of that shared state.
    fig = Figure(figsize=(12, 6), dpi=150)
    FigureCanvasAgg(fig)
    ax = fig.subplots()
    ax.imshow(rgba, aspect='auto')
    sm = matplotlib.cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cb = fig.colorbar(sm, ax=ax, label=cmap_info['label'], shrink=0.8)
    if cmap_info.get('log', False):
        cb.ax.yaxis.set_major_formatter(matplotlib.ticker.ScalarFormatter())
        cb.ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.set_title(cmap_info['label'])
    ax.axis('off')
    fig.tight_layout()
    # output_file is the atomic-write temp path ending in .tmp, so matplotlib can't
    # infer the image type from the extension; state it explicitly.
    fig.savefig(output_file, format='png', dpi=150, bbox_inches='tight', transparent=False)


def _hrrr_cog_name_prefix(product, date, hour):
    """Shared leading part of every HRRR COG cache filename (COG, lock, temps).
    One source of truth so the cache-hit check and writer can't disagree.
    ``hour`` colons are stripped here for filesystem portability."""
    return f'hrrr-{product}-{date}T{hour.replace(":", "")}'


def _cog_filename(product, date, hour):
    """Canonical EPSG:3857 COG cache filename; shared by the writer and the
    server's cache-hit check, which must agree byte-for-byte."""
    return _hrrr_cog_name_prefix(product, date, hour) + '-3857-cog.tif'


def _ensure_3857_geotiff(product, date, hour, cache_dir):
    """Download native HRRR GRIB and produce a Cloud-Optimized GeoTIFF in EPSG:3857."""
    # Bind product to the matching allowlist key, so the value interpolated into
    # the file paths below comes from our own dict and never from raw request
    # input (path-injection hardening, Sonar S2083).
    if product not in HRRR_PRODUCTS:
        raise ValueError(f'Unknown product: {product}')
    product = next(p for p in HRRR_PRODUCTS if p == product)
    date = datetime.strptime(date, '%Y-%m-%d').strftime('%Y-%m-%d')  # validate/normalize
    name_prefix = _hrrr_cog_name_prefix(product, date, hour)
    cog_file = _safe_path(cache_dir, _cog_filename(product, date, hour))
    if os.path.exists(cog_file):
        return cog_file

    # Acquire a cross-process file lock before downloading/processing.
    # The in-memory threading.Lock only works within one gunicorn worker process.
    # With multiple workers, they race to download the same GRIB file causing
    # GDAL "not a supported file format" errors on partial reads.
    lock_path = _safe_path(cache_dir, f'{name_prefix}.lock')
    lock_file = open(lock_path, 'w')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        if os.path.exists(cog_file):
            return cog_file

        search = HRRR_PRODUCTS[product]['search']
        H = Herbie(
            date + ' ' + hour,
            model='hrrr',
            fxx=0,
            save_dir=cache_dir
        )
        grib_file = str(H.download(search, verbose=False))

        gdalinfo_result = subprocess.run(['gdalinfo', grib_file], capture_output=True)
        if not os.path.exists(grib_file) or gdalinfo_result.returncode != 0:
            print(f'[COG] GRIB file missing or corrupt, re-downloading: {grib_file}')
            if os.path.exists(grib_file):
                os.remove(grib_file)
            grib_file = str(H.download(search, verbose=False))

        uid = f'{os.getpid()}-{threading.get_ident()}'
        tmp_tif = _safe_path(cache_dir, f'{name_prefix}-3857-tmp-{uid}.tif')
        tmp_cog = _safe_path(cache_dir, f'{name_prefix}-3857-cog-{uid}.tmp.tif')

        try:
            return _generate_cog(product, name_prefix, grib_file, tmp_tif, tmp_cog, cog_file, cache_dir)
        except Exception:
            for f in [tmp_tif, tmp_cog]:
                if os.path.exists(f):
                    os.remove(f)
            raise
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

def _generate_cog(product, name_prefix, grib_file, tmp_tif, tmp_cog, cog_file, cache_dir):
    # Bind product to its allowlist key before it's interpolated into the temp
    # file paths below, so the value can't come from raw request input (S2083).
    if product not in HRRR_PRODUCTS:
        raise ValueError(f'Unknown product: {product}')
    product = next(p for p in HRRR_PRODUCTS if p == product)
    # Step 1: warp to EPSG:3857, Float32 with nodata
    subprocess.run([
        'gdalwarp',
        '-of', 'GTiff',
        '-t_srs', 'EPSG:3857',
        '-r', 'near',
        '-ot', 'Float32',
        '-srcnodata', 'nan',
        '-dstnodata', '9999',
        '-co', 'TILED=YES',
        '-co', 'COMPRESS=LZW',
        '-co', 'BLOCKXSIZE=512',
        '-co', 'BLOCKYSIZE=512',
        grib_file, tmp_tif
    ], check=True)

    if product == 'winds':
        uid = f'{os.getpid()}-{threading.get_ident()}'
        tmp_uvs = _safe_path(cache_dir, f'{name_prefix}-3857-uvs-{uid}.tif')
        with rasterio.open(tmp_tif) as src:
            u = src.read(1).astype(np.float32)
            v = src.read(2).astype(np.float32) if src.count >= 2 else np.zeros_like(u)
            nodata = src.nodata
            mask = (u == nodata) | (v == nodata) if nodata is not None else np.zeros_like(u, dtype=bool)
            speed = np.sqrt(u**2 + v**2)
            u[mask] = 9999
            v[mask] = 9999
            speed[mask] = 9999
            profile = src.profile.copy()
        profile.update(count=3, nodata=9999)
        with rasterio.open(tmp_uvs, 'w', **profile) as dst:
            dst.write(u, 1)
            dst.write(v, 2)
            dst.write(speed, 3)
        os.remove(tmp_tif)
        os.rename(tmp_uvs, tmp_tif)

    elif product == 'smoke_massden':
        # HRRR near-surface smoke (MASSDEN) is native kg/m^3; convert to the
        # conventional µg/m^3 to make it interpretable with other pm 2.5 products.
        uid = f'{os.getpid()}-{threading.get_ident()}'
        tmp_scaled = _safe_path(cache_dir, f'{name_prefix}-3857-scaled-{uid}.tif')
        with rasterio.open(tmp_tif) as src:
            band = src.read(1).astype(np.float32)
            nodata = src.nodata
            mask = (band == nodata) if nodata is not None else np.zeros_like(band, dtype=bool)
            band = band * 1e9
            band[mask] = 9999
            profile = src.profile.copy()
        profile.update(count=1, nodata=9999)
        with rasterio.open(tmp_scaled, 'w', **profile) as dst:
            dst.write(band, 1)
        os.remove(tmp_tif)
        os.rename(tmp_scaled, tmp_tif)

    # Label bands so the COG is self-describing for downstream consumers
    # (gdal_translate carries these descriptions through into the final COG).
    band_names = list(WINDS_BAND_COLORMAPS) if product == 'winds' else [product]
    with rasterio.open(tmp_tif, 'r+') as dst:
        for i, name in enumerate(band_names, start=1):
            if i <= dst.count:
                dst.set_band_description(i, name)

    # Step 2: build overviews with nearest-neighbor (keep true model values at
    # lower zooms too; no blending). Switch to 'average' if zoomed-out looks noisy.
    subprocess.run([
        'gdaladdo', '-r', 'nearest',
        tmp_tif,
        '2', '4', '8', '16', '32'
    ], check=True)

    # Step 3: convert to COG (copies overviews into file header), write to temp path
    subprocess.run([
        'gdal_translate',
        '-of', 'GTiff',
        '-co', 'TILED=YES',
        '-co', 'COMPRESS=LZW',
        '-co', 'COPY_SRC_OVERVIEWS=YES',
        '-co', 'BLOCKXSIZE=512',
        '-co', 'BLOCKYSIZE=512',
        tmp_tif, tmp_cog
    ], check=True)

    os.remove(tmp_tif)
    if not os.path.exists(cog_file):
        os.rename(tmp_cog, cog_file)
    else:
        os.remove(tmp_cog)
    print(f'Created COG {cog_file}')
    return cog_file

HRRR_PRODUCTS = {
    'winds':         {'search': ':[U|V]GRD:10 m'},
    'temp_2m':       {'search': ':TMP:2 m above ground:'},
    'pbl_height':    {'search': ':HPBL:surface:'},
    'smoke_massden': {'search': ':MASSDEN:8 m above ground:'},
    'precip_rate':   {'search': ':PRATE:surface:'},
    'rh_2m':         {'search': ':RH:2 m above ground:'},
    'wind_gust':     {'search': ':GUST:surface:'},
    'dewpoint_2m':   {'search': ':DPT:2 m above ground:'},
}
def parse_arguments():
    """
    Parses command-line arguments.

    Returns:
        argparse.Namespace: Parsed command-line arguments.
    """
    parser = argparse.ArgumentParser(description="Subset wind data from selected models and generate visualization ready outputs.")
    parser.add_argument('-p', '--projwin', type=float, required=False,
                        default=None, nargs=4, help='<ulx> <uly> <lrx> <lry>')
    parser.add_argument('-d', '--date', type=str, required=True,
                        help='str year-month-day e.g., "2024-03-05"')
    parser.add_argument('-t', '--time', type=str, required=False,
                        default='00:00:00', help='hour_rounded: e.g., 19:00:00')
    parser.add_argument('-o', '--output_dir', type=str,
                        required=False, default='./output', help='Output directory')
    parser.add_argument('-m', '--model', type=str, required=False,
                        default='hrrr', help='Model name (ecmwf, gfs, hrrr)')
    parser.add_argument('-f', '--format', type=str, required=False,
                        default='gribjson', help='Output file format (gribjson, geojson, geotiff, png)')
    parser.add_argument('-r', '--product', type=str, required=False,
                        default='winds', help=f'Product name. Available: {list(HRRR_PRODUCTS.keys())}')
    parser.add_argument('-u', '--user_defined', type=str, required=False,
                        help='Path to user defined model configuration')
    
    return parser.parse_args()


def lon360(lon):
    """
    Convert a longitude from -180 to 180 range to 0 to 360 range.
    """
    if not isinstance(lon, (int, float)):
        lon = float(lon)
    return (lon + 360) % 360 if lon < 0 else lon


def _regrid_hrrr(product, date, hour, output_dir):
    """Download native HRRR and regrid it to a latlon GRIB2; return its path."""
    regrid_file = _safe_path(output_dir, f'hrrr-{product}-{date}T{hour}{EXT_GRIB2}')
    if os.path.exists(regrid_file):
        return regrid_file

    with _download_lock(output_dir, f'hrrr-{product}-{date}T{hour}'):
        if os.path.exists(regrid_file):  # built by another worker while we waited
            return regrid_file
        H = Herbie(date + ' ' + hour, model="hrrr", fxx=0, save_dir=output_dir)
        download_file = str(H.download(HRRR_PRODUCTS[product]['search'], verbose=True))
        print('Downloaded', download_file)

        with _atomic_output(regrid_file) as tmp:
            wgrib2_commands = ['wgrib2', download_file,
                               '-new_grid', 'latlon',
                               '-134:730:0.1', '21:310:0.1',
                               tmp]
            if product == 'winds':
                wgrib2_commands.insert(2, 'earth')
                wgrib2_commands.insert(2, '-new_grid_winds')
            print(' '.join(wgrib2_commands))
            subprocess.run(wgrib2_commands)
    return regrid_file


def _subset_hrrr(product, projwin, date, hour, output_dir, regrid_file):
    """Subset the regridded GRIB2 to projwin; return the GRIB to convert (the
    subset, or the full regrid when no projwin was given)."""
    if projwin == GLOBAL_PROJWIN:
        return regrid_file
    projwin_string = '_'.join(str(float(v)) for v in projwin)  # numeric only, no path chars (S2083)
    subset_file = _safe_path(output_dir, f'hrrr-{product}-{projwin_string}-{date}T{hour}{EXT_GRIB2}')
    if not os.path.exists(subset_file):
        with _atomic_output(subset_file) as tmp:
            wgrib2_commands = ['wgrib2', regrid_file,
                               '-small_grib',
                               str(projwin[0]) + ':' + str(projwin[2]),
                               str(projwin[3]) + ':' + str(projwin[1]),
                               tmp]
            print(' '.join(wgrib2_commands))
            subprocess.run(wgrib2_commands)
    return subset_file


def _convert_hrrr(output_grib, format, product):
    """Convert the GRIB to the requested format; return the file path, or an
    error string for an unsupported format."""
    if format == 'gribjson':
        output_file = output_grib.replace(EXT_GRIB2, EXT_JSON)
        if not os.path.exists(output_file):
            with _atomic_output(output_file) as tmp:
                with open(tmp, 'w') as f:
                    subprocess.run(['grib2json', '--names', '--data', '--fv', '10.0', output_grib],
                                   stdout=f, text=True)
            print('Created', output_file)
    elif format == 'geotiff':
        output_file = output_grib.replace(EXT_GRIB2, '.tif')
        if not os.path.exists(output_file):
            with _atomic_output(output_file) as tmp:
                subprocess.run(['gdalwarp', '-of', 'GTiff', '-t_srs', 'EPSG:3857', output_grib, tmp])
            print('Created', output_file)
    elif format == 'png':
        output_file = output_grib.replace(EXT_GRIB2, '.png')
        if not os.path.exists(output_file):
            with _atomic_output(output_file) as tmp:
                _create_png(output_grib, tmp, product)
            print('Created', output_file)
    else:
        return f'Unsupported format: {format}'
    return output_file


def process_hrrr(product, projwin, date, time, output_dir, format):
    if product not in HRRR_PRODUCTS:
        return f'Unknown product: {product}. Available: {list(HRRR_PRODUCTS.keys())}'
    # Use the allowlist key (not the raw arg) in the file paths below (S2083).
    product = next(p for p in HRRR_PRODUCTS if p == product)

    if format == 'gribjson' and product != 'winds':
        return (f"gribjson is only available for the 'winds' product; "
                f"use geotiff, png, or the /cog route for '{product}'.")

    print(f'Processing HRRR {product}')
    if projwin is None:
        projwin = GLOBAL_PROJWIN

    hour = datetime.strptime(time, '%H:%M:%S').strftime('%H:00:00')
    date = datetime.strptime(date, '%Y-%m-%d').strftime('%Y-%m-%d')  # validate/normalize

    regrid_file = _regrid_hrrr(product, date, hour, output_dir)
    output_grib = _subset_hrrr(product, projwin, date, hour, output_dir, regrid_file)
    return _convert_hrrr(output_grib, format, product)

def process_ecmwf(projwin, date, output_dir):
    print('Processing ECMWF data')

    # Round down to the nearest 6th hour
    hour = '00:00:00'
    date = datetime.strptime(date, '%Y-%m-%d').strftime('%Y-%m-%d')  # validate/normalize

    # Get GRIB from ECMWFDataServer
    download_file = _safe_path(output_dir, 'ecmwf-uv-' + date + 'T' + hour + EXT_GRIB)
    with _download_lock(output_dir, 'ecmwf-uv-' + date + 'T' + hour):
        if not os.path.isfile(download_file):  # re-check inside the lock
            server = ECMWFDataServer()
            with _atomic_output(download_file) as tmp:
                server.retrieve({
                    "class": "s2",
                    "dataset": "s2s",
                    "date": date + "/to/" + date,
                    "expver": "prod",
                    "levtype": "sfc",
                    "model": "glob",
                    "origin": "ecmf",
                    "param": "165/166",
                    "step": "0",
                    "stream": "enfo",
                    "time": hour,
                    "type": "cf",
                    "grid": "0.1/0.1",
                    "target": tmp
                })
            print('Downloaded', download_file)

    # Subset GRIB file
    if projwin is not None:
        projwin_string = '_'.join(str(float(v)) for v in projwin)  # numeric only, no path chars (S2083)
        subset_file = _safe_path(output_dir,
                                 'ecmwf-uv-' + projwin_string + '-' + date + 'T' + hour + EXT_GRIB)
        if not os.path.exists(subset_file):
            with _atomic_output(subset_file) as tmp:
                wgrib2_commands = ['wgrib2',
                                   download_file,
                                   '-small_grib',
                                   str(lon360(projwin[0])) +
                                   ':' + str(lon360(projwin[2])),
                                   str(projwin[3]) + ':' + str(projwin[1]),
                                   tmp]
                print(' '.join(wgrib2_commands))
                subprocess.run(wgrib2_commands)
        download_file = subset_file
        print('Subset file', subset_file)

    # Convert GRIB to JSON
    output_file = download_file.replace(EXT_GRIB, EXT_JSON)
    if not os.path.exists(output_file):
        grib2json_commands = ['grib2json',
                              '--names',
                              '--data',
                              '--fv', '10.0',
                              download_file]
        with _atomic_output(output_file) as tmp:
            with open(tmp, 'w') as f:
                print(' '.join(grib2json_commands))
                subprocess.run(grib2json_commands, stdout=f, text=True, timeout=60)
        print('Created', output_file)

    return output_file


def process_gfs(projwin, date, time, output_dir):
    print('Processing GFS data')
    if projwin is not None:
        projwin_string = '_'.join(str(float(v)) for v in projwin)  # numeric only, no path chars (S2083)
    else:
        projwin_string = 'global'

    # Round down to the nearest 6th hour
    time_obj = datetime.strptime(date + 'T' + time, "%Y-%m-%dT%H:%M:%S")
    rounded_hour = (time_obj.hour // 6) * 6
    hour = time_obj.replace(hour=rounded_hour).strftime('%H:00:00')

    # Download subsetted GFS GRIB file (date already validated by strptime above)
    download_file = _safe_path(output_dir, 'gfs-' + projwin_string + '-' + date + 'T' + hour + EXT_GRIB)
    output_file = _safe_path(output_dir, 'gfs-' + projwin_string + '-' + date + 'T' + hour + EXT_JSON)
    print('Checking for existing', download_file)
    with _download_lock(output_dir, 'gfs-' + projwin_string + '-' + date + 'T' + hour):
        if not os.path.isfile(download_file):  # re-check inside the lock
            url_base = 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?'
            url_middle = 'dir=%2Fgfs.' + time_obj.strftime('%Y%m%d') + '%2F' + \
                         f'{rounded_hour:02d}' + '%2Fatmos&file=gfs.t' + \
                         f'{rounded_hour:02d}' + 'z.pgrb2.0p25.f000'
            url_end = '&var_TMP=on&var_UGRD=on&var_VGRD=on&lev_10_m_above_ground=on'
            if projwin is not None:
                subregion = '&subregion=&toplat=' + str(projwin[1]) + \
                            '&leftlon=' + str(lon360(projwin[0])) + \
                            '&rightlon=' + str(lon360(projwin[2])) + \
                            '&bottomlat=' + str(projwin[3])
                url_end = url_end + subregion
            url = url_base + url_middle + url_end
            print('Downloading', url)
            response = requests.get(url, timeout=_HTTP_TIMEOUT)
            if response.status_code == 200:
                with _atomic_output(download_file) as tmp:
                    with open(tmp, 'wb') as f:
                        f.write(response.content)
                print('Downloaded', download_file)
            else:
                return f'Error retrieving data: {url} - {response.status_code}'

    # Convert GRIB to JSON
    print('Checking for existing', output_file)
    if not os.path.exists(output_file) and os.path.getsize(download_file) > 0:
        grib2json_commands = ['grib2json',
                              '--names',
                              '--data',
                              '--fv', '10.0',
                              download_file]
        with _atomic_output(output_file) as tmp:
            with open(tmp, 'wb') as f:
                print(' '.join(grib2json_commands))
                subprocess.run(grib2json_commands, stdout=f, text=True)
        print('Created', output_file)

    return output_file


def process_user_defined(definition):
    print(f'Processing user defined model: {definition}')


def main():
    """
    Main function.
    """
    args = parse_arguments()
    print('Getting info for:', args.projwin, args.date,
          args.time, args.output_dir)

    if args.user_defined:
        process_user_defined(args.user_defined)
        return

    if args.model == 'hrrr':
        process_hrrr(args.product, args.projwin, args.date, args.time,
                     args.output_dir, args.format)
    elif args.model == 'ecmwf':
        process_ecmwf(args.projwin, args.date, args.output_dir)
    elif args.model == 'gfs':
        process_gfs(args.projwin, args.date, args.time, args.output_dir)
    else:
        print('Model is not supported.')


if __name__ == '__main__':
    main()