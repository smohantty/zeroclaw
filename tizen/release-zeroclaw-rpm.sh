#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_FILE="$ROOT_DIR/.cargo-tizen.toml"
REMOTE="origin"
TAG_SUFFIX="-rpm"
GH_REPO=""

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

github_repo_from_remote() {
    local remote_name="$1"
    local remote_url

    remote_url="$(git remote get-url "$remote_name")"
    case "$remote_url" in
        git@github.com:*.git)
            printf '%s\n' "${remote_url#git@github.com:}" | sed 's/\.git$//'
            ;;
        https://github.com/*/*.git)
            printf '%s\n' "${remote_url#https://github.com/}" | sed 's/\.git$//'
            ;;
        https://github.com/*/*)
            printf '%s\n' "${remote_url#https://github.com/}"
            ;;
        *)
            echo "error: unsupported GitHub remote URL for $remote_name: $remote_url" >&2
            exit 1
            ;;
    esac
}

retry_release_publish() {
    local tag="$1"
    local repo="$2"
    local release_name="$3"
    local notes_file="$4"
    shift 4
    local assets=("$@")
    local attempt
    local max_attempts=8
    local delay_seconds=3
    local output

    for ((attempt = 1; attempt <= max_attempts; attempt++)); do
        if gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
            output="$(
                {
                    gh release edit "$tag" --repo "$repo" --title "$release_name"
                    gh release upload "$tag" --repo "$repo" "${assets[@]}" --clobber
                } 2>&1
            )" && return 0
        else
            output="$(
                gh release create "$tag" --repo "$repo" "${assets[@]}" \
                    --title "$release_name" \
                    --notes-file "$notes_file" 2>&1
            )" && return 0
        fi

        if (( attempt == max_attempts )); then
            echo "$output" >&2
            echo "error: failed to publish GitHub release $tag after $max_attempts attempts" >&2
            return 1
        fi

        echo "GitHub release API not ready for $tag yet; retrying in ${delay_seconds}s (attempt $attempt/$max_attempts)" >&2
        sleep "$delay_seconds"
    done
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

find_built_rpm() {
    local arch="$1"

    find "$ROOT_DIR/target/tizen/$arch/release/rpmbuild/RPMS" -type f -name 'zeroclaw-*.rpm' | sort | tail -n 1
}

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

binary_name() {
    awk '
        $0 == "[[bin]]" { in_bin = 1; next }
        /^\[/ { in_bin = 0 }
        in_bin && $1 == "name" {
            gsub(/"/, "", $3)
            print $3
            exit
        }
    ' "$ROOT_DIR/Cargo.toml"
}

build_rpm_for_arch() {
    local arch="$1"
    local package_name
    local executable_name
    local built_binary_path
    local package_binary_path
    local status

    package_name="$(package_value name)"
    executable_name="$(binary_name)"
    executable_name="${executable_name:-$package_name}"

    if [[ -z "$package_name" ]]; then
        echo "error: failed to read package name from Cargo.toml" >&2
        exit 1
    fi

    echo "Building zeroclaw RPM for $arch via cargo-tizen"
    (
        cd "$ROOT_DIR"
        cargo tizen build --config "$CONFIG_FILE" -A "$arch" --release -- -p "$package_name" --bin "$executable_name"
    )

    built_binary_path="$ROOT_DIR/target/tizen/$arch/cargo"
    built_binary_path="$(find "$built_binary_path" -type f -path "*/release/$executable_name" | sort | tail -n 1)"
    if [[ -z "$built_binary_path" || ! -f "$built_binary_path" ]]; then
        echo "error: built binary not found for $arch: $executable_name" >&2
        exit 1
    fi

    package_binary_path="$(dirname "$built_binary_path")/$package_name"
    cp "$built_binary_path" "$package_binary_path"
    (
        cd "$ROOT_DIR"
        cargo tizen rpm --config "$CONFIG_FILE" -A "$arch" --cargo-release --no-build
    )
    status=$?
    rm -f "$package_binary_path"
    if [[ $status -ne 0 ]]; then
        exit $status
    fi
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

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "error: cargo-tizen config not found: $CONFIG_FILE" >&2
    exit 1
fi

cd "$ROOT_DIR"

ensure_clean_worktree

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
    echo "error: git remote not found: $REMOTE" >&2
    exit 1
fi

GH_REPO="$(github_repo_from_remote "$REMOTE")"

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
build_rpm_for_arch aarch64
build_rpm_for_arch armv7l

AARCH64_RPM="$(find_built_rpm aarch64)"
ARMV7L_RPM="$(find_built_rpm armv7l)"

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

retry_release_publish "$TAG" "$GH_REPO" "$RELEASE_NAME" "$NOTES_FILE" "${ASSETS[@]}"

REMOTE_TAG_COMMIT="$(git ls-remote "$REMOTE" "refs/tags/$TAG^{}" | awk '{print $1}')"
if [[ "$REMOTE_TAG_COMMIT" != "$HEAD_COMMIT" ]]; then
    echo "error: remote tag $TAG does not resolve to $HEAD_COMMIT" >&2
    exit 1
fi

echo "Uploaded RPM assets:"
printf '%s\n' "${ASSETS[@]}"
