# Veloserver - Velocity Data Visualization Server

Veloserver is a geospatial data visualization server designed to dynamically return velocity data such as winds and ocean currents from weather models and other datasets. It returns the data in formats optimized for visualizing in a web client including animated vector streamlines via gribjson. It includes built-in caching to reduce requests to external data sources and deliver quick results.

### Quick Start

Build
```
docker build --platform=linux/amd64 -t NASA-AMMOS/veloserver:latest .
```

Run
```
docker run --rm -p 8104:8104 -v ~/.ecmwfapirc:/root/.ecmwfapirc/ --name veloserver NASA-AMMOS/veloserver:latest
```

With an external cache directory:
```
docker run --rm -p 8104:8104 -v ~/.ecmwfapirc:/root/.ecmwfapirc/ -v $(pwd)/cache:/home/veloserver/cache --name veloserver NASA-AMMOS/veloserver:latest
```

To use ECMWF data, you need to have an ECMWF account with an appropriate [licence](https://www.ecmwf.int/en/forecasts/accessing-forecasts/licences-available) and your key added to `~/.ecmwfapirc`.


### Usage

Default server port is 8104. REST requests are structured in the form:

`/<model>/<format>/<datetime>/<projwin>`

**model** - Available options: `gribjson`, `geojson`, `geotiff`, `png`

**format** - Available options: `ecmwf`, `gfs`, `hrrr`

**datetime** - In ISO8601 format: `YYYY-MM-DDThh:mm:ssZ`

**projwin** - Bounding box window in format: `ulx,uly,lrx,lry`


### Sample Requests:

HRRR global wind data for 2025-01-01 at midnight: http://localhost:8104/hrrr/gribjson/2025-01-01/

ECMWF localized wind data for 2025-01-01 at 06:00:00 UTC: http://localhost:8104/ecmwf/gribjson/2025-01-01T06:00:00Z/-118,34,-117,33/
