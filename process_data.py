#!/usr/bin/env python3

import os
import argparse
import subprocess
import requests
from datetime import datetime
from herbie import Herbie
from ecmwfapi import ECMWFDataServer

from modules.parse import _safe_path, canonical_product, hrrr_format_error, normalize_date, projwin_to_string
from modules.concurrency import _atomic_output, _download_lock
from modules import convert
from config import HRRR_PRODUCTS

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

def _regrid_latlon(grib_path, out_path, winds=False):
    """Regrid a native HRRR GRIB onto the latlon grid. For winds, -new_grid_winds
    earth rotates the vectors to earth-relative first. Returns out_path."""
    if os.path.exists(out_path):
        return out_path
    with _atomic_output(out_path) as tmp:
        cmd = ['wgrib2', grib_path]
        if winds:
            cmd += ['-new_grid_winds', 'earth']
        cmd += ['-new_grid', 'latlon', '-134:730:0.1', '21:310:0.1', tmp]
        subprocess.run(cmd)
    return out_path


def _subset_grib(grib_path, out_path, lon_min, lon_max, lat_min, lat_max):
    """Subset a GRIB to a lon/lat box with wgrib2 -small_grib. The caller supplies
    the bounds in the grid's own convention (e.g. 0-360 lon). Returns out_path."""
    if os.path.exists(out_path):
        return out_path
    with _atomic_output(out_path) as tmp:
        subprocess.run(['wgrib2', grib_path, '-small_grib',
                        f'{lon_min}:{lon_max}', f'{lat_min}:{lat_max}', tmp])
    return out_path


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
        _regrid_latlon(download_file, regrid_file, winds=(product == 'winds'))
    return regrid_file


def _subset_hrrr(product, projwin, date, hour, output_dir, regrid_file):
    """Subset the regridded GRIB2 to projwin; return the GRIB to convert (the
    subset, or the full regrid when no projwin was given)."""
    if projwin == GLOBAL_PROJWIN:
        return regrid_file
    subset_file = _safe_path(output_dir, f'hrrr-{product}-{projwin_to_string(projwin)}-{date}T{hour}{EXT_GRIB2}')
    return _subset_grib(regrid_file, subset_file,
                        projwin[0], projwin[2], projwin[3], projwin[1])


def _convert_hrrr(output_grib, format, product):
    """Dispatch the GRIB to the requested format producer; return the output file
    path, or an error string for an unsupported format. The per-format work lives
    in modules.convert; here we just map format -> output filename."""
    if format == 'gribjson':
        return convert.to_gribjson(output_grib, output_grib.replace(EXT_GRIB2, EXT_JSON))
    elif format == 'geotiff':
        return convert.to_geotiff(output_grib, output_grib.replace(EXT_GRIB2, '.tif'))
    elif format == 'png':
        return convert.to_png(output_grib, output_grib.replace(EXT_GRIB2, '.png'), product)
    return f'Unsupported format: {format}'


def process_hrrr(product, projwin, date, time, output_dir, format):
    try:
        product = canonical_product(product)
    except ValueError as e:
        return str(e)

    format_error = hrrr_format_error(product, format)
    if format_error:
        return format_error

    print(f'Processing HRRR {product}')
    if projwin is None:
        projwin = GLOBAL_PROJWIN

    hour = datetime.strptime(time, '%H:%M:%S').strftime('%H:00:00')  # round to top of hour
    date = normalize_date(date)

    regrid_file = _regrid_hrrr(product, date, hour, output_dir)
    output_grib = _subset_hrrr(product, projwin, date, hour, output_dir, regrid_file)
    return _convert_hrrr(output_grib, format, product)


def _cog_name_prefix(product, date, hour):
    """Shared leading part of every HRRR COG cache filename (COG file + lock).
    One source of truth so the cache-hit check and the lock key agree. ``hour``
    colons are stripped for filesystem portability."""
    return f'hrrr-{product}-{date}T{hour.replace(":", "")}'


def _cog_filename(product, date, hour):
    """Canonical EPSG:3857 COG cache filename; shared by the writer and the
    cache-hit check, which must agree byte-for-byte."""
    return _cog_name_prefix(product, date, hour) + '-3857-cog.tif'


def _download_hrrr_native(product, date, hour, cache_dir):
    """Download the native-grid HRRR GRIB for the COG path, re-downloading once if
    the file is missing or unreadable by gdal (partial/corrupt fetch). Returns the
    GRIB path. product/date/hour are already validated at the request boundary."""
    search = HRRR_PRODUCTS[product]['search']
    H = Herbie(date + ' ' + hour, model='hrrr', fxx=0, save_dir=cache_dir)
    grib_file = str(H.download(search, verbose=False))
    gdalinfo_result = subprocess.run(['gdalinfo', grib_file], capture_output=True)
    if not os.path.exists(grib_file) or gdalinfo_result.returncode != 0:
        print(f'[COG] GRIB file missing or corrupt, re-downloading: {grib_file}')
        if os.path.exists(grib_file):
            os.remove(grib_file)
        grib_file = str(H.download(search, verbose=False))
    return grib_file


def ensure_cog(product, date, hour, cache_dir):
    """Return the EPSG:3857 COG path for an HRRR product/time, building it on a
    cache miss."""
    cog_file = _safe_path(cache_dir, _cog_filename(product, date, hour))
    if os.path.exists(cog_file):
        return cog_file
    # One cross-process builder per COG: the lock spans download AND generate so
    # two gunicorn workers can't both fetch and build the same COG
    with _download_lock(cache_dir, _cog_name_prefix(product, date, hour)):
        if os.path.exists(cog_file):
            return cog_file
        grib_file = _download_hrrr_native(product, date, hour, cache_dir)
        return convert.to_cog(grib_file, cog_file, product)

def process_ecmwf(projwin, date, output_dir):
    print('Processing ECMWF data')

    # Round down to the nearest 6th hour
    hour = '00:00:00'
    date = normalize_date(date)

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
        subset_file = _safe_path(output_dir,
                                 'ecmwf-uv-' + projwin_to_string(projwin) + '-' + date + 'T' + hour + EXT_GRIB)
        download_file = _subset_grib(download_file, subset_file,
                                     lon360(projwin[0]), lon360(projwin[2]),
                                     projwin[3], projwin[1])
        print('Subset file', subset_file)

    # Convert GRIB to JSON
    output_file = download_file.replace(EXT_GRIB, EXT_JSON)
    return convert.to_gribjson(download_file, output_file, timeout=60)


def process_gfs(projwin, date, time, output_dir):
    print('Processing GFS data')
    if projwin is not None:
        projwin_string = projwin_to_string(projwin)
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

    # Convert GRIB to JSON. Guard against an empty download so we don't emit empty
    # JSON; the cache-hit short-circuit lives in convert.to_gribjson.
    print('Checking for existing', output_file)
    if os.path.getsize(download_file) > 0:
        convert.to_gribjson(download_file, output_file)
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
