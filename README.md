# Veloserver - Velocity Data Visualization Services

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

To use ECMWF data, you need to have an ECMWF account with your key added to `~/.ecmwfapirc`.

