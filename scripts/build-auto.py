#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
MNT Reform Kernel Auto-Build Script
Compiles kernel, out-of-tree modules, and creates deployment tarball.
Assumes an automated build, with reduced args / options from mnt_build.py.
Assumes we checked out the correct SHA of the kernel for building.
"""

import argparse
import os
import sys

# Import build functionality from mnt_build.py
from mnt_build import (
    run_build,
    DEFAULT_KERNEL_VERSION,
    DEFAULT_CROSS_COMPILE,
    DEFAULT_LOCALVERSION_REV,
)

def main():
    parser = argparse.ArgumentParser(
            description='Run automated kernel build.'
            )
    parser.add_argument(
            '--kversion',
            default=os.environ.get('MNT_KERNEL_VERSION', DEFAULT_KERNEL_VERSION),
            help=f'Kernel version to build (default: env MNT_KERNEL_VERSION or {DEFAULT_KERNEL_VERSION}).'
            )
    parser.add_argument(
            '--arch',
            default='arm64',
            help='Kernel ARCH to build for (default: arm64).'
            )
    parser.add_argument(
            '--cross-compile',
            default=os.environ.get('CROSS_COMPILE', DEFAULT_CROSS_COMPILE),
            help=f'CROSS_COMPILE prefix (default: env CROSS_COMPILE or {DEFAULT_CROSS_COMPILE}). '
                 'Use "" or "none" for native build with no prefix. '
                 'This does not change ARCH; use --arch to override that.'
            )
    parser.add_argument(
            '--with-headers',
            action='store_true',
            help='Also generate an external-module headers tree.'
            )
    parser.add_argument(
            '--localversion-rev',
            type=int,
            default=int(os.environ.get('MNT_LOCALVERSION_REV', DEFAULT_LOCALVERSION_REV)),
            help=f'Kernel localversion revision (default: env MNT_LOCALVERSION_REV or {DEFAULT_LOCALVERSION_REV}).'
            )
    args = parser.parse_args()

    return run_build(
            version=args.kversion,
            build_dir=None,
            jobs=None,
            localversion_rev=args.localversion_rev,
            skip_git_operations=True,
            dry_run=False,
            arch=args.arch,
            cross_compile=args.cross_compile,
            with_headers=args.with_headers
            )


if __name__ == '__main__':
    sys.exit(main())
