#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="$ROOT_DIR/.cargo-tizen.toml"
RPM_SPEC_FILE="$ROOT_DIR/tizen/rpm/zeroclawlabs.spec"

if [[ $# -gt 1 ]]; then
    echo "usage: $0 [aarch64|armv7l]" >&2
    exit 1
fi

REQUESTED_ARCH="${1:-}"

if ! command -v cargo >/dev/null 2>&1; then
    echo "error: cargo is required" >&2
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

if [[ ! -f "$RPM_SPEC_FILE" ]]; then
    echo "error: RPM spec not found: $RPM_SPEC_FILE" >&2
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
BINARY_NAME="$(awk '
    $0 == "[[bin]]" { in_bin = 1; next }
    /^\[/ { in_bin = 0 }
    in_bin && $1 == "name" {
        gsub(/"/, "", $3)
        print $3
        exit
    }
' "$ROOT_DIR/Cargo.toml")"
BINARY_NAME="${BINARY_NAME:-$PACKAGE_NAME}"
SPEC_VERSION="$(awk '
    $1 == "Version:" {
        print $2
        exit
    }
' "$RPM_SPEC_FILE")"

if [[ -z "$PACKAGE_NAME" ]]; then
    echo "error: failed to read package name from Cargo.toml" >&2
    exit 1
fi

if [[ -z "$VERSION" ]]; then
    echo "error: failed to read package version from Cargo.toml" >&2
    exit 1
fi

if [[ -z "$SPEC_VERSION" ]]; then
    echo "error: failed to read Version from $RPM_SPEC_FILE" >&2
    exit 1
fi

if [[ "$SPEC_VERSION" != "$VERSION" ]]; then
    echo "error: Cargo.toml version ($VERSION) does not match RPM spec version ($SPEC_VERSION)" >&2
    echo "next check: update $RPM_SPEC_FILE before publishing RPM assets" >&2
    exit 1
fi

ARCH="${REQUESTED_ARCH:-$(awk '
    $0 == "[default]" { in_default = 1; next }
    /^\[/ { in_default = 0 }
    in_default && $1 == "arch" {
        gsub(/"/, "", $3)
        print $3
        exit
    }
' "$CONFIG_FILE")}"
ARCH="${ARCH:-armv7l}"
case "$ARCH" in
    aarch64|armv7l)
        ;;
    *)
        echo "error: invalid arch: $ARCH (expected aarch64 or armv7l)" >&2
        exit 1
        ;;
esac

echo "Building zeroclaw RPM via cargo-tizen for arch=$ARCH"
(
    cd "$ROOT_DIR"
    cargo tizen build -A "$ARCH" --release -- --bin "$BINARY_NAME"
)

mapfile -t BUILT_BINARIES < <(find "$ROOT_DIR/target/tizen/$ARCH/cargo" -type f -path "*/release/$BINARY_NAME" | sort)
if [[ "${#BUILT_BINARIES[@]}" -ne 1 ]]; then
    echo "error: expected exactly one built binary named $BINARY_NAME for arch=$ARCH" >&2
    printf '%s\n' "${BUILT_BINARIES[@]}" >&2
    exit 1
fi

BUILT_BINARY_PATH="${BUILT_BINARIES[0]}"
PACKAGE_BINARY_PATH="$(dirname "$BUILT_BINARY_PATH")/$PACKAGE_NAME"

# cargo-tizen currently stages RPM binaries by Cargo package name, while this
# repo ships a renamed [[bin]] target (`zeroclaw`). Create a temporary alias so
# the direct `cargo tizen rpm` backend can package the standard layout.
cp "$BUILT_BINARY_PATH" "$PACKAGE_BINARY_PATH"
trap 'rm -f "$PACKAGE_BINARY_PATH"' EXIT

(
    cd "$ROOT_DIR"
    cargo tizen rpm -A "$ARCH" --release --no-build
)

RPM_PATH="$(find "$ROOT_DIR/target/tizen/$ARCH/release/rpmbuild/RPMS" -type f -name "zeroclaw-${VERSION}-*.rpm" | sort | tail -n 1)"
if [[ -z "$RPM_PATH" ]]; then
    echo "error: cargo tizen rpm completed but no RPM was found for arch=$ARCH" >&2
    exit 1
fi

echo "Built RPM:"
echo "$RPM_PATH"
