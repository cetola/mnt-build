#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
MNT Pocket Reform Kernel Auto-Build Script
Compiles kernel, out-of-tree modules, and creates deployment tarball.
Assumes an automated build, with reduced args / options from build.py.
Assumes we checked out the correct SHA of the kernel for building.
"""

import sys

# Import build functionality from build.py
from build import run_build, DEFAULT_KERNEL_VERSION, DEFAULT_PKGREL

def main():
    return run_build(
            version=DEFAULT_KERNEL_VERSION,
            build_dir=None,
            jobs=None,
            pkgrel=DEFAULT_PKGREL,
            skip_git_operations=True,
            dry_run=False
            )


if __name__ == '__main__':
    sys.exit(main())
