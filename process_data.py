#!/usr/bin/env python3

import os
import argparse
import subprocess
import requests
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker
import rasterio
import hashlib
import io
import config
from datetime import datetime
from pyproj import Transformer
from herbie import Herbie
from ecmwfapi import ECMWFDataServer

HRRR_PRODUCT_COLORMAPS = {
    'wind_speed':    {'cmap': 'viridis',   'label': 'Wind Speed (m/s)'},
    'wind_u':        {'cmap': 'RdBu_r',   'label': 'Wind U Component (m/s)'},
    'wind_v':        {'cmap': 'RdBu_r',   'label': 'Wind V Component (m/s)'},
    'temp_2m':       {'cmap': 'RdYlBu_r', 'label': 'Temperature (K)'},
    'pbl_height':    {'cmap': 'plasma',   'label': 'PBL Height (m)'},
    'smoke_massden': {'cmap': 'YlOrRd',   'label': 'Smoke Mass Density (µg/m³)', 'scale': 1e9, 'vmin': 0, 'vmax': 250},
    'precip_rate':   {'cmap': 'Blues',    'label': 'Precip Rate (kg/m²/s)'},
}


def _render_colormap_png(grib_file, output_file, product):
    cmap_info = HRRR_PRODUCT_COLORMAPS.get(product, {'cmap': 'viridis', 'label': product})

    # Convert GRIB2 to GeoTIFF first so rasterio can reliably read it
    tmp_tif = output_file.replace('.png', '_tmp.tif')
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
    parsed_hour = hour.replace(':', '')
    cog_file = cache_dir + f'/hrrr-{product}-{date}T{parsed_hour}-3857-cog.tif'
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

    pid = os.getpid()
    tmp_tif = cache_dir + f'/hrrr-{product}-{date}T{parsed_hour}-3857-tmp-{pid}.tif'
    tmp_cog = cache_dir + f'/hrrr-{product}-{date}T{parsed_hour}-3857-cog-{pid}.tmp.tif'

    try:
        return _generate_cog(product, date, parsed_hour, grib_file, tmp_tif, tmp_cog, cog_file)
    except Exception as e:
        for f in [tmp_tif, tmp_cog]:
            if os.path.exists(f):
                os.remove(f)
        raise

