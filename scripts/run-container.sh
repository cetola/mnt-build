#!/bin/bash
# SPDX-License-Identifier: MIT
MOUNT_PATH="${GITHUB_WORKSPACE:-$HOME/mnt-build}"

if [ $# -eq 0 ]; then
    docker run --rm -it \
        -v "$MOUNT_PATH:/home/builder/mnt-build" \
        -w /home/builder/mnt-build \
        arch-kernel-builder \
        bash
else
    docker run --rm \
        -v "$MOUNT_PATH:/home/builder/mnt-build" \
        -w /home/builder/mnt-build \
        arch-kernel-builder \
        bash -c "cd scripts && $*"
fi
