#!/bin/bash
# SPDX-License-Identifier: MIT
docker run -it --rm \
    -v ~/mnt-build:/home/builder/mnt-build \
    arch-kernel-builder \
    bash
