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
from config import BuildConfig, DEFAULT_CROSS_COMPILE, DEFAULT_KERNEL_ONLY, DEFAULT_KERNEL_VERSION, DEFAULT_PKGREL
from errors import BuildError
from logging_setup import Colors, setup_logging

__version__ = "0.9.2"


def run_build(version: str = DEFAULT_KERNEL_VERSION, build_dir: Optional[Path] = None,
              jobs: Optional[int] = None, pkgrel: int = DEFAULT_PKGREL,
              skip_git_operations: bool = False, dry_run: bool = False,
              run_olddefconfig: bool = False,
              arch: str = "arm64",
              cross_compile: str = DEFAULT_CROSS_COMPILE,
              with_headers: bool = False,
              kernel_only: bool = DEFAULT_KERNEL_ONLY) -> int:
    """Run the kernel build process."""
    if cross_compile is None:
        cross_compile = DEFAULT_CROSS_COMPILE
    normalized_cross_compile = cross_compile.strip()
    if normalized_cross_compile.lower() in {"none", "native", "off", "false"}:
        normalized_cross_compile = ""

    config = BuildConfig.create(version=version, arch=arch, build_dir=build_dir, jobs=jobs, pkgrel=pkgrel)

    config.build_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(config.log_file)

    builder = KernelBuilder(
        config,
        logger,
        arch=arch,
        cross_compile=normalized_cross_compile,
        kernel_only=kernel_only,
    )

    try:
        logger.info("=" * 60)
        logger.info("Starting kernel build process")
        logger.info(f"Version: {config.version}")
        logger.info(f"Package release: {config.pkgrel}")
        logger.info(f"Build directory: {config.build_dir}")
        logger.info(f"Patches directory: {config.patches_dir}")
        logger.info(f"Log file: {config.log_file}")
        logger.info(f"Parallel jobs: {config.jobs}")
        logger.info(f"Target arch: {arch}")
        logger.info(f"Defconfig file: {config.defconfig_file}")
        logger.info(f"Kernel config file: {config.config_file}")
        logger.info(f"Cross compile prefix: {normalized_cross_compile if normalized_cross_compile else '(native/no prefix)'}")
        logger.info(f"Generate extmod headers tree: {'yes' if with_headers else 'no'}")
        logger.info(f"Kernel only mode: {'yes (skipping modules and tarball)' if kernel_only else 'no'}")
        logger.info("=" * 60)

        start_time = datetime.now()

        builder.log_phase("Preflight")
        builder.check_prerequisites(run_olddefconfig=run_olddefconfig)

        if dry_run and not skip_git_operations:
            builder.log_phase("Source Prep")
            logger.info(
                "Dry run mode: syncing kernel repository to target version "
                f"v{config.version} (same checkout behavior as build)."
            )
            builder.checkout_kernel_version()

        if dry_run and run_olddefconfig:
            logger.info(
                "Dry run + olddefconfig selected: will update config file, "
                "then exit without building."
            )
            builder.log_phase("Patching")
            builder.apply_patches()
            builder.log_phase("Config Update")
            builder.update_config_with_olddefconfig(skip_git_operations=skip_git_operations)
            logger.info("Dry run mode - config updated via olddefconfig; no build performed")
            return 0

        if dry_run:
            builder.log_phase("Patching")
            builder.apply_patches()
            logger.info("Dry run mode - exiting after prerequisites check and apply patches")
            return 0

        builder.log_phase("Kernel Build")
        builder.build_kernel(skip_git_operations=skip_git_operations, run_olddefconfig=run_olddefconfig)
        if with_headers:
            builder.log_phase("Headers")
            headers_dir = builder.install_extmod_build_tree()
            builder.create_headers_tarball(headers_dir)

        if kernel_only:
            logger.info("Kernel only mode - skipping module builds and tarball creation")
        else:
            builder.log_phase("Out-of-Tree Modules")
            builder.build_lpc_module()
            builder.build_qcacld2_module()
            builder.log_phase("Packaging")
            builder.create_module_tarballs()
            builder.create_tarball()

        elapsed = (datetime.now() - start_time).total_seconds()
        builder.log_phase("Summary")
        logger.info("=" * 60)
        logger.info(f"{Colors.GREEN}✓ Build completed successfully in {elapsed:.0f} seconds!{Colors.RESET}")
        if not kernel_only:
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


