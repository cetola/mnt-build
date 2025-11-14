#!/bin/bash
docker build --no-cache -t arch-kernel-builder . 2>&1 | tee build.log

