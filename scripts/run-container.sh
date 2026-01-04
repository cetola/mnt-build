#!/bin/bash
# SPDX-License-Identifier: MIT
set -euo pipefail

MOUNT_PATH="${GITHUB_WORKSPACE:-$HOME/mnt-build}"
DOCKER_FLAGS="${DOCKER_FLAGS:-}"

if [ $# -eq 0 ]; then
    docker run --rm -it $DOCKER_FLAGS \
        -v "$MOUNT_PATH:/home/builder/mnt-build" \
        -w /home/builder/mnt-build \
        arch-kernel-builder \
        bash
else
    docker run --rm $DOCKER_FLAGS \
        -v "$MOUNT_PATH:/home/builder/mnt-build" \
        -w /home/builder/mnt-build \
        arch-kernel-builder \
        bash -c "cd scripts && $*"
fi

