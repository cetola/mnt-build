#!/bin/bash
# SPDX-License-Identifier: MIT

# Array to collect build arguments
BUILD_ARGS=()

# Parse command line arguments
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
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--uid UID] [--username USERNAME]"
            exit 1
            ;;
    esac
done

# Build docker command with optional build args (will use Dockerfile defaults if not provided)
docker build --no-cache -t arch-kernel-builder "${BUILD_ARGS[@]}" . 2>&1 | tee container-build.log
