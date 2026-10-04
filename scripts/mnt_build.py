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
from config import (
    BuildConfig,
    DEFAULT_CROSS_COMPILE,
    DEFAULT_KERNEL_DIR,
    DEFAULT_KERNEL_ONLY,
    DEFAULT_KERNEL_VERSION,
    DEFAULT_LOCALVERSION_REV,
)
from errors import BuildError
from logging_setup import Colors, setup_logging
from barebox import BareboxManager, print_barebox_table
from uboot import UBootManager, print_sysimage_table

__version__ = "1.5.8"


def run_build(version: str = DEFAULT_KERNEL_VERSION, build_dir: Optional[Path] = None,
              jobs: Optional[int] = None,
              localversion_rev: int = DEFAULT_LOCALVERSION_REV,
              skip_git_operations: bool = False, dry_run: bool = False,
              run_olddefconfig: bool = False,
              post_clean: bool = False,
              arch: str = "arm64",
              cross_compile: str = DEFAULT_CROSS_COMPILE,
              with_headers: bool = False,
              kernel_only: bool = DEFAULT_KERNEL_ONLY,
              dtbs_only: bool = False,
              modules_only: bool = False,
              kernel: str = DEFAULT_KERNEL_DIR) -> int:
    if cross_compile is None:
        cross_compile = DEFAULT_CROSS_COMPILE
    normalized_cross_compile = cross_compile.strip()
    if normalized_cross_compile.lower() in {"none", "native", "off", "false"}:
        normalized_cross_compile = ""

    config = BuildConfig.create(
        version=version,
        arch=arch,
        build_dir=build_dir,
        jobs=jobs,
        localversion_rev=localversion_rev,
        kernel=kernel,
    )

    config.build_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(config.log_file)

    builder = KernelBuilder(
        config,
        logger,
        arch=arch,
        cross_compile=normalized_cross_compile,
        kernel_only=kernel_only,
        dtbs_only=dtbs_only,
        modules_only=modules_only,
    )

    try:
        logger.info("=" * 60)
        logger.info("Starting kernel build process")
        logger.info(f"Version: {config.version}")
        logger.info(f"Build version: {config.build_version}")
        logger.info(f"Kernel release: {config.kernel_release}")
        logger.info(f"Kernel localversion: {config.localversion}")
        logger.info(f"Build directory: {config.build_dir}")
        logger.info(f"Kernel directory: {config.linux_dir}")
        logger.info(f"Extra patches directory: {config.xtra_patches_dir}")
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

        if dry_run:
            builder.log_phase("Source Prep")
            builder.prepare_kernel_source(skip_git_operations)

        if dry_run and run_olddefconfig:
            logger.info(
                "Dry run + olddefconfig selected: will update config file, "
                "then exit without building."
            )
            builder.log_phase("Patching")
            builder.apply_patches()
            builder.log_phase("Config Update")
            builder.update_config_with_olddefconfig()
            if post_clean:
                builder.log_phase("Cleanup")
                logger.info("Post-clean selected: removing in-tree kernel artifacts.")
                builder.clean_in_tree_kernel_artifacts()
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
        elif dtbs_only:
            builder.log_phase("Collect DTBs")
            dtbs_dir = builder.collect_dtbs()
        elif modules_only:
            logger.info("Modules only mode - skipping kernel image, DTBs, and tarball creation")
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
        if kernel_only:
            logger.info(f"Kernel image: {builder.kernel_image_path()}")
        elif dtbs_only:
            logger.info(f"DTBs: {dtbs_dir}")
        elif modules_only:
            logger.info(f"Modules: {builder.modules_install_path()}")
        else:
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
    finally:
        try:
            builder.restore_kernel_tree()
        except BuildError as e:
            logger.error(f"Could not restore the kernel tree: {e}")


def run_clean(build_dir: Optional[Path] = None, kernel: str = DEFAULT_KERNEL_DIR) -> int:
    config = BuildConfig.create(version=DEFAULT_KERNEL_VERSION, build_dir=build_dir, kernel=kernel)
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


