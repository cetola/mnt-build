#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
MNT Reform Kernel Build Script
Compiles kernel, out-of-tree modules, and creates deployment tarball.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from builder import KernelBuilder
from config import BuildConfig, DEFAULT_CROSS_COMPILE, DEFAULT_KERNEL_VERSION, DEFAULT_PKGREL
from errors import BuildError
from logging_setup import Colors, setup_logging

__version__ = "0.7.0"


def run_build(version: str = DEFAULT_KERNEL_VERSION, build_dir: Optional[Path] = None,
              jobs: Optional[int] = None, pkgrel: int = DEFAULT_PKGREL,
              skip_git_operations: bool = False, dry_run: bool = False,
              run_olddefconfig: bool = False,
              cross_compile: str = DEFAULT_CROSS_COMPILE,
              with_headers: bool = False) -> int:
    """Run the kernel build process.

    Args:
        version: Kernel version to build
        build_dir: Build directory (default: ~/mnt-build)
        jobs: Number of parallel jobs (default: number of CPUs)
        pkgrel: Package release number
        skip_git_operations: If True, skip git reset/checkout operations
        dry_run: If True, only check prerequisites, do not build
        run_olddefconfig: If True, update config using olddefconfig before building
        cross_compile: Compiler prefix for kernel build tools. Use empty string for native build.
        with_headers: If True, also generate an external-module headers tree.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Normalize special values that explicitly disable cross-compilation.
    if cross_compile is None:
        cross_compile = DEFAULT_CROSS_COMPILE
    normalized_cross_compile = cross_compile.strip()
    if normalized_cross_compile.lower() in {"none", "native", "off", "false"}:
        normalized_cross_compile = ""

    config = BuildConfig.create(version=version, build_dir=build_dir, jobs=jobs, pkgrel=pkgrel)

    config.build_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(config.log_file)

    builder = KernelBuilder(config, logger, cross_compile=normalized_cross_compile)

    try:
        logger.info("=" * 60)
        logger.info("Starting kernel build process")
        logger.info(f"Version: {config.version}")
        logger.info(f"Package release: {config.pkgrel}")
        logger.info(f"Build directory: {config.build_dir}")
        logger.info(f"Patches directory: {config.patches_dir}")
        logger.info(f"Log file: {config.log_file}")
        logger.info(f"Parallel jobs: {config.jobs}")
        logger.info(f"Cross compile prefix: {normalized_cross_compile if normalized_cross_compile else '(native/no prefix)'}")
        logger.info(f"Generate extmod headers tree: {'yes' if with_headers else 'no'}")
        logger.info("=" * 60)

        start_time = datetime.now()

        builder.check_prerequisites(run_olddefconfig=run_olddefconfig)

        if dry_run:
            logger.info("Dry run mode - exiting after prerequisites check")
            return 0

        builder.build_kernel(skip_git_operations=skip_git_operations, run_olddefconfig=run_olddefconfig)
        builder.build_lpc_module()
        builder.build_qcacld2_module()
        builder.create_module_tarballs()
        if with_headers:
            headers_dir = builder.install_extmod_build_tree()
            builder.create_headers_tarball(headers_dir)
        builder.create_tarball()

        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info("=" * 60)
        logger.info(f"{Colors.GREEN}✓ Build completed successfully in {elapsed:.0f} seconds!{Colors.RESET}")
        logger.info(f"Output: {config.output_tar}")
        logger.info(f"LPC module output: {config.build_dir / config.output_lpc_module_tar.name}")
        logger.info(f"WiFi module output: {config.build_dir / config.output_wifi_module_tar.name}")
        if with_headers:
            logger.info(f"Headers output: {config.build_dir / config.output_headers_tar.name}")
        logger.info(f"Log file: {config.log_file}")
        logger.info("=" * 60)

        return 0

    except BuildError as e:
        logger.error(f"Build failed: {e}")
        logger.error(f"Check log file for details: {config.log_file}")
        return 1
    except KeyboardInterrupt:
        logger.warning("Build interrupted by user")
        return 130
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return 1


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Build MNT Pocket Reform kernel and modules.',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        'version',
        nargs='?',
        default=DEFAULT_KERNEL_VERSION,
        help=f'Kernel version to build (default: {DEFAULT_KERNEL_VERSION})'
    )
    parser.add_argument(
        '--build-dir',
        type=Path,
        help='Build directory (default: ~/mnt-build)'
    )
    parser.add_argument(
        '-j', '--jobs',
        type=int,
        help='Number of parallel jobs (default: number of CPUs)'
    )
    parser.add_argument(
        '--pkgrel',
        type=int,
        default=DEFAULT_PKGREL,
        help=f'Package release number (default: {DEFAULT_PKGREL})'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Check prerequisites only, do not build'
    )
    parser.add_argument(
        '--olddefconfig',
        action='store_true',
        help='Update kernel config using olddefconfig before building. '
             'Copies configs/defconfig to .config, runs olddefconfig, '
             'then saves the updated config back to configs/config-[VERSION]-mnt-reform-arm64'
    )
    parser.add_argument(
        '--skip-git-ops',
        action='store_true',
        default=False,
        help='Skip git reset, checkout, and tag operations. '
             'Assumes the kernel repository is already at the correct version and state. '
             'Useful for automated builds or when you have manually prepared the repository. '
             'When enabled, the script will not reset the repo, fetch tags, checkout the version, '
             'or create git commits/tags during the build process.'
    )
    parser.add_argument(
        '--cross-compile',
        default=DEFAULT_CROSS_COMPILE,
        help=f'CROSS_COMPILE prefix (default: {DEFAULT_CROSS_COMPILE}). '
             'Use "" or "none" for native build with no prefix.'
    )
    parser.add_argument(
        '--with-headers',
        action='store_true',
        help='Also generate an external-module headers tree using '
             'scripts/package/install-extmod-build.'
    )
    parser.add_argument(
        '--version',
        action='version',
        help='Prints the version of the build script.',
        version=f"%(prog)s {__version__}"
    )

    args = parser.parse_args()

    return run_build(
        version=args.version,
        build_dir=args.build_dir,
        jobs=args.jobs,
        pkgrel=args.pkgrel,
        skip_git_operations=args.skip_git_ops,
        dry_run=args.dry_run,
        run_olddefconfig=args.olddefconfig,
        cross_compile=args.cross_compile,
        with_headers=args.with_headers
    )


if __name__ == '__main__':
    sys.exit(main())