def _generate_cog(product, date, parsed_hour, grib_file, tmp_tif, tmp_cog, cog_file):
    # Step 1: warp to EPSG:3857, Float32 with nodata
    subprocess.run([
        'gdalwarp',
        '-of', 'GTiff',
        '-t_srs', 'EPSG:3857',
        '-r', 'bilinear',
        '-ot', 'Float32',
        '-srcnodata', 'nan',
        '-dstnodata', '9999',
        '-co', 'TILED=YES',
        '-co', 'COMPRESS=LZW',
        '-co', 'BLOCKXSIZE=512',
        '-co', 'BLOCKYSIZE=512',
        grib_file, tmp_tif
    ], check=True)

    # For wind products: compute magnitude or extract U/V component
    if product in ('wind_speed', 'wind_u', 'wind_v'):
        tmp_mag = cache_dir + f'/hrrr-{product}-{date}T{parsed_hour}-3857-mag.tif'
        with rasterio.open(tmp_tif) as src:
            u = src.read(1).astype(np.float32)
            v = src.read(2).astype(np.float32) if src.count >= 2 else np.zeros_like(u)
            nodata = src.nodata
            mask = (u == nodata) | (v == nodata) if nodata is not None else np.zeros_like(u, dtype=bool)
            if product == 'wind_speed':
                band = np.sqrt(u**2 + v**2)
            elif product == 'wind_u':
                band = u
            else:
                band = v
            band[mask] = 9999
            profile = src.profile.copy()
        profile.update(count=1, nodata=9999)
        with rasterio.open(tmp_mag, 'w', **profile) as dst:
            dst.write(band, 1)
        os.remove(tmp_tif)
        os.rename(tmp_mag, tmp_tif)

    # Step 2: build overviews with bilinear resampling
    subprocess.run([
        'gdaladdo', '-r', 'bilinear',
        tmp_tif,
        '2', '4', '8', '16', '32'
    ], check=True)

    # Step 3: convert to COG (copies overviews into file header), write to temp path
    subprocess.run([
        'gdal_translate',
        '-of', 'GTiff',
        '-ot', 'Float32',
        '-a_nodata', '9999',
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


def render_wms_tile(model, product, iso_string, bbox, width, height, srs='EPSG:4326', transparent=True):
    datetime_object = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
    date = datetime_object.strftime('%Y-%m-%d')
    time_str = datetime_object.strftime('%H:%M:%S')
    hour = datetime_object.strftime('%H:00:00')

    if model != 'hrrr':
        raise ValueError(f'WMS not yet supported for model: {model}')

    cache_dir = config.APP_CONFIG['CACHE_DIR']
    tif_3857 = _ensure_3857_geotiff(product, date, hour, cache_dir)

    # Crop cached 3857 GeoTIFF to exactly the requested BBOX at WIDTH x HEIGHT
    minx, miny, maxx, maxy = bbox
    bbox_hash = hashlib.md5(f'{"".join(map(str,bbox))}{product}{srs}'.encode()).hexdigest()[:8]
    tmp_tif = cache_dir + f'/wms_tmp_{product}_{os.getpid()}.tif'

    subprocess.run([
        'gdal_translate',
        '-of', 'GTiff',
        '-projwin', str(minx), str(maxy), str(maxx), str(miny),
        '-outsize', str(width), str(height),
        tif_3857, tmp_tif
    ], check=True)

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
        alpha = src.read(src.count) if src.count > 1 else None

    if os.path.exists(tmp_tif):
        os.remove(tmp_tif)

    cmap_info = HRRR_PRODUCT_COLORMAPS.get(product, {'cmap': 'viridis', 'label': product})
    scale = cmap_info.get('scale', 1)
    data = data * scale

    vmin = cmap_info['vmin'] if 'vmin' in cmap_info else np.nanpercentile(data[np.isfinite(data)], 2)
    vmax = cmap_info['vmax'] if 'vmax' in cmap_info else np.nanpercentile(data[np.isfinite(data)], 98)
    norm = plt.Normalize(vmin=vmin, vmax=vmax)
    cmap_fn = plt.get_cmap(cmap_info['cmap'])
    rgba = cmap_fn(norm(data))

    if transparent:
        mask = np.isnan(data)
        if alpha is not None:
            mask = mask | (alpha == 0)
        rgba[mask] = (0, 0, 0, 0)

    from PIL import Image
    img_uint8 = (rgba * 255).astype(np.uint8)
    img = Image.fromarray(img_uint8, mode='RGBA')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf.read()


HRRR_PRODUCTS = {
    'wind_speed':    {'search': ':[U|V]GRD:10 m'},
    'wind_u':        {'search': ':[U|V]GRD:10 m'},
    'wind_v':        {'search': ':[U|V]GRD:10 m'},
    'temp_2m':       {'search': ':TMP:2 m above ground:'},
    'pbl_height':    {'search': ':HPBL:surface:'},
    'smoke_massden': {'search': ':MASSDEN:8 m above ground:'},
    'precip_rate':   {'search': ':PRATE:surface:'},
    'rh_2m':         {'search': ':RH:2 m above ground:'},
    'wind_gust':     {'search': ':GUST:surface:'},
    'dewpoint_2m':   {'search': ':DPT:2 m above ground:'},
    'visibility':    {'search': ':VIS:surface:'},
    'cape':          {'search': ':CAPE:surface:'},
    'lightning':     {'search': ':LTNG:entire atmosphere:'},
    'cloud_cover':   {'search': ':TCDC:entire atmosphere:'},
    'smoke_column':  {'search': ':COLMD:entire atmosphere'},
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

    print(f'Processing HRRR {product}')
    if projwin is None:
        projwin = [-180, 90, 180, -90]

    hour = datetime.strptime(time, '%H:%M:%S').strftime('%H:00:00')
    search = HRRR_PRODUCTS[product]['search']

    # Regrid to latlon
    regrid_file = output_dir + f'/hrrr-{product}-{date}T{hour}.grib2'
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
        projwin_string = '_'.join(map(str, projwin))
        subset_file = output_dir + f'/hrrr-{product}-{projwin_string}-{date}T{hour}.grib2'
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
            _render_colormap_png(output_grib, output_file, product)
            print('Created', output_file)
    else:
        return f'Unsupported format: {format}'

    return output_file

def process_ecmwf(projwin, date, time, output_dir, format):
    print('Processing ECMWF data')

    # Round down to the nearest 6th hour
    hour = '00:00:00'

    # Get GRIB from ECMWFDataServer
    download_file = output_dir + '/ecmwf-uv-' + date + 'T' + hour + '.grib'
    output_file = output_dir + '/ecmwf-uv-' + date + 'T' + hour + '.json'
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
        projwin_string = '_'.join(map(str, projwin))
        subset_file = output_dir + '/ecmwf-uv-' + \
            projwin_string + '-' + date + 'T' + hour + '.grib'
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
        projwin_string = '_'.join(map(str, projwin))
    else:
        projwin_string = 'global'

    # Round down to the nearest 6th hour
    time_obj = datetime.strptime(date + 'T' + time, "%Y-%m-%dT%H:%M:%S")
    rounded_hour = (time_obj.hour // 6) * 6
    hour = time_obj.replace(hour=rounded_hour).strftime('%H:00:00')

    # Download subsetted GFS GRIB file
    download_file = output_dir + '/gfs-' + projwin_string + '-' + date + 'T' + hour + '.grib'
    output_file = output_dir + '/gfs-' + projwin_string + '-' + date + 'T' + hour + '.json'
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