def run_dev_kernel(build_dir: Optional[Path] = None, kernel: str = DEFAULT_KERNEL_DIR,
                   action: Optional[str] = None, offline: bool = False,
                   log: bool = False, version: str = DEFAULT_KERNEL_VERSION,
                   source: Optional[str] = None) -> int:
    config = BuildConfig.create(version=version, build_dir=build_dir, kernel=kernel)
    if log:
        config.build_dir.mkdir(parents=True, exist_ok=True)
    else:
        config.log_file = None
    logger = setup_logging(config.log_file)
    builder = KernelBuilder(config, logger)

    try:
        logger.info("=" * 60)
        logger.info("Kernel development checkout")
        logger.info(f"Build directory: {config.build_dir}")
        logger.info(f"Kernel directory: {config.linux_dir}")
        if log:
            logger.info(f"Log file: {config.log_file}")
        logger.info("=" * 60)

        if action == 'rebase':
            builder.log_phase("Rebase")
            builder.rebase_mnt_linux_branch(source=source)
            return 0

        builder.log_phase("Remotes")
        if action == 'add-remotes':
            builder.ensure_kernel_remotes()
        elif action == 'fetch':
            builder.fetch_kernel_remotes()
        else:
            builder.show_kernel_remotes(offline=offline)
            builder.log_phase("Versions")
            builder.show_kernel_versions(offline=offline)
        return 0
    except BuildError as e:
        logger.error(f"dev-kernel failed: {e}")
        if log:
            logger.error(f"Check log file for details: {config.log_file}")
        else:
            logger.error("Re-run with --log to capture details in a log file.")
        return 1
    except KeyboardInterrupt:
        logger.warning("dev-kernel interrupted by user")
        return 130
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return 1


def run_uboot_list() -> int:
    mnt_build_root = Path(__file__).parent.parent
    manager = UBootManager(mnt_build_root)
    try:
        infos = manager.list_sysimages()
    except Exception as e:
        print(f"Error: failed to enumerate sysimage configs: {e}", file=sys.stderr)
        return 1
    if not infos:
        print("No supported sysimages found.", file=sys.stderr)
        print("  Looked for machine configs in:", file=sys.stderr)
        print(f"    {mnt_build_root / 'local-machines'}", file=sys.stderr)
        print(f"    {mnt_build_root / 'reform-tools' / 'machines'}", file=sys.stderr)
        print("  Looked for sysimage list in:", file=sys.stderr)
        print(f"    {mnt_build_root / 'scripts' / 'sysimage-config.sh'}", file=sys.stderr)
        return 1
    print_sysimage_table(infos)
    return 0


def run_uboot_build(sysimage: str) -> int:
    """Prepares the checkout if needed, then builds."""
    mnt_build_root = Path(__file__).parent.parent
    manager = UBootManager(mnt_build_root)
    try:
        return manager.build(sysimage)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def run_uboot_diff(sysimage: str) -> int:
    """Builds if needed, then compares against the MNT prebuilt artifact."""
    mnt_build_root = Path(__file__).parent.parent
    manager = UBootManager(mnt_build_root)
    try:
        return manager.diff_vs_prebuilt(sysimage)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def run_uboot_reset(sysimage: str) -> int:
    """Reset the inner u-boot/ sub-repo to the SHA expected by build.sh."""
    mnt_build_root = Path(__file__).parent.parent
    manager = UBootManager(mnt_build_root)
    try:
        return manager.reset(sysimage)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def run_uboot_clean(sysimage: str) -> int:
    mnt_build_root = Path(__file__).parent.parent
    manager = UBootManager(mnt_build_root)
    try:
        return manager.clean(sysimage)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def run_uboot_menuconfig(sysimage: str) -> int:
    mnt_build_root = Path(__file__).parent.parent
    manager = UBootManager(mnt_build_root)
    try:
        return manager.menuconfig(sysimage)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def run_uboot_dry_run(sysimage: str) -> int:
    """Clone/fetch U-Boot for a sysimage, checkout tag, apply patches. No build."""
    mnt_build_root = Path(__file__).parent.parent
    manager = UBootManager(mnt_build_root)
    try:
        return manager.prepare(sysimage)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def run_barebox_list() -> int:
    mnt_build_root = Path(__file__).parent.parent
    manager = BareboxManager(mnt_build_root)
    try:
        infos = manager.list_sysimages()
    except Exception as e:
        print(f"Error: failed to enumerate sysimage configs: {e}", file=sys.stderr)
        return 1
    if not infos:
        print("No supported sysimages found.", file=sys.stderr)
        return 1
    print_barebox_table(infos)
    return 0


