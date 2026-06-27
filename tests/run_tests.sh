#!/usr/bin/env bash
#
# Run the Veloserver test suite against a fresh disposable container (empty cache),
# streaming results live. Cleans up the container + cache volume on exit.
# Usage:
#   tests/run_tests.sh                      # full suite (functional + concurrency + stress)
#   tests/run_tests.sh tests/test_stress.py # run just one test file
#   KEEP=1 tests/run_tests.sh               # leave the container running afterwards
#   IMAGE=NASA-AMMOS/veloserver:latest tests/run_tests.sh   # use a different image
#   CACHE_MAX_BYTES=536870912 tests/run_tests.sh            # bigger budget (512MB)
#
# Budget sizing: 1GB is comfortably larger than the concurrent working set, so the
# LRU front holds the in-flight files (eviction only reclaims genuinely-old ones,
# as intended) -- yet the LRU-eviction test still fills past it to prove eviction.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-ghcr.io/nasa-ammos/veloserver:development}"
NAME="${NAME:-veloserver-test}"
VOL="${NAME}-cache"
BUDGET="${CACHE_MAX_BYTES:-1073741824}"  # 1GB; realistic — bigger than the working set
SUITE="${1:-tests/run_all.py}"

cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then
    echo "==> KEEP=1: leaving container '$NAME' running (remove with: docker rm -f $NAME)"
  else
    echo "==> cleaning up container + cache volume"
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm "$VOL" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "==> starting disposable test server"
echo "    image=$IMAGE  budget=$((BUDGET/1024/1024))MB  empty cache"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker volume rm "$VOL" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  -e CACHE_MAX_BYTES="$BUDGET" \
  -v "$PWD":/home/veloserver \
  -v "$VOL":/home/veloserver/cache \
  -w /home/veloserver \
  "$IMAGE" >/dev/null

echo "==> waiting for server to boot..."
for _ in $(seq 1 30); do
  if docker exec "$NAME" python3 -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8104', timeout=2)" \
      >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "==> running $SUITE (live)"
echo
# Unbuffered (-u) so each PASS/FAIL line streams as it happens. VELOSERVER_CACHE_DIR
# lets the suite empty the cache before the run; STRESS_BUDGET_BYTES matches the
# server budget so the LRU eviction assertions line up.
set +e
docker exec \
  -e VELOSERVER_CACHE_DIR=/home/veloserver/cache \
  -e STRESS_BUDGET_BYTES="$BUDGET" \
  "$NAME" python3 -u "$SUITE"
rc=$?
set -e

echo
echo "==> suite exit code: $rc  (0 = all passed)"
exit $rc
