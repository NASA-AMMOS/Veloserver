#!/usr/bin/env python3

import os
import argparse
import subprocess
import threading
import fcntl
import requests
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker
import rasterio
import config
from datetime import datetime
from herbie import Herbie
from ecmwfapi import ECMWFDataServer


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



HRRR_PRODUCT_COLORMAPS = {
    'wind_speed':    {'cmap': 'viridis',   'label': 'Wind Speed (m/s)'},
    'wind_u':        {'cmap': 'RdBu_r',   'label': 'Wind U Component (m/s)'},
    'wind_v':        {'cmap': 'RdBu_r',   'label': 'Wind V Component (m/s)'},
    'temp_2m':       {'cmap': 'RdYlBu_r', 'label': 'Temperature (K)'},
    'pbl_height':    {'cmap': 'plasma',   'label': 'PBL Height (m)'},
    'smoke_massden': {'cmap': 'YlOrRd',   'label': 'Smoke Mass Density (µg/m³)', 'scale': 1e9, 'vmin': 0, 'vmax': 250},
    'precip_rate':   {'cmap': 'Blues',    'label': 'Precip Rate (kg/m²/s)'},
}


def _create_png(grib_file, output_file, product):
    cmap_info = HRRR_PRODUCT_COLORMAPS.get(product, {'cmap': 'viridis', 'label': product})

    # Convert GRIB2 to GeoTIFF first (temp file confined to the output dir, S8707)
    tmp_tif = _safe_path(os.path.dirname(output_file),
                         os.path.basename(output_file).replace('.png', '_tmp.tif'))
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
        norm = plt.Normalize(vmin=vmin, vmax=vmax)
    cmap = plt.get_cmap(cmap_info['cmap'])
    rgba = cmap(norm(data))
    rgba[np.isnan(data)] = (0, 0, 0, 0)
    fig, ax = plt.subplots(figsize=(12, 6), dpi=150)
    ax.imshow(rgba, aspect='auto')
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cb = plt.colorbar(sm, ax=ax, label=cmap_info['label'], shrink=0.8)
    if cmap_info.get('log', False):
        cb.ax.yaxis.set_major_formatter(matplotlib.ticker.ScalarFormatter())
        cb.ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.set_title(cmap_info['label'])
    ax.axis('off')
    plt.tight_layout()
    plt.savefig(output_file, dpi=150, bbox_inches='tight', transparent=False)
    plt.close(fig)


def _ensure_3857_geotiff(product, date, hour, cache_dir):
    """Download native HRRR GRIB and produce a Cloud-Optimized GeoTIFF in EPSG:3857."""
    # Bind product to the matching allowlist key, so the value interpolated into
    # the file paths below comes from our own dict and never from raw request
    # input (path-injection hardening, Sonar S2083).
    if product not in HRRR_PRODUCTS:
        raise ValueError(f'Unknown product: {product}')
    product = next(p for p in HRRR_PRODUCTS if p == product)
    date = datetime.strptime(date, '%Y-%m-%d').strftime('%Y-%m-%d')  # validate/normalize
    parsed_hour = hour.replace(':', '')
    cog_file = _safe_path(cache_dir, f'hrrr-{product}-{date}T{parsed_hour}-3857-cog.tif')
    if os.path.exists(cog_file):
        return cog_file

    # Acquire a cross-process file lock before downloading/processing.
    # The in-memory threading.Lock only works within one gunicorn worker process.
    # With multiple workers, they race to download the same GRIB file causing
    # GDAL "not a supported file format" errors on partial reads.
    lock_path = _safe_path(cache_dir, f'hrrr-{product}-{date}T{parsed_hour}.lock')
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
        tmp_tif = _safe_path(cache_dir, f'hrrr-{product}-{date}T{parsed_hour}-3857-tmp-{uid}.tif')
        tmp_cog = _safe_path(cache_dir, f'hrrr-{product}-{date}T{parsed_hour}-3857-cog-{uid}.tmp.tif')

        try:
            return _generate_cog(product, date, parsed_hour, grib_file, tmp_tif, tmp_cog, cog_file, cache_dir)
        except Exception as e:
            for f in [tmp_tif, tmp_cog]:
                if os.path.exists(f):
                    os.remove(f)
            raise
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

