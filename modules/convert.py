"""Format producers: turn a (possibly subsetted) GRIB into the requested output
format. Each to_* function is idempotent -- it returns the existing file on a
cache hit, otherwise writes it atomically and returns the path. gdal/grib2json
commands are kept inline here (rather than behind a gdal wrapper) so the exact
flags stay visible at the point of use."""
import os
import subprocess
import threading
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.cm
import matplotlib.colors
import matplotlib.ticker
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
import rasterio

from modules.parse import _safe_path
from modules.concurrency import _atomic_output
from config import HRRR_PRODUCTS, WINDS_BAND_COLORMAPS

# These producers receive paths the caller already built from validated tokens and
# confined with _safe_path, so they trust their inputs (clean-at-the-boundary):
# the request layer cleans once, the workers don't re-clean.


def to_gribjson(grib_path, out_path, timeout=None):
    """Convert a GRIB to grib2json. Returns out_path (existing or freshly built).
    timeout bounds the grib2json subprocess when set (used for the ECMWF feed)."""
    if os.path.exists(out_path):
        return out_path
    with _atomic_output(out_path) as tmp:
        with open(tmp, 'w') as f:
            subprocess.run(['grib2json', '--names', '--data', '--fv', '10.0', grib_path],
                           stdout=f, text=True, timeout=timeout)
    print('Created', out_path)
    return out_path


def to_geotiff(grib_path, out_path):
    """Reproject a GRIB to an EPSG:3857 GeoTIFF. Returns out_path."""
    if os.path.exists(out_path):
        return out_path
    with _atomic_output(out_path) as tmp:
        subprocess.run(['gdalwarp', '-of', 'GTiff', '-t_srs', 'EPSG:3857', grib_path, tmp])
    print('Created', out_path)
    return out_path


def to_png(grib_path, out_path, product):
    """Render a GRIB to a colorized PNG visualization. Returns out_path."""
    if os.path.exists(out_path):
        return out_path
    with _atomic_output(out_path) as tmp:
        _create_png(grib_path, tmp, product)
    print('Created', out_path)
    return out_path


def to_cog(grib_path, out_path, product):
    """Produce the EPSG:3857 Cloud-Optimized GeoTIFF. Returns out_path (existing or
    freshly built); published atomically. The expensive download+build is serialized
    by the caller's lock (process_data.ensure_cog); this stays a pure producer."""
    if os.path.exists(out_path):
        return out_path
    with _atomic_output(out_path) as tmp:
        _create_cog(grib_path, tmp, product)
    print(f'Created COG {out_path}')
    return out_path


def _create_cog(grib_path, output_file, product):
    """Reproject a GRIB to EPSG:3857, derive bands (u/v/speed for winds, kg/m^3 ->
    µg/m^3 for smoke), build overviews, and write the COG to output_file. Manages
    its own intermediate raster (confined to output_file's dir, S8707)."""
    base = os.path.dirname(output_file)
    stem = os.path.basename(output_file)
    uid = f'{os.getpid()}-{threading.get_ident()}'
    tmp_tif = _safe_path(base, f'{stem}-warp-{uid}.tif')
    try:
        # Step 1: warp to EPSG:3857, Float32 with nodata.
        subprocess.run([
            'gdalwarp', '-of', 'GTiff', '-t_srs', 'EPSG:3857', '-r', 'near',
            '-ot', 'Float32', '-srcnodata', 'nan', '-dstnodata', '-9999',
            '-co', 'TILED=YES', '-co', 'COMPRESS=LZW',
            '-co', 'BLOCKXSIZE=512', '-co', 'BLOCKYSIZE=512',
            grib_path, tmp_tif
        ], check=True)

        if product == 'winds':
            tmp_uvs = _safe_path(base, f'{stem}-uvs-{uid}.tif')
            with rasterio.open(tmp_tif) as src:
                u = src.read(1).astype(np.float32)
                v = src.read(2).astype(np.float32) if src.count >= 2 else np.zeros_like(u)
                nodata = src.nodata
                mask = (u == nodata) | (v == nodata) if nodata is not None else np.zeros_like(u, dtype=bool)
                speed = np.sqrt(u**2 + v**2)
                u[mask] = -9999
                v[mask] = -9999
                speed[mask] = -9999
                profile = src.profile.copy()
            profile.update(count=3, nodata=-9999)
            with rasterio.open(tmp_uvs, 'w', **profile) as dst:
                dst.write(u, 1)
                dst.write(v, 2)
                dst.write(speed, 3)
            os.remove(tmp_tif)
            os.rename(tmp_uvs, tmp_tif)

        elif product == 'smoke_massden':
            # HRRR near-surface smoke (MASSDEN) is native kg/m^3; convert to the
            # conventional µg/m^3 to make it interpretable with other pm 2.5 products.
            tmp_scaled = _safe_path(base, f'{stem}-scaled-{uid}.tif')
            with rasterio.open(tmp_tif) as src:
                band = src.read(1).astype(np.float32)
                nodata = src.nodata
                mask = (band == nodata) if nodata is not None else np.zeros_like(band, dtype=bool)
                band = band * 1e9
                band[mask] = -9999
                profile = src.profile.copy()
            profile.update(count=1, nodata=-9999)
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

        # Step 2: build overviews with nearest-neighbor to keep meaning at the pixel level.
        subprocess.run(['gdaladdo', '-r', 'nearest', tmp_tif, '2', '4', '8', '16', '32'], check=True)

        # Step 3: convert to the COG, written into output_file (to_cog's atomic temp).
        subprocess.run([
            'gdal_translate', '-of', 'GTiff',
            '-co', 'TILED=YES', '-co', 'COMPRESS=LZW', '-co', 'COPY_SRC_OVERVIEWS=YES',
            '-co', 'BLOCKXSIZE=512', '-co', 'BLOCKYSIZE=512',
            tmp_tif, output_file
        ], check=True)
    finally:
        if os.path.exists(tmp_tif):
            os.remove(tmp_tif)


def _create_png(grib_file, output_file, product):
    cmap_info = HRRR_PRODUCTS.get(product, {'cmap': 'viridis', 'label': product})

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
