# Anemoserver - Wind Data Services

### Quick Start

Build
```
docker build --platform=linux/amd64 -t NASA-AMMOS/anemoserver:latest .
```

Run
```
docker run --rm -p 8104:8104 -v ~/.ecmwfapirc:/root/.ecmwfapirc/ --name anemoserver NASA-AMMOS/anemoserver:latest
```

With an external cache directory:
```
docker run --rm -p 8104:8104 -v ~/.ecmwfapirc:/root/.ecmwfapirc/ -v $(pwd)/cache:/home/wind_server/cache --name anemoserver NASA-AMMOS/anemoserver:latest
```

To use ECMWF data, you need to have an ECMWF account with your key added to `~/.ecmwfapirc`.

