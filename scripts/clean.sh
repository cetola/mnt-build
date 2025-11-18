#!/bin/bash
# SPDX-License-Identifier: MIT
git submodule update --init --recursive --force
git submodule foreach --recursive git reset --hard
git submodule foreach --recursive git clean -fdx

