#!/bin/bash
# SPDX-License-Identifier: MIT
docker run -it --rm \
    -v ~/mnt-build:/build \
    -w /build/linux \
    arch-kernel-builder \
    bash
