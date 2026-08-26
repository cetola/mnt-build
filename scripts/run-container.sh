#!/bin/bash
# SPDX-License-Identifier: MIT
set -euo pipefail

MOUNT_PATH="${GITHUB_WORKSPACE:-$HOME/mnt-build}"
read -ra DOCKER_FLAGS <<< "${DOCKER_FLAGS:-}"

# CONTAINER_ENGINE overrides. Otherwise docker wins if both are installed.
if [ -z "${CONTAINER_ENGINE:-}" ]; then
    if command -v docker >/dev/null 2>&1; then
        CONTAINER_ENGINE=docker
    elif command -v podman >/dev/null 2>&1; then
        CONTAINER_ENGINE=podman
    else
        echo "ERROR: neither docker nor podman found in PATH." >&2
        echo "       Install one, or set CONTAINER_ENGINE to point at a docker-compatible CLI." >&2
        exit 1
    fi
fi

if [ $# -eq 0 ]; then
    "${CONTAINER_ENGINE}" run --rm -it "${DOCKER_FLAGS[@]}" \
        -v "$MOUNT_PATH:/home/builder/mnt-build" \
        -w /home/builder/mnt-build \
        arch-kernel-builder \
        bash
else
    "${CONTAINER_ENGINE}" run --rm "${DOCKER_FLAGS[@]}" \
        -v "$MOUNT_PATH:/home/builder/mnt-build" \
        -w /home/builder/mnt-build \
        arch-kernel-builder \
        bash -c "cd scripts && $*"
fi

