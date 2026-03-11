#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
MNT Reform Kernel Auto-Build Script
Compiles kernel, out-of-tree modules, and creates deployment tarball.
Assumes an automated build, with reduced args / options from build.py.
Assumes we checked out the correct SHA of the kernel for building.
"""

import argparse
import os
import sys

# Import build functionality from build.py
from build import run_build, DEFAULT_KERNEL_VERSION, DEFAULT_PKGREL, DEFAULT_CROSS_COMPILE

def main():
    parser = argparse.ArgumentParser(
            description='Run automated kernel build.'
            )
    parser.add_argument(
            '--cross-compile',
            default=os.environ.get('CROSS_COMPILE', DEFAULT_CROSS_COMPILE),
            help=f'CROSS_COMPILE prefix (default: env CROSS_COMPILE or {DEFAULT_CROSS_COMPILE}). '
                 'Use "" or "none" for native build with no prefix.'
            )
    parser.add_argument(
            '--with-headers',
            action='store_true',
            help='Also generate an external-module headers tree.'
            )
    args = parser.parse_args()

    return run_build(
            version=DEFAULT_KERNEL_VERSION,
            build_dir=None,
            jobs=None,
            pkgrel=DEFAULT_PKGREL,
            skip_git_operations=True,
            dry_run=False,
            cross_compile=args.cross_compile,
            with_headers=args.with_headers
            )


if __name__ == '__main__':
    sys.exit(main())
