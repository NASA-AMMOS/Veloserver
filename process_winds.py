#!/usr/bin/env python3

import os
import argparse
import subprocess
import requests
from datetime import datetime
from herbie import Herbie
from ecmwfapi import ECMWFDataServer


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


def process_hrrr(projwin, date, time, output_dir, format):
    print('Processing HRRR data')
    if projwin is None:
        projwin = [-180, 90, 180, -90]

    # Use just the hour
    hour = datetime.strptime(time, '%H:%M:%S').strftime('%H:00:00')

    # Get HRRR data
    H = Herbie(
        date + ' ' + hour,  # model run date/time
        model="hrrr",  # model name
        fxx=0,  # forecast lead time
        save_dir=output_dir
    )

    download_file = str(H.download(r":[U|V]GRD:10 m", verbose=True))
    print('Downloaded', download_file)

    regrid_file = output_dir + '/hrrr-uv-' + date + 'T' + hour + '.grib2'
    if not os.path.exists(regrid_file):
        wgrib2_commands = ['wgrib2',
                           download_file,
                           '-new_grid_winds', 'earth',
                           '-new_grid', 'latlon',
                           '-134:730:0.1', '21:310:0.1',
                           regrid_file]
        print(' '.join(wgrib2_commands))
        subprocess.run(wgrib2_commands)
    output_grib = regrid_file
    print('Reproject file', regrid_file)

    # Subset GRIB file
    if projwin != [-180, 90, 180, -90]:
        projwin_string = '_'.join(map(str, projwin))
        subset_file = output_dir + '/hrrr-uv-' + \
            projwin_string + '-' + date + 'T' + hour + '.grib2'
        if not os.path.exists(subset_file):
            wgrib2_commands = ['wgrib2',
                               regrid_file,
                               '-small_grib',
                               str(projwin[0]) + ':' + str(projwin[2]),
                               str(projwin[3]) + ':' + str(projwin[1]),
                               subset_file]
            print(' '.join(wgrib2_commands))
            subprocess.run(wgrib2_commands)
        output_grib = subset_file
        print('Subset file', subset_file)

    # Convert GRIB to JSON
    output_file = output_grib.replace('.grib2', '.json')
    if not os.path.exists(output_file):
        grib2json_commands = ['grib2json',
                              '--names',
                              '--data',
                              '--fv', '10.0',
                              output_grib]
        with open(output_file, 'w') as f:
            print(' '.join(grib2json_commands))
            subprocess.run(grib2json_commands, stdout=f, text=True)
            print('Created', output_file)

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

    if args.format != 'gribjson':
        print('Only gribjson format is currently supported. ' +
              'Support for geojson, geotiff, and png is under development')
        return

    if args.user_defined:
        process_user_defined(args.projwin, args.date, args.time,
                             args.output_dir, args.format, args.user_defined)
        return

    if args.model == 'hrrr':
        process_hrrr(args.projwin, args.date, args.time,
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
