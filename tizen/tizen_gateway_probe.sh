#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./tizen/tizen_gateway_probe.sh --host <device-ip-or-host>

Options:
  --host HOST            Gateway host or IP reachable from the machine running this script.
  --port PORT            Gateway port. Default: 42617
  --serial SERIAL        Optional SDB device serial.
  --service-name NAME    Service name. Default: zeroclaw
  --token TOKEN          Existing bearer token. Skips auto-pair.
  --show-logs            Print recent service logs before probing.
  --help                 Show this help text.

Behavior:
  - probes GET /health from the host machine
  - if pairing is enabled and no token is supplied:
      - extracts the latest pairing code from target service logs
      - POSTs /pair from the host machine
      - uses the returned bearer token for API requests
  - probes GET /api/status and GET /api/health
  - falls back to on-device /health probing via sdb shell when host reachability fails
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

log() {
  printf '==> %s\n' "$*"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

trim_crlf() {
  tr -d '\r'
}

json_get() {
  local key="$1"
  python3 - "$key" <<'PY'
import json
import sys

key = sys.argv[1]
data = json.load(sys.stdin)
value = data
for part in key.split("."):
    if isinstance(value, dict):
        value = value.get(part)
    else:
        value = None
        break
if isinstance(value, bool):
    print("true" if value else "false")
elif value is None:
    print("")
else:
    print(value)
PY
}

HOST=""
PORT="42617"
DEVICE_SERIAL=""
SERVICE_NAME="zeroclaw"
TOKEN=""
SHOW_LOGS=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --host)
      [ "$#" -ge 2 ] || die "--host requires a value"
      HOST="$2"
      shift 2
      ;;
    --port)
      [ "$#" -ge 2 ] || die "--port requires a value"
      PORT="$2"
      shift 2
      ;;
    --serial)
      [ "$#" -ge 2 ] || die "--serial requires a value"
      DEVICE_SERIAL="$2"
      shift 2
      ;;
    --service-name)
      [ "$#" -ge 2 ] || die "--service-name requires a value"
      SERVICE_NAME="$2"
      shift 2
      ;;
    --token)
      [ "$#" -ge 2 ] || die "--token requires a value"
      TOKEN="$2"
      shift 2
      ;;
    --show-logs)
      SHOW_LOGS=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[ -n "$HOST" ] || die "--host is required"

need_cmd curl
need_cmd sdb
need_cmd python3

declare -a SDB_CMD
SDB_CMD=(sdb)
if [ -n "$DEVICE_SERIAL" ]; then
  SDB_CMD+=(-s "$DEVICE_SERIAL")
fi

sdb_run() {
  "${SDB_CMD[@]}" "$@"
}

sdb_shell() {
  sdb_run shell "$1"
}

extract_pairing_code() {
  sdb_shell "journalctl -u '$SERVICE_NAME'.service -n 400 --no-pager 2>/dev/null || true" \
    | trim_crlf \
    | python3 - <<'PY'
import re
import sys

lines = sys.stdin.read().splitlines()
patterns = [
    re.compile(r'X-Pairing-Code:\s*([A-Z0-9-]+)'),
    re.compile(r'│\s*([A-Z0-9-]{4,})\s*│'),
]

matches = []
for line in lines:
    for pattern in patterns:
        match = pattern.search(line)
        if match:
            matches.append(match.group(1))

print(matches[-1] if matches else "")
PY
}

print_logs() {
  sdb_shell "journalctl -u '$SERVICE_NAME'.service -n 120 --no-pager 2>/dev/null || true" | trim_crlf
}

host_health_probe() {
  curl --silent --show-error --fail \
    "http://$HOST:$PORT/health"
}

device_health_probe() {
  sdb_shell "curl -fsS 'http://127.0.0.1:$PORT/health' 2>/dev/null || wget -qO- 'http://127.0.0.1:$PORT/health' 2>/dev/null || true" \
    | trim_crlf
}

auth_header_args=()
if [ -n "$TOKEN" ]; then
  auth_header_args=(-H "Authorization: Bearer $TOKEN")
fi

if [ "$SHOW_LOGS" -eq 1 ]; then
  log "recent service logs"
  print_logs
  printf '\n'
fi

log "probing host-reachable health endpoint"
if HEALTH_JSON="$(host_health_probe)"; then
  :
else
  HEALTH_JSON=""
fi

if [ -z "$HEALTH_JSON" ]; then
  log "host probe failed; trying on-device localhost probe"
  DEVICE_HEALTH_JSON="$(device_health_probe)"
  if [ -z "$DEVICE_HEALTH_JSON" ]; then
    die "gateway not reachable from host and on-device localhost probe also failed"
  fi
  printf '%s\n' "$DEVICE_HEALTH_JSON"
  die "gateway appears up on the device, but host cannot reach http://$HOST:$PORT"
fi

printf '%s\n' "$HEALTH_JSON"

REQUIRE_PAIRING="$(printf '%s\n' "$HEALTH_JSON" | json_get require_pairing)"
PAIRED="$(printf '%s\n' "$HEALTH_JSON" | json_get paired)"

log "health summary: require_pairing=${REQUIRE_PAIRING:-unknown}, paired=${PAIRED:-unknown}"

if [ -z "$TOKEN" ] && [ "$REQUIRE_PAIRING" = "true" ]; then
  log "extracting pairing code from target logs"
  PAIRING_CODE="$(extract_pairing_code)"
  [ -n "$PAIRING_CODE" ] || die "pairing is enabled but no pairing code was found in recent logs"

  log "pairing via host-visible gateway"
  PAIR_RESPONSE="$(
    curl --silent --show-error --fail \
      -X POST \
      -H "X-Pairing-Code: $PAIRING_CODE" \
      "http://$HOST:$PORT/pair"
  )"
  printf '%s\n' "$PAIR_RESPONSE"

  TOKEN="$(printf '%s\n' "$PAIR_RESPONSE" | json_get token)"
  [ -n "$TOKEN" ] || die "pairing response did not contain a token"
  auth_header_args=(-H "Authorization: Bearer $TOKEN")
  log "pairing succeeded; token acquired"
fi

log "probing /api/status"
STATUS_JSON="$(
  curl --silent --show-error --fail \
    "${auth_header_args[@]}" \
    "http://$HOST:$PORT/api/status"
)"
printf '%s\n' "$STATUS_JSON"

log "probing /api/health"
API_HEALTH_JSON="$(
  curl --silent --show-error --fail \
    "${auth_header_args[@]}" \
    "http://$HOST:$PORT/api/health"
)"
printf '%s\n' "$API_HEALTH_JSON"

printf '\n'
printf 'Gateway URL: http://%s:%s\n' "$HOST" "$PORT"
if [ -n "$TOKEN" ]; then
  printf 'Bearer: %s\n' "$TOKEN"
fi