def run_clean(build_dir: Optional[Path] = None) -> int:
    """Clean kernel repository state for development."""
    config = BuildConfig.create(version=DEFAULT_KERNEL_VERSION, build_dir=build_dir)
    config.build_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(config.log_file)
    builder = KernelBuilder(config, logger)

    try:
        logger.info("=" * 60)
        logger.info("Starting repository clean")
        logger.info(f"Build directory: {config.build_dir}")
        logger.info(f"Kernel directory: {config.linux_dir}")
        logger.info(f"Log file: {config.log_file}")
        logger.info("=" * 60)

        builder.log_phase("Clean")
        builder.clean_kernel_repo()

        builder.log_phase("Summary")
        logger.info("=" * 60)
        logger.info(f"{Colors.GREEN}✓ Clean completed successfully{Colors.RESET}")
        logger.info("=" * 60)
        return 0
    except BuildError as e:
        logger.error(f"Clean failed: {e}")
        logger.error(f"Check log file for details: {config.log_file}")
        return 1
    except KeyboardInterrupt:
        logger.warning("Clean interrupted by user")
        return 130
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='mnt-build',
        description='MNT Reform kernel tooling.',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    subparsers = parser.add_subparsers(dest='command', required=True)

    build_parser = subparsers.add_parser('build', help='Build kernel and artifacts')
    build_parser.add_argument(
        '--kversion',
        dest='kernel_version',
        default=DEFAULT_KERNEL_VERSION,
        help=f'Kernel version to build (default: {DEFAULT_KERNEL_VERSION})'
    )
    build_parser.add_argument(
        '--build-dir',
        type=Path,
        help='Build directory (default: ~/mnt-build)'
    )
    build_parser.add_argument(
        '-j', '--jobs',
        type=int,
        help='Number of parallel jobs (default: number of CPUs)'
    )
    build_parser.add_argument(
        '--pkgrel',
        type=int,
        default=DEFAULT_PKGREL,
        help=f'Package release number (default: {DEFAULT_PKGREL})'
    )
    build_parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Check prerequisites and apply patches, do not build. '
             'If combined with --olddefconfig, updates config and exits without building.'
    )
    build_parser.add_argument(
        '--olddefconfig',
        action='store_true',
        help='Update kernel config using olddefconfig before building. '
             'Copies configs/defconfig_[arch] to .config, runs olddefconfig, '
             'then saves the updated config back to configs/config-[VERSION]-mnt-reform-[arch]'
    )
    build_parser.add_argument(
        '--defconfig',
        dest='olddefconfig',
        action='store_true',
        help='Alias for --olddefconfig'
    )
    build_parser.add_argument(
        '--skip-git-ops',
        action='store_true',
        default=False,
        help='Skip git reset, checkout, and tag operations. '
             'Assumes the kernel repository is already at the correct version and state. '
             'Useful for automated builds or when you have manually prepared the repository. '
             'When enabled, the script will not reset the repo, fetch tags, checkout the version, '
             'or create git commits/tags during the build process.'
    )
    build_parser.add_argument(
        '--arch',
        default='arm64',
        help='Kernel ARCH to build for (default: arm64).'
    )
    build_parser.add_argument(
        '--cross-compile',
        default=DEFAULT_CROSS_COMPILE,
        help=f'CROSS_COMPILE prefix (default: {DEFAULT_CROSS_COMPILE}). '
             'Use "" or "none" for native build with no prefix. '
             'This does not change ARCH; use --arch to override that.'
    )
    build_parser.add_argument(
        '--with-headers',
        action='store_true',
        help='Also generate an external-module headers tree using '
             'scripts/package/install-extmod-build.'
    )
    build_parser.add_argument(
        '--kernel-only',
        action='store_true',
        default=DEFAULT_KERNEL_ONLY,
        help='Build only the kernel. Skips dtb, modules, and out-of-tree '
             'modules. Handy for testing a newly patched kernel without '
             'rebuilding all the other artifacts. No tarball artifacts '
             'are produced.'
    )

    clean_parser = subparsers.add_parser('clean', help='Clean local kernel git state')
    clean_parser.add_argument(
        '--build-dir',
        type=Path,
        help='Build directory (default: ~/mnt-build)'
    )

    parser.add_argument(
        '--version',
        action='version',
        help='Prints the version of the build script.',
        version=f"%(prog)s {__version__}"
    )

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == 'build':
        return run_build(
            version=args.kernel_version,
            build_dir=args.build_dir,
            jobs=args.jobs,
            pkgrel=args.pkgrel,
            skip_git_operations=args.skip_git_ops,
            dry_run=args.dry_run,
            run_olddefconfig=args.olddefconfig,
            arch=args.arch,
            cross_compile=args.cross_compile,
            with_headers=args.with_headers,
            kernel_only=args.kernel_only
        )

    if args.command == 'clean':
        return run_clean(build_dir=args.build_dir)

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == '__main__':
    sys.exit(main())
