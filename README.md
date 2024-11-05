# Wind Server

### Quick Start

Build
```
docker build --platform=linux/amd64 -t NASA-AMMOS/wind_server:latest .
```

Run
```
docker run --rm -p 8104:8104 -v ~/.ecmwfapirc:/root/.ecmwfapirc/ --name wind NASA-AMMOS/wind_server:latest`
```

To use ECMWF data, you need to have an ECMWF account with your key added to `~/.ecmwfapirc`.

