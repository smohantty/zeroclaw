#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="$ROOT_DIR/tizen/.cargo-tizen.toml"
SPEC_FILE="$ROOT_DIR/tizen/packaging/rpm/zeroclaw.spec"
SERVICE_FILE="$ROOT_DIR/tizen/packaging/systemd/zeroclaw.service"
RPMRC_FILE="$ROOT_DIR/tizen/packaging/rpm/zeroclaw.rpmrc"

if [[ $# -gt 1 ]]; then
    echo "usage: $0 [aarch64|armv7l]" >&2
    exit 1
fi

REQUESTED_ARCH="${1:-}"

if ! command -v cargo >/dev/null 2>&1; then
    echo "error: cargo is required" >&2
    exit 1
fi

if ! command -v rpmbuild >/dev/null 2>&1; then
    echo "error: rpmbuild is required" >&2
    exit 1
fi

if ! cargo tizen --version >/dev/null 2>&1; then
    echo "error: cargo-tizen is required" >&2
    exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "error: cargo-tizen config not found: $CONFIG_FILE" >&2
    exit 1
fi

if [[ ! -f "$RPMRC_FILE" ]]; then
    echo "error: rpmrc compatibility overrides not found: $RPMRC_FILE" >&2
    exit 1
fi

package_value() {
    local key="$1"

    awk -v wanted_key="$key" '
        $0 == "[package]" { in_package = 1; next }
        /^\[/ { in_package = 0 }
        in_package && $1 == wanted_key {
            value = $3
            gsub(/"/, "", value)
            print value
            exit
        }
    ' "$ROOT_DIR/Cargo.toml"
}

PACKAGE_NAME="$(package_value name)"
VERSION="$(package_value version)"

if [[ -z "$PACKAGE_NAME" ]]; then
    echo "error: failed to read package name from Cargo.toml" >&2
    exit 1
fi

if [[ -z "$VERSION" ]]; then
    echo "error: failed to read package version from Cargo.toml" >&2
    exit 1
fi

toml_value() {
    local section="$1"
    local key="$2"
    awk -v wanted_section="$section" -v wanted_key="$key" '
        /^\[/ {
            section = $0
            gsub(/^\[/, "", section)
            gsub(/\]$/, "", section)
            next
        }
        section == wanted_section && $1 == wanted_key {
            value = $3
            gsub(/"/, "", value)
            print value
            exit
        }
    ' "$CONFIG_FILE"
}

ARCH="${REQUESTED_ARCH:-$(toml_value default arch)}"
ARCH="${ARCH:-aarch64}"
case "$ARCH" in
    aarch64)
        TARGET_TRIPLE="$(toml_value "arch.$ARCH" rust_target)"
        TARGET_TRIPLE="${TARGET_TRIPLE:-aarch64-unknown-linux-gnu}"
        RPM_ARCH="$(toml_value "arch.$ARCH" rpm_build_arch)"
        RPM_ARCH="${RPM_ARCH:-aarch64}"
        ;;
    armv7l)
        TARGET_TRIPLE="$(toml_value "arch.$ARCH" rust_target)"
        TARGET_TRIPLE="${TARGET_TRIPLE:-armv7-unknown-linux-gnueabi}"
        RPM_ARCH="$(toml_value "arch.$ARCH" rpm_build_arch)"
        RPM_ARCH="${RPM_ARCH:-armv7l}"
        ;;
    *)
        echo "error: invalid arch: $ARCH (expected aarch64 or armv7l)" >&2
        exit 1
        ;;
esac

PROFILE_NAME="$(toml_value default profile)"
PROFILE_NAME="${PROFILE_NAME:-tizen}"
PLATFORM_VERSION="$(toml_value default platform_version)"
PLATFORM_VERSION="${PLATFORM_VERSION:-10.0}"
PROVIDER="$(toml_value default provider)"
PROVIDER="${PROVIDER:-rootstrap}"
TOPDIR="$ROOT_DIR/target/packages/rpm/$RPM_ARCH"
HOST_ARCH="$(uname -m)"
RPMRC_FILES=(/usr/lib/rpm/rpmrc)

if [[ -f /etc/rpmrc ]]; then
    RPMRC_FILES+=(/etc/rpmrc)
fi

if [[ -n "${HOME:-}" && -f "$HOME/.rpmrc" ]]; then
    RPMRC_FILES+=("$HOME/.rpmrc")
fi

RPMRC_FILES+=("$RPMRC_FILE")
RPMRC_PATH="$(IFS=:; echo "${RPMRC_FILES[*]}")"

echo "Building zeroclaw for Tizen arch=$ARCH profile=$PROFILE_NAME platform_version=$PLATFORM_VERSION provider=$PROVIDER"
(
    cd "$ROOT_DIR"
    cargo tizen build --config "$CONFIG_FILE" -A "$ARCH" --release -- -p "$PACKAGE_NAME"
)

BINARY_PATH="$ROOT_DIR/target/tizen/$ARCH/cargo/$TARGET_TRIPLE/release/zeroclaw"
if [[ ! -f "$BINARY_PATH" ]]; then
    echo "error: built zeroclaw binary not found: $BINARY_PATH" >&2
    exit 1
fi

rm -rf "$TOPDIR"
mkdir -p "$TOPDIR"/BUILD "$TOPDIR"/BUILDROOT "$TOPDIR"/RPMS "$TOPDIR"/SOURCES "$TOPDIR"/SPECS "$TOPDIR"/SRPMS

cp "$BINARY_PATH" "$TOPDIR/SOURCES/zeroclaw"
cp "$SERVICE_FILE" "$TOPDIR/SOURCES/zeroclaw.service"
cp "$SPEC_FILE" "$TOPDIR/SPECS/zeroclaw.spec"

RPMBUILD_ARGS=(
    -bb
    --rcfile "$RPMRC_PATH"
    --target "$RPM_ARCH"
    --define "_topdir $TOPDIR"
    --define "_build_id_links none"
    --define "zeroclaw_version $VERSION"
    --define "zeroclaw_arch $RPM_ARCH"
)

if [[ "$HOST_ARCH" != "$ARCH" ]]; then
    RPMBUILD_ARGS+=(
        --define "__brp_strip /bin/true"
        --define "__brp_strip_static_archive /bin/true"
        --define "__brp_strip_comment_note /bin/true"
    )
fi

RPMBUILD_ARGS+=("$TOPDIR/SPECS/zeroclaw.spec")

if ! rpmbuild "${RPMBUILD_ARGS[@]}"; then
    echo "error: rpmbuild rejected target arch $RPM_ARCH on this host." >&2
    echo "error: checked with rpmrc overrides from $RPMRC_FILE." >&2
    echo "next check: rpmbuild --rcfile \"$RPMRC_PATH\" --showrc | sed -n '1,12p'" >&2
    exit 1
fi

RPM_PATH="$(find "$TOPDIR/RPMS" -type f -name "zeroclaw-${VERSION}-1*.rpm" | head -n 1)"
if [[ -z "$RPM_PATH" ]]; then
    echo "error: rpmbuild completed but no RPM was found under $TOPDIR/RPMS" >&2
    exit 1
fi

echo "Built RPM:"
echo "$RPM_PATH"