def run_barebox_build(sysimage: str) -> int:
    """Prepares the checkout if needed, then builds."""
    mnt_build_root = Path(__file__).parent.parent
    manager = BareboxManager(mnt_build_root)
    try:
        return manager.build(sysimage)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def run_barebox_diff(sysimage: str) -> int:
    """Builds if needed, then compares against the MNT CI artifact."""
    mnt_build_root = Path(__file__).parent.parent
    manager = BareboxManager(mnt_build_root)
    try:
        return manager.diff_vs_prebuilt(sysimage)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def run_barebox_menuconfig(sysimage: str) -> int:
    mnt_build_root = Path(__file__).parent.parent
    manager = BareboxManager(mnt_build_root)
    try:
        return manager.menuconfig(sysimage)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def run_barebox_dry_run(sysimage: str) -> int:
    """Clone barebox repo, checkout tag, apply patches. No build."""
    mnt_build_root = Path(__file__).parent.parent
    manager = BareboxManager(mnt_build_root)
    try:
        return manager.prepare(sysimage)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def run_barebox_clean(sysimage: str) -> int:
    mnt_build_root = Path(__file__).parent.parent
    manager = BareboxManager(mnt_build_root)
    try:
        return manager.clean(sysimage)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
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
        '--kernel',
        default=DEFAULT_KERNEL_DIR,
        help='Kernel checkout to use, as a directory name under build_dir '
             f'(default: {DEFAULT_KERNEL_DIR}). The build switches it to branch '
             'mnt-v{kversion}, taking the branch from the first remote that has it.'
    )
    build_parser.add_argument(
        '-j', '--jobs',
        type=int,
        help='Number of parallel jobs (default: number of CPUs)'
    )
    build_parser.add_argument(
        '--localversion-rev',
        type=int,
        default=DEFAULT_LOCALVERSION_REV,
        help=f'Kernel localversion revision (default: {DEFAULT_LOCALVERSION_REV})'
    )
    build_parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Check prerequisites, check out the kernel branch and apply '
             'xtra-patches, do not build. The patches are taken back out afterwards. '
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
        '--post-clean',
        action='store_true',
        help='After --olddefconfig --dry-run, run make mrproper to remove in-tree '
             'Kbuild artifacts.'
    )
    build_parser.add_argument(
        '--skip-git-ops',
        action='store_true',
        default=False,
        help='Do not switch branches or fetch. Build the kernel checkout as it stands. '
             'Useful for automated builds that pin the kernel commit, or when you have '
             'checked out something by hand. The checkout must still be free of '
             'uncommitted changes and match --kversion.'
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
    mode_group = build_parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        '--kernel-only',
        action='store_true',
        default=DEFAULT_KERNEL_ONLY,
        help='Build kernel image only; skip DTBs, modules, and tarballs.'
    )
    mode_group.add_argument(
        '--dtbs-only',
        action='store_true',
        help='Build DTBs only; skip kernel image, modules, and tarballs. ARM64 only.'
    )
    mode_group.add_argument(
        '--modules-only',
        action='store_true',
        help='Build and install in-tree kernel modules only; skip kernel image, DTBs, and tarballs.'
    )

    clean_parser = subparsers.add_parser(
        'clean',
        help='Discard uncommitted changes and untracked files in the kernel checkout'
    )
    clean_parser.add_argument(
        '--build-dir',
        type=Path,
        help='Build directory (default: ~/mnt-build)'
    )
    clean_parser.add_argument(
        '--kernel',
        default=DEFAULT_KERNEL_DIR,
        help=f'Kernel checkout dir under build_dir to clean (default: {DEFAULT_KERNEL_DIR}). '
             'Commits, branches and tags are left alone.'
    )

    dev_kernel_parser = subparsers.add_parser(
        'dev-kernel',
        help='Show the git remotes of a kernel checkout and their status'
    )
    dev_kernel_parser.add_argument(
        'action',
        nargs='?',
        choices=['add-remotes', 'fetch', 'rebase'],
        help='With no action, list the remotes in the checkout and whether each '
             'is fetched and up to date, then report whether an mnt-v branch '
             'exists for the latest stable kernel. add-remotes: add any remote listed in '
             'kernel-remotes.data that the checkout lacks (nothing is fetched). '
             'fetch: fetch the latest from every remote in the checkout. '
             'rebase: create local branch mnt-v{kversion} by rebasing the newest '
             'mnt-v branch of the same series onto stable tag v{kversion}. Works '
             'from what is already fetched, pushes nothing, and leaves the rebase '
             'in progress on a conflict.'
    )
    dev_kernel_parser.add_argument(
        '--kversion',
        default=DEFAULT_KERNEL_VERSION,
        help='Kernel version. Picks the series (X.Y) the status report covers, '
             f'and is the version rebase targets (default: {DEFAULT_KERNEL_VERSION})'
    )
    dev_kernel_parser.add_argument(
        '--from',
        dest='source',
        metavar='BRANCH',
        help='With rebase: the branch to rebase from, e.g. mnt/mnt-v7.2.6 '
             '(default: the newest mnt-v branch of the same series).'
    )
    dev_kernel_parser.add_argument(
        '--build-dir',
        type=Path,
        help='Build directory (default: ~/mnt-build)'
    )
    dev_kernel_parser.add_argument(
        '--kernel',
        default=DEFAULT_KERNEL_DIR,
        help=f'Kernel checkout dir under build_dir (default: {DEFAULT_KERNEL_DIR}).'
    )
    dev_kernel_parser.add_argument(
        '--offline',
        action='store_true',
        help='Do not contact the remotes. Reports only whether each one has '
             'been fetched, not whether it is up to date.'
    )
    dev_kernel_parser.add_argument(
        '--log',
        action='store_true',
        help='Also write the output to a log file in the build directory '
             '(default: console only).'
    )

    uboot_parser = subparsers.add_parser('uboot', help='U-Boot development workflow')
    uboot_parser.add_argument(
        '--list',
        choices=['sysimage'],
        metavar='sysimage',
        help='List all supported sysimages and their U-Boot configuration'
    )
    uboot_parser.add_argument(
        '--sysimage',
        metavar='name',
        help='Target sysimage (required for --dry-run and other build actions)'
    )
    uboot_parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Clone/fetch U-Boot repo, checkout tag, apply patches — stop before building'
    )
    uboot_parser.add_argument(
        '--diff',
        action='store_true',
        help='Build (if needed) then byte-compare against the MNT prebuilt artifact'
    )
    uboot_parser.add_argument(
        '--menuconfig',
        action='store_true',
        help='Run make menuconfig in the U-Boot checkout (builds first if no .config exists)'
    )
    uboot_parser.add_argument(
        '--clean',
        action='store_true',
        help='Remove the U-Boot checkout entirely (uboot/<project>/)'
    )
    uboot_parser.add_argument(
        '--reset',
        action='store_true',
        help='Reset the inner u-boot/ sub-repo to the SHA expected by build.sh'
    )

    barebox_parser = subparsers.add_parser('barebox', help='Barebox development workflow')
    barebox_parser.add_argument(
        '--list',
        choices=['sysimage'],
        metavar='sysimage',
        help='List all supported sysimages and their barebox configuration'
    )
    barebox_parser.add_argument(
        '--sysimage',
        metavar='name',
        help='Target sysimage (required for --dry-run and other build actions)'
    )
    barebox_parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Clone barebox repo, checkout tag, apply patches — stop before building'
    )
    barebox_parser.add_argument(
        '--diff',
        action='store_true',
        help='Build (if needed) then byte-compare against the MNT CI artifact'
    )
    barebox_parser.add_argument(
        '--menuconfig',
        action='store_true',
        help='Run make menuconfig in the barebox checkout'
    )
    barebox_parser.add_argument(
        '--clean',
        action='store_true',
        help='Remove the barebox checkout entirely (barebox/<project>/)'
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
            localversion_rev=args.localversion_rev,
            skip_git_operations=args.skip_git_ops,
            dry_run=args.dry_run,
            run_olddefconfig=args.olddefconfig,
            post_clean=args.post_clean,
            arch=args.arch,
            cross_compile=args.cross_compile,
            with_headers=args.with_headers,
            kernel_only=args.kernel_only,
            dtbs_only=args.dtbs_only,
            modules_only=args.modules_only,
            kernel=args.kernel,
        )

    if args.command == 'clean':
        return run_clean(build_dir=args.build_dir, kernel=args.kernel)

    if args.command == 'dev-kernel':
        return run_dev_kernel(build_dir=args.build_dir, kernel=args.kernel,
                              action=args.action, offline=args.offline, log=args.log,
                              version=args.kversion, source=args.source)

    if args.command == 'uboot':
        if args.list == 'sysimage':
            return run_uboot_list()
        if args.dry_run:
            if not args.sysimage:
                parser.error("mnt-build uboot --dry-run requires --sysimage <name>")
            return run_uboot_dry_run(args.sysimage)
        if args.diff:
            if not args.sysimage:
                parser.error("mnt-build uboot --diff requires --sysimage <name>")
            return run_uboot_diff(args.sysimage)
        if args.menuconfig:
            if not args.sysimage:
                parser.error("mnt-build uboot --menuconfig requires --sysimage <name>")
            return run_uboot_menuconfig(args.sysimage)
        if args.clean:
            if not args.sysimage:
                parser.error("mnt-build uboot --clean requires --sysimage <name>")
            return run_uboot_clean(args.sysimage)
        if args.reset:
            if not args.sysimage:
                parser.error("mnt-build uboot --reset requires --sysimage <name>")
            return run_uboot_reset(args.sysimage)
        if args.sysimage:
            return run_uboot_build(args.sysimage)
        parser.error("mnt-build uboot requires an action (e.g. --list sysimage or --sysimage <name> --dry-run)")

    if args.command == 'barebox':
        if args.list == 'sysimage':
            return run_barebox_list()
        if args.dry_run:
            if not args.sysimage:
                parser.error("mnt-build barebox --dry-run requires --sysimage <name>")
            return run_barebox_dry_run(args.sysimage)
        if args.diff:
            if not args.sysimage:
                parser.error("mnt-build barebox --diff requires --sysimage <name>")
            return run_barebox_diff(args.sysimage)
        if args.menuconfig:
            if not args.sysimage:
                parser.error("mnt-build barebox --menuconfig requires --sysimage <name>")
            return run_barebox_menuconfig(args.sysimage)
        if args.clean:
            if not args.sysimage:
                parser.error("mnt-build barebox --clean requires --sysimage <name>")
            return run_barebox_clean(args.sysimage)
        if args.sysimage:
            return run_barebox_build(args.sysimage)
        parser.error("mnt-build barebox requires an action "
                     "(e.g. --list sysimage or --sysimage <name> --dry-run)")

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == '__main__':
    sys.exit(main())
