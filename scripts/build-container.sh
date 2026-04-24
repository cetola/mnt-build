#!/bin/bash
# SPDX-License-Identifier: MIT

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARM_STAGING="${SCRIPT_DIR}/arm-libs-staging"
TMPWORK=""

# Always clean up intermediate download scratch space on exit.
# ARM_STAGING is intentionally left behind on failure so the caller
# can inspect what was (or wasn't) staged.
cleanup_tmpwork() {
    [ -z "${TMPWORK}" ] || rm -rf "${TMPWORK}"
}
trap cleanup_tmpwork EXIT

ALARM_MIRRORS=(
    "http://mirror.archlinuxarm.org/aarch64"
    "http://de.mirror.archlinuxarm.org/aarch64"
    "http://eu.mirror.archlinuxarm.org/aarch64"
    "http://sg.mirror.archlinuxarm.org/aarch64"
)

# Try every configured mirror for <repo>/<pkg>; write the tarball to <outfile>.
# <workdir> must be a private scratch directory — it will hold the repo.db.
# Returns 1 if the package cannot be resolved from any mirror.
try_download_alarm_pkg() {
    local repo="$1" pkg="$2" outfile="$3" workdir="$4"
    for mirror in "${ALARM_MIRRORS[@]}"; do
        curl -fsSL "${mirror}/${repo}/${repo}.db" -o "${workdir}/repo.db" 2>/dev/null || continue
        local entry file
        entry="$(bsdtar -tf "${workdir}/repo.db" | grep -E "^${pkg}-[^/]+/desc$" | head -n1 || true)"
        [ -n "${entry}" ] || continue
        file="$(bsdtar -xOf "${workdir}/repo.db" "${entry}" \
                | awk 'found{print;exit} /^%FILENAME%$/{found=1}' || true)"
        [ -n "${file}" ] || continue
        curl -fL "${mirror}/${repo}/${file}" -o "${outfile}" 2>/dev/null || continue
        echo "  ${pkg}: ${mirror}/${repo}/${file}"
        return 0
    done
    return 1
}

# Download an ALARM package and copy its shared libs into arm-libs-staging/lib/.
# Each call gets its own scratch subdir so calls can run concurrently.
install_alarm_libs() {
    local repo="$1" pkg="$2"
    local workdir
    workdir="$(mktemp -d "${TMPWORK}/${pkg}.XXXXXX")"
    try_download_alarm_pkg "${repo}" "${pkg}" "${workdir}/pkg.tar" "${workdir}" || return 1
    mkdir "${workdir}/pkg-dir"
    bsdtar --no-same-owner -xf "${workdir}/pkg.tar" -C "${workdir}/pkg-dir"
    find "${workdir}/pkg-dir/usr/lib" -maxdepth 1 -name 'lib*.so*' \
        -exec cp -a {} "${ARM_STAGING}/lib/" \;
}

prepare_arm_libs() {
    echo "Preparing aarch64 sysroot libs in ${ARM_STAGING} ..."
    TMPWORK="$(mktemp -d)"
    rm -rf "${ARM_STAGING}"
    mkdir -p "${ARM_STAGING}/lib" "${ARM_STAGING}/include"

    # All four packages are independent — fetch them in parallel.
    local pids=()

    {
        local workdir
        workdir="$(mktemp -d "${TMPWORK}/openssl.XXXXXX")"
        try_download_alarm_pkg "core" "openssl" "${workdir}/pkg.tar" "${workdir}" \
            || { echo "ERROR: openssl not resolved from any mirror" >&2; exit 1; }
        mkdir "${workdir}/pkg-dir"
        bsdtar --no-same-owner -xf "${workdir}/pkg.tar" -C "${workdir}/pkg-dir"
        cp -a "${workdir}/pkg-dir/usr/include/openssl" "${ARM_STAGING}/include/"
        find "${workdir}/pkg-dir/usr/lib" -maxdepth 1 \
            \( -name 'libssl*' -o -name 'libcrypto*' \) -exec cp -a {} "${ARM_STAGING}/lib/" \;
    } &
    pids+=($!)

    {
        install_alarm_libs "core" "zlib" \
            || { echo "ERROR: zlib not resolved from any mirror" >&2; exit 1; }
    } &
    pids+=($!)

    {
        install_alarm_libs "core" "zstd" \
            || { echo "ERROR: zstd not resolved from any mirror" >&2; exit 1; }
    } &
    pids+=($!)

    {
        install_alarm_libs "core" "brotli" \
            || { echo "ERROR: brotli not resolved from any mirror" >&2; exit 1; }
    } &
    pids+=($!)

    local failed=0
    for pid in "${pids[@]}"; do
        wait "${pid}" || failed=1
    done
    [ "${failed}" -eq 0 ] || return 1

    local missing=0
    for lib in libssl.so libcrypto.so libz.so libzstd.so libbrotlidec.so; do
        ls "${ARM_STAGING}/lib/${lib}"* >/dev/null 2>&1 \
            || { echo "ERROR: ${lib} missing from staging" >&2; missing=1; }
    done
    [ "${missing}" -eq 0 ] || return 1

    echo "aarch64 sysroot libs ready."
}

# ── argument parsing ──────────────────────────────────────────────────────────

WITH_ARM_LIBS=false
BUILD_ARGS=()

while [[ $# -gt 0 ]]; do
    case $1 in
        --uid)
            BUILD_ARGS+=("--build-arg" "UID=$2")
            shift 2
            ;;
        --username)
            BUILD_ARGS+=("--build-arg" "USERNAME=$2")
            shift 2
            ;;
        --with-arm-libs)
            WITH_ARM_LIBS=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--uid UID] [--username USERNAME] [--with-arm-libs]"
            exit 1
            ;;
    esac
done

# ── main ──────────────────────────────────────────────────────────────────────

if [ "${WITH_ARM_LIBS}" = true ]; then
    prepare_arm_libs
else
    mkdir -p "${ARM_STAGING}/lib" "${ARM_STAGING}/include"
fi

cd "${SCRIPT_DIR}"
docker build --no-cache -t arch-kernel-builder "${BUILD_ARGS[@]}" . 2>&1 | tee container-build.log

rm -rf "${ARM_STAGING}"
