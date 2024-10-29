#!/usr/bin/env python3

import os
import argparse
import subprocess
from herbie import Herbie
from ecmwfapi import ECMWFDataServer


def parse_arguments():
    """
    Parses command-line arguments.

    Returns:
        argparse.Namespace: Parsed command-line arguments.
    """
    parser = argparse.ArgumentParser(description="Subset wind data from selected models and generate visualization ready outputs.")
    parser.add_argument('-p', '--projwin', type=float, required=False, default=None, nargs=4, help='<ulx> <uly> <lrx> <lry>')
    parser.add_argument('-d', '--date', type=str, required=True, help='str year-month-day e.g., "2024-03-05"')
    parser.add_argument('-t', '--time', type=str, required=False, default='00:00:00', help='hour_rounded: e.g., 19:00:00')
    parser.add_argument('-o', '--output_dir', type=str, required=False, default='./output', help='Output directory')
    parser.add_argument('-m', '--model', type=str, required=False, default='hrrr', help='Model name (hrrr, ecmwf)')
    parser.add_argument('-f', '--format', type=str, required=False, default='gribjson', help='Output file format (gribjson, geojson, geotiff)')

    return parser.parse_args()


def lon360(lon):
    """
    Convert a longitude from -180 to 180 range to 0 to 360 range.
    """
    return (lon + 360) % 360 if lon < 0 else lon


def process_hrrr(projwin, date, time, output_dir, format):
    print('Download HRRR data')
    if projwin is None:
        projwin = [-180, 90, 180, -90]

    # Get HRRR data
    H = Herbie(
        date + ' ' + time,  # model run date/time
        model="hrrr",  # model name
        fxx=0,  # forecast lead time
        save_dir=output_dir
    )

    download_file = str(H.download(r":[U|V]GRD:10 m", verbose=True))
    print('Downloaded', download_file)

    regrid_file = output_dir + '/hrrr-uv-' + date + '.grib2'
    wgrib2_commands = ['./wgrib2',
                       download_file,
                       '-new_grid_winds', 'earth',
                       '-new_grid', 'latlon',
                       '-134:730:0.1', '21:310:0.1',
                       regrid_file]
    print(' '.join(wgrib2_commands))
    subprocess.run(wgrib2_commands)
    output_grib = regrid_file
    print('Reproject file', regrid_file)

    if projwin != [-180, 90, 180, -90]:
        projwin_string = '_'.join(map(str, projwin))
        subset_file = output_dir + '/hrrr-uv-' + projwin_string + '-' + date + '.grib2'
        wgrib2_commands = ['./wgrib2',
                           regrid_file,
                           '-small_grib',
                           str(projwin[0]) + ':' + str(projwin[2]),
                           str(projwin[3]) + ':' + str(projwin[1]),
                           subset_file]
        print(' '.join(wgrib2_commands))
        subprocess.run(wgrib2_commands)
        output_grib = subset_file
        print('Subset file', subset_file)

    output_file = output_grib.replace('.grib2', '.json')
    grib2json_commands = ['./grib2json',
                          '--names',
                          '--data',
                          '--fv', '10.0',
                          output_grib]
    with open(output_file, 'w') as f:
        print(' '.join(grib2json_commands))
        subprocess.run(grib2json_commands, stdout=f, text=True)
        print('Created', output_file)


def process_ecmwf(projwin, date, time, output_dir, format):
    print('Downloading ECMWF data')

    download_file = output_dir + '/ecmwf-uv-' + date + '.grib'
    output_file = output_dir + '/ecmwf-uv-' + date + '.json'
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
            "time": "00:00:00",
            "type": "cf",
            "grid": "0.1/0.1",
            "target": download_file
        })
        print('Downloaded', download_file)

    if projwin is not None:
        projwin_string = '_'.join(map(str, projwin))
        subset_file = output_dir + '/ecmwf-uv-' + projwin_string + '-' + date + '.grib'
        wgrib2_commands = ['./wgrib2',
                           download_file,
                           '-small_grib',
                           str(lon360(projwin[0])) + ':' + str(lon360(projwin[2])),
                           str(projwin[3]) + ':' + str(projwin[1]),
                           subset_file]
        print(' '.join(wgrib2_commands))
        subprocess.run(wgrib2_commands)
        download_file = subset_file
        print('Subset file', subset_file)

    output_file = download_file.replace('.grib', '.json')
    grib2json_commands = ['./grib2json',
                          '--names',
                          '--data',
                          '--fv', '10.0',
                          download_file]
    with open(output_file, 'w') as f:
        print(' '.join(grib2json_commands))
        subprocess.run(grib2json_commands, stdout=f, text=True)
        print('Created', output_file)


def main():
    """
    Main function.
    """
    args = parse_arguments()
    print('Getting info for', args.projwin, args.date, args.time, args.model, args.output_dir)

    if args.model == 'hrrr':
        process_hrrr(args.projwin, args.date, args.time, args.output_dir, args.format)
    elif args.model == 'ecmwf':
        process_ecmwf(args.projwin, args.date, args.time, args.output_dir, args.format)
    else:
        print('Model is not supported.')


if __name__ == '__main__':
    main()
