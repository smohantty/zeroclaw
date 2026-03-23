#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_SCRIPT="$ROOT_DIR/tizen/build-zeroclaw-rpm.sh"
TARGET=""
WAIT_SECS=60

usage() {
    cat <<'EOF'
usage: ./tizen/dev-update-zeroclaw.sh [--target <ip[:port]>] [--wait-secs <seconds>]

Uses rsdb to detect the target architecture, build the matching zeroclaw RPM,
push it to the device, schedule a detached install, then verify that
zeroclaw.service came back with the updated binary.
EOF
}

require_command() {
    local command_name="$1"

    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "error: $command_name is required" >&2
        exit 1
    fi
}

package_version() {
    awk '
        $0 == "[package]" { in_package = 1; next }
        /^\[/ { in_package = 0 }
        in_package && $1 == "version" {
            gsub(/"/, "", $3)
            print $3
            exit
        }
    ' "$ROOT_DIR/Cargo.toml"
}

normalize_arch() {
    local raw_arch="$1"

    case "$raw_arch" in
        aarch64|arm64)
            echo "aarch64"
            ;;
        armv7l|armv7hl|armv7*)
            echo "armv7l"
            ;;
        *)
            echo "error: unsupported remote architecture: $raw_arch" >&2
            exit 1
            ;;
    esac
}

run_rsdb() {
    local subcommand="$1"
    shift

    if [[ -n "$TARGET" ]]; then
        "$RSDB_BIN" "$subcommand" --target "$TARGET" "$@"
    else
        "$RSDB_BIN" "$subcommand" "$@"
    fi
}

capture_last_line() {
    tr -d '\r' | awk 'NF { value = $0 } END { print value }'
}

safe_rsdb_capture() {
    {
        run_rsdb "$@" 2>/dev/null || true
    } | capture_last_line
}

hint_rsdb_shell() {
    if [[ -n "$TARGET" ]]; then
        printf '%q ' "$RSDB_BIN" shell --target "$TARGET" "$@"
    else
        printf '%q ' "$RSDB_BIN" shell "$@"
    fi
}

service_start_marker() {
    safe_rsdb_capture shell systemctl show -p ActiveEnterTimestampMonotonic --value zeroclaw.service
}

service_state() {
    safe_rsdb_capture shell systemctl is-active zeroclaw.service
}

remote_version() {
    safe_rsdb_capture shell /usr/bin/zeroclaw --version
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target)
            if [[ $# -lt 2 ]]; then
                echo "error: --target requires a value" >&2
                exit 1
            fi
            TARGET="$2"
            shift 2
            ;;
        --wait-secs)
            if [[ $# -lt 2 ]]; then
                echo "error: --wait-secs requires a value" >&2
                exit 1
            fi
            WAIT_SECS="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

require_command cargo
require_command awk
require_command rsdb
require_command date

if [[ ! -x "$BUILD_SCRIPT" ]]; then
    echo "error: build script is missing or not executable: $BUILD_SCRIPT" >&2
    exit 1
fi

cd "$ROOT_DIR"

RSDB_BIN="$(command -v rsdb)"
ZEROCLAW_VERSION="$(package_version)"
if [[ -z "$ZEROCLAW_VERSION" ]]; then
    echo "error: failed to read package version from Cargo.toml" >&2
    exit 1
fi

echo "Detecting remote architecture"
RAW_ARCH="$(
    run_rsdb shell uname -m | capture_last_line
)"

if [[ -z "$RAW_ARCH" ]]; then
    echo "error: failed to determine remote architecture with \`rsdb shell uname -m\`" >&2
    exit 1
fi

TARGET_ARCH="$(normalize_arch "$RAW_ARCH")"
echo "Remote architecture: $RAW_ARCH -> $TARGET_ARCH"

echo "Capturing current zeroclaw.service start marker"
BEFORE_START_MARKER="$(service_start_marker)"

echo "Building zeroclaw RPM for $TARGET_ARCH"
"$BUILD_SCRIPT" "$TARGET_ARCH"

RPM_PATH="$ROOT_DIR/target/packages/rpm/$TARGET_ARCH/RPMS/$TARGET_ARCH/zeroclaw-$ZEROCLAW_VERSION-1.$TARGET_ARCH.rpm"
if [[ ! -f "$RPM_PATH" ]]; then
    echo "error: expected RPM not found: $RPM_PATH" >&2
    exit 1
fi

REMOTE_RPM_PATH="/tmp/zeroclaw-$ZEROCLAW_VERSION-dev-$TARGET_ARCH.rpm"
UNIT_NAME="zeroclaw-upgrade-$(date +%s)"
REMOTE_INSTALL_COMMAND="rpm -Uvh --force \"$REMOTE_RPM_PATH\"; status=\$?; rm -f \"$REMOTE_RPM_PATH\"; exit \$status"

echo "Pushing RPM to $REMOTE_RPM_PATH"
run_rsdb push "$RPM_PATH" "$REMOTE_RPM_PATH"

echo "Scheduling detached install with systemd-run ($UNIT_NAME)"
run_rsdb shell \
    systemd-run \
    --unit "$UNIT_NAME" \
    --quiet \
    --collect \
    --service-type=oneshot \
    --no-block \
    /bin/sh \
    -lc \
    "$REMOTE_INSTALL_COMMAND"

echo "Waiting up to $WAIT_SECS seconds for zeroclaw.service"
DEADLINE=$((SECONDS + WAIT_SECS))
while (( SECONDS < DEADLINE )); do
    if [[ "$(service_state)" == "active" ]]; then
        AFTER_START_MARKER="$(service_start_marker)"
        REMOTE_VERSION="$(remote_version)"
        if [[ -n "$BEFORE_START_MARKER" && "$AFTER_START_MARKER" == "$BEFORE_START_MARKER" ]]; then
            echo "error: service responded again, but zeroclaw.service does not appear to have restarted" >&2
            echo "next check: $(hint_rsdb_shell systemctl status zeroclaw.service --no-pager -l)" >&2
            exit 1
        fi
        echo "Development update complete"
        echo "target_arch: $TARGET_ARCH"
        echo "rpm: $RPM_PATH"
        echo "remote_version: ${REMOTE_VERSION:-unknown}"
        echo "service_start_marker: ${BEFORE_START_MARKER:-unknown} -> ${AFTER_START_MARKER:-unknown}"
        exit 0
    fi
    sleep 1
done

echo "error: zeroclaw.service did not become ready within $WAIT_SECS seconds" >&2
echo "next check: $(hint_rsdb_shell systemctl status zeroclaw.service --no-pager -l)" >&2
echo "next check: $(hint_rsdb_shell journalctl -u zeroclaw.service -n 100 --no-pager)" >&2
echo "next check: $(hint_rsdb_shell /usr/bin/zeroclaw --version)" >&2
exit 1
