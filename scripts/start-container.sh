#!/bin/bash
docker run -it --rm \
    -v ~/mnt-build:/build \
    -w /build/linux \
    arch-kernel-builder \
    bash
