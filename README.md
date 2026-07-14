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

With SSL certs:
```
docker run --rm -p 8104:8104 -v ~/.ecmwfapirc:/root/.ecmwfapirc/ -v $(pwd)/certs:/certs --name veloserver NASA-AMMOS/veloserver:latest
```

With an external cache directory:
```
docker run --rm -p 8104:8104 -v ~/.ecmwfapirc:/root/.ecmwfapirc/ -v $(pwd)/certs:/certs -v $(pwd)/cache:/home/veloserver/cache --name veloserver NASA-AMMOS/veloserver:latest
```

To use ECMWF data, you need to have an ECMWF account with an appropriate [licence](https://www.ecmwf.int/en/forecasts/accessing-forecasts/licences-available) and your key added to `~/.ecmwfapirc`.

### Usage

Default server port is `8104`. Data is fetched on demand from the upstream model, converted to the requested format, cached, and returned; repeat requests for the same product/time/area/forecast-hour are served from cache. A trailing `/` is optional on every route.

Most requests are structured in the form:

`/<model>/<product>/<format>/<datetime>/<projwin>`

`product` and `projwin` are optional, which gives the following routes:

| Route | Returns |
|-------|---------|
| `/<model>/<format>/<datetime>` | Global data for the default product (`winds`) |
| `/<model>/<format>/<datetime>/<projwin>` | Data subset to a bounding box |
| `/<model>/<product>/<format>/<datetime>` | Global data for a specific product |
| `/<model>/<product>/<format>/<datetime>/<projwin>` | A specific product, subset to a bounding box |

There is also one dedicated route for Cloud-Optimized GeoTIFFs, which is HRRR only and takes no `model` or `format`:

`/cog/<product>/<datetime>` returns an EPSG:3857 Cloud-Optimized GeoTIFF for the HRRR product.

#### Path parameters

**model**

| Model | Coverage | Products | Formats | Notes |
|-------|----------|----------|---------|-------|
| `hrrr` | CONUS | `winds` + many more (see below) | `gribjson`, `geotiff`, `png`, COG | Supports forecast hours via `fxx` |
| `gfs` | Global | `winds` | `gribjson` | Rounded to the nearest 6-hourly run |
| `ecmwf` | Global | `winds` | `gribjson` | Requires an ECMWF licence + `~/.ecmwfapirc` |

**product**

Applies to HRRR. If you leave it out of the route, it defaults to `winds`.

| Product | Description |
|---------|-------------|
| `winds` | 10 m U/V wind vectors. This is the velocity / streamline layer. |
| `temp_2m` | 2 m temperature |
| `dewpoint_2m` | 2 m dewpoint |
| `rh_2m` | 2 m relative humidity |
| `wind_gust` | Surface wind gust |
| `pbl_height` | Planetary boundary-layer height |
| `precip_rate` | Surface precipitation rate |
| `smoke_massden` | Near-surface smoke mass density |

**format**

| Format | Description | Availability |
|--------|-------------|--------------|
| `gribjson` | Vector JSON of U/V components for animated streamlines | `winds` only from any model |
| `geotiff` | Latlon GeoTIFF raster | any supported HRRR product |
| `png` | Colored PNG raster, per-product colormap | any supported HRRR product |
| COG | EPSG:3857 Cloud-Optimized GeoTIFF, via the `/cog` route | any supported HRRR product |

Only `winds` supports `gribjson`. For any other product use `geotiff`, `png`, or the `/cog` route. Requesting `gribjson` for another product returns `400`.

**datetime**

ISO 8601, for example `2025-01-01T06:00:00Z`. A bare date such as `2025-01-01` means `00:00:00`. Each model rounds the time to its own run schedule: HRRR hourly, GFS every 6 hours, ECMWF 00z.

**projwin**

A bounding box, written `ulx,uly,lrx,lry` (upper-left lon/lat, lower-right lon/lat), for example `-118,34,-117,33`. Leave it out of the route to get the full grid.

#### Query parameters

**fxx**

Applies to HRRR. The forecast hour: how many hours ahead of the run's start time the data is valid for. F00 is the analysis hour, the model's best estimate of conditions at the run time itself; F06, F12, and so on are the model's forecast that many hours into the future. If you leave `fxx` out, you get F00.

Every HRRR run forecasts out to F18. The four runs each day at 00z, 06z, 12z, and 18z are extended runs that go all the way to F48; every other run stops at F18. So F00 to F18 works for any `datetime`, but F19 to F48 works only when the `datetime` hour is 00, 06, 12, or 18.

`fxx` is a query param, so it works on both the data routes and `/cog`, for example `?fxx=6`. A value that is out of range for the run, or not an integer, returns `400`.

### Sample Requests

**Winds (velocity / streamline layer)**

- Global HRRR winds at midnight: http://localhost:8104/hrrr/gribjson/2025-01-01T00:00:00Z
- HRRR winds over Southern California: http://localhost:8104/hrrr/winds/gribjson/2025-01-01T00:00:00Z/-118,34,-117,33
- Global GFS winds: http://localhost:8104/gfs/gribjson/2025-01-01T00:00:00Z
- ECMWF winds over a box (06:00 UTC): http://localhost:8104/ecmwf/gribjson/2025-01-01T06:00:00Z/-118,34,-117,33

**HRRR forecast hours (`fxx`)**

Default (no `fxx`):

- Analysis hour (F00), returned when `fxx` is left off: http://localhost:8104/hrrr/gribjson/2025-01-01T12:00:00Z

Standard runs (any hour other than 00/06/12/18z, valid F00 to F18):

- F6 from the 09z run: http://localhost:8104/hrrr/gribjson/2025-01-01T09:00:00Z?fxx=6
- F18 (the maximum) from the 13z run: http://localhost:8104/hrrr/gribjson/2025-01-01T13:00:00Z?fxx=18

Extended runs (00, 06, 12, and 18z, valid F00 to F48):

- F48 from the 00z run: http://localhost:8104/hrrr/gribjson/2025-01-01T00:00:00Z?fxx=48
- F48 from the 06z run: http://localhost:8104/hrrr/gribjson/2025-01-01T06:00:00Z?fxx=48
- F48 from the 12z run: http://localhost:8104/hrrr/gribjson/2025-01-01T12:00:00Z?fxx=48
- F48 from the 18z run: http://localhost:8104/hrrr/gribjson/2025-01-01T18:00:00Z?fxx=48

Out of range (returns `400`):

- F48 from the 13z run, a standard run that stops at F18: http://localhost:8104/hrrr/gribjson/2025-01-01T13:00:00Z?fxx=48
- F24 from the 09z run, past F18: http://localhost:8104/hrrr/gribjson/2025-01-01T09:00:00Z?fxx=24

**HRRR raster products (`png`)**

- 2 m temperature: http://localhost:8104/hrrr/temp_2m/png/2025-01-01T00:00:00Z
- Relative humidity: http://localhost:8104/hrrr/rh_2m/png/2025-01-01T00:00:00Z
- Wind gust, 6-hour forecast: http://localhost:8104/hrrr/wind_gust/png/2025-01-01T00:00:00Z?fxx=6
- Smoke during the Jan 2025 LA fires: http://localhost:8104/hrrr/smoke_massden/png/2025-01-08T21:00:00Z

**HRRR raster products (`geotiff`)**

- 2 m temperature: http://localhost:8104/hrrr/temp_2m/geotiff/2025-01-01T00:00:00Z
- PBL height: http://localhost:8104/hrrr/pbl_height/geotiff/2025-01-01T00:00:00Z
- Dewpoint, 3-hour forecast: http://localhost:8104/hrrr/dewpoint_2m/geotiff/2025-01-01T00:00:00Z?fxx=3
- Smoke during the Jan 2025 LA fires: http://localhost:8104/hrrr/smoke_massden/geotiff/2025-01-08T21:00:00Z

**Cloud-Optimized GeoTIFFs (`/cog`)**

- Winds: http://localhost:8104/cog/winds/2025-01-01T00:00:00Z
- 2 m temperature: http://localhost:8104/cog/temp_2m/2025-01-01T00:00:00Z
- PBL height: http://localhost:8104/cog/pbl_height/2025-01-01T00:00:00Z
- Smoke: http://localhost:8104/cog/smoke_massden/2025-01-01T00:00:00Z
- 2 m temperature, 12-hour forecast: http://localhost:8104/cog/temp_2m/2025-01-01T00:00:00Z?fxx=12