def _generate_cog(product, date, parsed_hour, grib_file, tmp_tif, tmp_cog, cog_file, cache_dir):
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
        tmp_uvs = _safe_path(cache_dir, f'hrrr-{product}-{date}T{parsed_hour}-3857-uvs-{uid}.tif')
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
        tmp_scaled = _safe_path(cache_dir, f'hrrr-{product}-{date}T{parsed_hour}-3857-scaled-{uid}.tif')
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
    band_names = ['u', 'v', 'speed'] if product == 'winds' else [product]
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
        projwin = [-180, 90, 180, -90]

    hour = datetime.strptime(time, '%H:%M:%S').strftime('%H:00:00')
    date = datetime.strptime(date, '%Y-%m-%d').strftime('%Y-%m-%d')  # validate/normalize
    search = HRRR_PRODUCTS[product]['search']

    # Regrid to latlon
    regrid_file = _safe_path(output_dir, f'hrrr-{product}-{date}T{hour}.grib2')
    if not os.path.exists(regrid_file):
        H = Herbie(
            date + ' ' + hour,
            model="hrrr",
            fxx=0,
            save_dir=output_dir
        )
        download_file = str(H.download(search, verbose=True))
        print('Downloaded', download_file)
    if not os.path.exists(regrid_file):
        wgrib2_commands = ['wgrib2', download_file,
                           '-new_grid', 'latlon',
                           '-134:730:0.1', '21:310:0.1',
                           regrid_file]
        if product == 'winds':
            wgrib2_commands.insert(2, 'earth')
            wgrib2_commands.insert(2, '-new_grid_winds')
        print(' '.join(wgrib2_commands))
        subprocess.run(wgrib2_commands)

    # Subset if projwin provided
    output_grib = regrid_file
    if projwin != [-180, 90, 180, -90]:
        projwin_string = '_'.join(str(float(v)) for v in projwin)  # numeric only, no path chars (S2083)
        subset_file = _safe_path(output_dir, f'hrrr-{product}-{projwin_string}-{date}T{hour}.grib2')
        if not os.path.exists(subset_file):
            wgrib2_commands = ['wgrib2', regrid_file,
                               '-small_grib',
                               str(projwin[0]) + ':' + str(projwin[2]),
                               str(projwin[3]) + ':' + str(projwin[1]),
                               subset_file]
            print(' '.join(wgrib2_commands))
            subprocess.run(wgrib2_commands)
        output_grib = subset_file

    # Convert to requested format
    if format == 'gribjson':
        output_file = output_grib.replace('.grib2', '.json')
        if not os.path.exists(output_file):
            with open(output_file, 'w') as f:
                subprocess.run(['grib2json', '--names', '--data', '--fv', '10.0', output_grib],
                               stdout=f, text=True)
                print('Created', output_file)
    elif format == 'geotiff':
        output_file = output_grib.replace('.grib2', '.tif')
        if not os.path.exists(output_file):
            subprocess.run(['gdalwarp', '-of', 'GTiff', '-t_srs', 'EPSG:3857', output_grib, output_file])
            print('Created', output_file)
    elif format == 'png':
        output_file = output_grib.replace('.grib2', '.png')
        if not os.path.exists(output_file):
            _create_png(output_grib, output_file, product)
            print('Created', output_file)
    else:
        return f'Unsupported format: {format}'

    return output_file

def process_ecmwf(projwin, date, time, output_dir, format):
    print('Processing ECMWF data')

    # Round down to the nearest 6th hour
    hour = '00:00:00'
    date = datetime.strptime(date, '%Y-%m-%d').strftime('%Y-%m-%d')  # validate/normalize

    # Get GRIB from ECMWFDataServer
    download_file = _safe_path(output_dir, 'ecmwf-uv-' + date + 'T' + hour + '.grib')
    output_file = _safe_path(output_dir, 'ecmwf-uv-' + date + 'T' + hour + '.json')
    if not os.path.isfile(download_file):
        server = ECMWFDataServer()
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
            "target": download_file
        })
        print('Downloaded', download_file)

    # Subset GRIB file
    if projwin is not None:
        projwin_string = '_'.join(str(float(v)) for v in projwin)  # numeric only, no path chars (S2083)
        subset_file = _safe_path(output_dir,
                                 'ecmwf-uv-' + projwin_string + '-' + date + 'T' + hour + '.grib')
        if not os.path.exists(subset_file):
            wgrib2_commands = ['wgrib2',
                               download_file,
                               '-small_grib',
                               str(lon360(projwin[0])) +
                               ':' + str(lon360(projwin[2])),
                               str(projwin[3]) + ':' + str(projwin[1]),
                               subset_file]
            print(' '.join(wgrib2_commands))
            subprocess.run(wgrib2_commands)
        download_file = subset_file
        print('Subset file', subset_file)

    # Convert GRIB to JSON
    output_file = download_file.replace('.grib', '.json')
    if not os.path.exists(output_file):
        grib2json_commands = ['grib2json',
                              '--names',
                              '--data',
                              '--fv', '10.0',
                              download_file]
        with open(output_file, 'w') as f:
            print(' '.join(grib2json_commands))
            subprocess.run(grib2json_commands, stdout=f, text=True, timeout=60)
            print('Created', output_file)

    return output_file


def process_gfs(projwin, date, time, output_dir, format):
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
    download_file = _safe_path(output_dir, 'gfs-' + projwin_string + '-' + date + 'T' + hour + '.grib')
    output_file = _safe_path(output_dir, 'gfs-' + projwin_string + '-' + date + 'T' + hour + '.json')
    print('Checking for existing', download_file)
    if not os.path.isfile(download_file):
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
        response = requests.get(url)
        if response.status_code == 200:
            with open(download_file, 'wb') as f:
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
        with open(output_file, 'wb') as f:
            print(' '.join(grib2json_commands))
            subprocess.run(grib2json_commands, stdout=f, text=True)
            print('Created', output_file)

    return output_file


def process_user_defined(projwin, date, time, output_dir, format, definition):
    print('Processing user defined model')


def main():
    """
    Main function.
    """
    args = parse_arguments()
    print('Getting info for:', args.projwin, args.date,
          args.time, args.output_dir)

    if args.user_defined:
        process_user_defined(args.projwin, args.date, args.time,
                             args.output_dir, args.format, args.user_defined)
        return

    if args.model == 'hrrr':
        process_hrrr(args.product, args.projwin, args.date, args.time,
                     args.output_dir, args.format)
    elif args.model == 'ecmwf':
        process_ecmwf(args.projwin, args.date, args.time,
                      args.output_dir, args.format)
    elif args.model == 'gfs':
        process_gfs(args.projwin, args.date, args.time,
                    args.output_dir, args.format)
    else:
        print('Model is not supported.')


if __name__ == '__main__':
    main()