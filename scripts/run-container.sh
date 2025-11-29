#!/bin/bash
# SPDX-License-Identifier: MIT
if [ $# -eq 0 ]; then
    docker run --rm -it \
        -v "$HOME/mnt-build:/home/builder/mnt-build" \
        -w /home/builder/mnt-build \
        arch-kernel-builder \
        bash
else
    docker run --rm \
        -v "$GITHUB_WORKSPACE:/home/builder/mnt-build" \
        -w /home/builder/mnt-build \
        arch-kernel-builder \
        bash -c "cd scripts && $*"
fi
