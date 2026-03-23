#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_SCRIPT="$ROOT_DIR/tizen/build-zeroclaw-rpm.sh"
REMOTE="origin"
TAG_SUFFIX="-rpm"

usage() {
    cat <<'EOF'
usage: ./tizen/release-zeroclaw-rpm.sh [--remote <name>]

Builds fresh zeroclaw RPMs for aarch64 and armv7l and uploads them to the
dedicated GitHub RPM release tag for the current package version.

It uses a separate `vX.Y.Z-rpm` tag so the RPM asset flow does not rewrite the
canonical zeroclaw release tag managed by the main release pipeline.
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

ensure_clean_worktree() {
    if [[ -n "$(git status --porcelain)" ]]; then
        echo "error: worktree must be clean before publishing RPM assets" >&2
        exit 1
    fi
}

write_sha256_sidecar() {
    local asset_path="$1"
    local digest

    digest="$(sha256sum "$asset_path" | awk '{print $1}')"
    printf '%s  %s\n' "$digest" "$(basename "$asset_path")" > "$asset_path.sha256"
}

release_notes() {
    local output_path="$1"
    local version_tag="$2"
    local head_commit="$3"
    local aarch64_rpm="$4"
    local armv7l_rpm="$5"

    {
        printf 'ZeroClaw RPM assets for `%s`\n\n' "$version_tag"
        printf 'This release tracks commit `%s` and publishes Tizen RPM assets only.\n\n' "$head_commit"
        printf 'Assets:\n'
        printf -- '- `%s`\n' "$(basename "$aarch64_rpm")"
        printf -- '- `%s`\n' "$(basename "$aarch64_rpm.sha256")"
        printf -- '- `%s`\n' "$(basename "$armv7l_rpm")"
        printf -- '- `%s`\n' "$(basename "$armv7l_rpm.sha256")"
    } > "$output_path"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --remote)
            if [[ $# -lt 2 ]]; then
                echo "error: --remote requires a value" >&2
                exit 1
            fi
            REMOTE="$2"
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

require_command git
require_command gh
require_command cargo
require_command rpmbuild
require_command sha256sum
require_command awk

if ! cargo tizen --version >/dev/null 2>&1; then
    echo "error: cargo-tizen is required" >&2
    exit 1
fi

if [[ ! -x "$BUILD_SCRIPT" ]]; then
    echo "error: build script is missing or not executable: $BUILD_SCRIPT" >&2
    exit 1
fi

cd "$ROOT_DIR"

ensure_clean_worktree

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
    echo "error: git remote not found: $REMOTE" >&2
    exit 1
fi

gh auth status >/dev/null

git fetch "$REMOTE" --tags >/dev/null

VERSION="$(package_version)"
if [[ -z "$VERSION" ]]; then
    echo "error: failed to read package version from Cargo.toml" >&2
    exit 1
fi

VERSION_TAG="v$VERSION"
TAG="${VERSION_TAG}${TAG_SUFFIX}"
RELEASE_NAME="ZeroClaw ${VERSION_TAG} RPM"
HEAD_COMMIT="$(git rev-parse HEAD)"

echo "Building release assets"
"$BUILD_SCRIPT" aarch64
"$BUILD_SCRIPT" armv7l

AARCH64_RPM="$ROOT_DIR/target/packages/rpm/aarch64/RPMS/aarch64/zeroclaw-$VERSION-1.aarch64.rpm"
ARMV7L_RPM="$ROOT_DIR/target/packages/rpm/armv7l/RPMS/armv7l/zeroclaw-$VERSION-1.armv7l.rpm"

for asset in "$AARCH64_RPM" "$ARMV7L_RPM"; do
    if [[ ! -f "$asset" ]]; then
        echo "error: expected asset not found: $asset" >&2
        exit 1
    fi
    write_sha256_sidecar "$asset"
done

NOTES_FILE="$(mktemp)"
trap 'rm -f "$NOTES_FILE"' EXIT
release_notes "$NOTES_FILE" "$VERSION_TAG" "$HEAD_COMMIT" "$AARCH64_RPM" "$ARMV7L_RPM"

echo "Refreshing RPM tag $TAG to HEAD"
if git rev-parse "$TAG" >/dev/null 2>&1; then
    git tag -fa "$TAG" -m "$RELEASE_NAME" HEAD
else
    git tag -a "$TAG" -m "$RELEASE_NAME" HEAD
fi

git push "$REMOTE" "refs/tags/$TAG" --force >/dev/null

ASSETS=(
    "$AARCH64_RPM"
    "$AARCH64_RPM.sha256"
    "$ARMV7L_RPM"
    "$ARMV7L_RPM.sha256"
)

if gh release view "$TAG" >/dev/null 2>&1; then
    gh release edit "$TAG" --title "$RELEASE_NAME" >/dev/null
    gh release upload "$TAG" "${ASSETS[@]}" --clobber >/dev/null
else
    gh release create "$TAG" "${ASSETS[@]}" --title "$RELEASE_NAME" --notes-file "$NOTES_FILE" >/dev/null
fi

REMOTE_TAG_COMMIT="$(git ls-remote "$REMOTE" "refs/tags/$TAG^{}" | awk '{print $1}')"
if [[ "$REMOTE_TAG_COMMIT" != "$HEAD_COMMIT" ]]; then
    echo "error: remote tag $TAG does not resolve to $HEAD_COMMIT" >&2
    exit 1
fi

echo "Uploaded RPM assets:"
printf '%s\n' "${ASSETS[@]}"
