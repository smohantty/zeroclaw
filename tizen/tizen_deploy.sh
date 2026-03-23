#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./tizen/tizen_deploy.sh [--force-state]

Options:
  --force-state           Replace /root/.zeroclaw on the device.
  --help                  Show this help text.

Behavior:
  - reads ./.zeroclaw from the directory where the script is invoked
  - reads ./zeroclaw from the directory where the script is invoked
  - installs ./zeroclaw to /usr/bin/zeroclaw
  - seeds /root/.zeroclaw if missing (or replaces it with --force-state)
  - installs a systemd unit at /usr/lib/systemd/system/zeroclaw.service
  - runs the daemon as root
  - checks whether the service becomes active after startup
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

FORCE_STATE=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --force-state)
      FORCE_STATE=1
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

need_cmd pwd
need_cmd cp
need_cmd rm
need_cmd mkdir
need_cmd cat
need_cmd systemctl
need_cmd sleep

CURRENT_DIR="$(pwd -P)"
SOURCE_STATE_DIR="$CURRENT_DIR/.zeroclaw"
SOURCE_BIN_PATH="$CURRENT_DIR/zeroclaw"

[ -d "$SOURCE_STATE_DIR" ] || die "state dir not found: $SOURCE_STATE_DIR"
[ -f "$SOURCE_STATE_DIR/config.toml" ] || die "state dir must contain config.toml: $SOURCE_STATE_DIR/config.toml"
[ -f "$SOURCE_BIN_PATH" ] || die "binary not found: $SOURCE_BIN_PATH"

SERVICE_NAME="zeroclaw"
DEVICE_USER="root"
TARGET_BIN_DIR="/usr/bin"
TARGET_BIN_PATH="$TARGET_BIN_DIR/zeroclaw"
TARGET_STATE_DIR="/root/.zeroclaw"
TARGET_WORKSPACE_DIR="$TARGET_STATE_DIR/workspace"
TARGET_UNIT_DIR="/usr/lib/systemd/system"
TARGET_UNIT_PATH="$TARGET_UNIT_DIR/${SERVICE_NAME}.service"
UNIT_TMP="/tmp/${SERVICE_NAME}.service.$$"

cleanup() {
  rm -f "$UNIT_TMP"
}
trap cleanup EXIT

log "working dir: $CURRENT_DIR"
log "binary: $SOURCE_BIN_PATH"
log "state source: $SOURCE_STATE_DIR"
log "device user: $DEVICE_USER"
log "install path: $TARGET_BIN_PATH"
log "state path: $TARGET_STATE_DIR"
log "unit path: $TARGET_UNIT_PATH"

log "preparing target directories"
mkdir -p "$TARGET_BIN_DIR" "$TARGET_UNIT_DIR"

log "stopping service if present"
systemctl stop "$SERVICE_NAME.service" >/dev/null 2>&1 || true

log "installing binary to $TARGET_BIN_PATH"
cp "$SOURCE_BIN_PATH" "$TARGET_BIN_PATH"

if [ "$FORCE_STATE" -eq 1 ]; then
  log "replacing state at $TARGET_STATE_DIR"
  rm -rf "$TARGET_STATE_DIR"
fi

if [ -d "$TARGET_STATE_DIR" ]; then
  log "state already exists at $TARGET_STATE_DIR; leaving it in place"
else
  log "copying seeded state to $TARGET_STATE_DIR"
  cp -R "$SOURCE_STATE_DIR" "$TARGET_STATE_DIR"
fi

log "ensuring writable runtime directories"
mkdir -p "$TARGET_WORKSPACE_DIR"

cat >"$UNIT_TMP" <<EOF
[Unit]
Description=ZeroClaw daemon (Tizen TV)
After=network.target

[Service]
Type=simple
User=$DEVICE_USER
WorkingDirectory=$TARGET_WORKSPACE_DIR
ExecStart=$TARGET_BIN_PATH --config-dir $TARGET_STATE_DIR daemon
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

log "installing systemd unit"
cp "$UNIT_TMP" "$TARGET_UNIT_PATH"

log "reloading and enabling service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME.service" >/dev/null 2>&1 || true

log "starting service"
systemctl start "$SERVICE_NAME.service"
sleep 2

SERVICE_ACTIVE="$(systemctl is-active "$SERVICE_NAME.service" 2>/dev/null || true)"
log "service status: ${SERVICE_ACTIVE:-unknown}"

if [ "$SERVICE_ACTIVE" != "active" ]; then
  systemctl --no-pager --full status "$SERVICE_NAME.service" || true
  if command -v journalctl >/dev/null 2>&1; then
    printf '\n'
    log "recent service logs"
    journalctl -u "$SERVICE_NAME.service" -n 120 --no-pager || true
  fi
  exit 1
fi

log "deploy complete"
printf '\n'
printf 'Binary:  %s\n' "$TARGET_BIN_PATH"
printf 'State:   %s\n' "$TARGET_STATE_DIR"
printf 'Service: %s.service\n' "$SERVICE_NAME"
printf 'Unit:    %s\n' "$TARGET_UNIT_PATH"
