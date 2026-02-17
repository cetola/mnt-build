#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
MNT Pocket Reform Kernel Build Script
Compiles kernel, out-of-tree modules, and creates deployment tarball.
"""

import argparse
import logging
import os
import subprocess
import sys
import tarfile
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

__version__ = "0.4.0"

DEFAULT_KERNEL_VERSION = '6.18.10'
DEFAULT_PKGREL = 1
DEFAULT_CROSS_COMPILE = "aarch64-linux-gnu-"


# DTS/DTB configuration - single source of truth for all DTS files
DTS_CONFIGS = [
    {
        "name": "imx8mp-mnt-pocket-reform.dts",
        "vendor": "freescale",
        "config": "CONFIG_ARCH_MXC"
    },
    {
        "name": "meson-g12b-bananapi-cm4-mnt-pocket-reform.dts",
        "vendor": "amlogic",
        "config": "CONFIG_ARCH_MESON"
    },
    {
        "name": "rk3588-mnt-pocket-reform.dts",
        "vendor": "rockchip",
        "config": "CONFIG_ARCH_ROCKCHIP"
    },
    {
        "name": "rk3588-mnt-reform-next.dts",
        "vendor": "rockchip",
        "config": "CONFIG_ARCH_ROCKCHIP"
    }
]


# ANSI color codes for terminal output
class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    RESET = '\033[0m'


@dataclass
class BuildConfig:
    """Configuration for kernel build."""
    version: str
    build_dir: Path
    linux_dir: Path
    patches_dir: Path
    config_file: Path
    dtb_files: list[Path]
    output_tar: Path
    output_headers_tar: Path
    log_file: Path
    jobs: int
    pkgrel: int

    @classmethod
    def create(cls, version: str, build_dir: Optional[Path] = None, jobs: Optional[int] = None, pkgrel: Optional[int] = None):
        """Create build configuration with sensible defaults."""
        if build_dir is None:
            build_dir = Path.home() / "mnt-build"

        if jobs is None:
            jobs = os.cpu_count() or 4

        if pkgrel is None:
            pkgrel = DEFAULT_PKGREL

        linux_dir = build_dir / "linux"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        # Extract major.minor version (e.g., "6.17" from "6.17.8")
        version_parts = version.split('.')
        major_minor = f"{version_parts[0]}.{version_parts[1]}"
        
        # Generate DTB file paths from DTS_CONFIGS
        dtb_files = [
            linux_dir / f"arch/arm64/boot/dts/{dts_config['vendor']}/{dts_config['name'].replace('.dts', '.dtb')}"
            for dts_config in DTS_CONFIGS
        ]

        return cls(
                version=version,
                build_dir=build_dir,
                linux_dir=linux_dir,
                patches_dir=build_dir / "reform-debian-packages" / "linux" / f"patches{major_minor}",
                config_file=build_dir / "configs" / f"config-{version}-mnt-reform-arm64",
                dtb_files=dtb_files,
                output_tar=linux_dir / f"kernel-{version}-{pkgrel}-mnt.tar.gz",
                output_headers_tar=linux_dir / f"headers-{version}-{pkgrel}-mnt.tar.gz",
                log_file=build_dir / f"build-{version}-{timestamp}.log",
                jobs=jobs,
                pkgrel=pkgrel
                )


class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors for different log levels."""

    FORMATS = {
            logging.DEBUG: f"{Colors.BLUE}[DEBUG]{Colors.RESET} %(asctime)s - %(message)s",
            logging.INFO: f"{Colors.BLUE}[INFO]{Colors.RESET} %(asctime)s - %(message)s",
            logging.WARNING: f"{Colors.YELLOW}[WARN]{Colors.RESET} %(asctime)s - %(message)s",
            logging.ERROR: f"{Colors.RED}[ERROR]{Colors.RESET} %(asctime)s - %(message)s",
            logging.CRITICAL: f"{Colors.RED}[CRITICAL]{Colors.RESET} %(asctime)s - %(message)s",
            }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt='%Y-%m-%d %H:%M:%S')
        return formatter.format(record)


def setup_logging(log_file: Path) -> logging.Logger:
    """Setup logging to both console and file."""
    logger = logging.getLogger('kernel_build')
    logger.setLevel(logging.DEBUG)

    # Console handler with colors
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ColoredFormatter())

    # File handler without colors
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
            logging.Formatter('%(levelname)s %(asctime)s - %(message)s',
                              datefmt='%Y-%m-%d %H:%M:%S')
            )

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger


class BuildError(Exception):
    """Custom exception for build failures."""
    pass


class PatchStats:
    """Track patch application statistics."""
    def __init__(self):
        self.success = 0
        self.failed = 0
        self.failed_patches = []

    @property
    def total(self) -> int:
        return self.success + self.failed

    def add_success(self):
        self.success += 1

    def add_failure(self, patch_name: str):
        self.failed += 1
        self.failed_patches.append(patch_name)


class KernelBuilder:
    """Handles kernel and module building."""

    def __init__(self, config: BuildConfig, logger: logging.Logger, cross_compile: str = DEFAULT_CROSS_COMPILE):
        self.config = config
        self.logger = logger
        self.arch = "arm64"
        self.cross_compile = cross_compile

    def run_command(self, cmd: List[str], cwd: Optional[Path] = None,
                    check: bool = True, input_data: Optional[str] = None,
                    stream_output: bool = False) -> subprocess.CompletedProcess:
        """Run a shell command. If stream_output is True, stream stdout/stderr live to logger and file."""
        cwd = cwd or Path.cwd()
        cmd_str = ' '.join(cmd)
        self.logger.info(f"$ {cmd_str}")
        self.logger.debug(f"Running in {cwd}")

        if not stream_output:
            try:
                result = subprocess.run(
                        cmd,
                        cwd=cwd,
                        capture_output=True,
                        text=True,
                        check=check,
                        input=input_data
                        )
                if result.stdout:
                    self.logger.debug(f"stdout: {result.stdout.strip()}")
                if result.stderr:
                    self.logger.debug(f"stderr: {result.stderr.strip()}")
                return result
            except subprocess.CalledProcessError as e:
                self.logger.error(f"Command failed: {cmd_str}")
                self.logger.error(f"Exit code: {e.returncode}")
                self.logger.error(f"stdout: {e.stdout}")
                self.logger.error(f"stderr: {e.stderr}")
                raise BuildError(f"Command failed: {cmd_str}") from e

        # stream_output == True: use Popen and stream lines to logger + file
        logfile_path = Path(self.config.log_file)
        with open(logfile_path, "a", buffering=1) as logfile:
            proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE if input_data is not None else None,
                    text=True,
                    bufsize=1,
                    universal_newlines=True
                    )

            # If input_data is provided, send it and close stdin
            if input_data is not None:
                try:
                    proc.stdin.write(input_data)
                    proc.stdin.close()
                except Exception:
                    pass

            # Stream output line by line
            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, ''):
                # strip trailing newline only for logging
                self.logger.info(line.rstrip())
                logfile.write(line)

            proc.wait()
            ret = proc.returncode

        if ret != 0 and check:
            raise BuildError(f"Command failed (exit {ret}): {cmd_str}")

        # Return a fake CompletedProcess-like object with stdout/stderr empty (we logged to file)
        cp = subprocess.CompletedProcess(cmd, ret, stdout=None, stderr=None)
        return cp

    def check_prerequisites(self, run_olddefconfig: bool = False):
        """Verify all required tools and files exist.

        Args:
            run_olddefconfig: If True, check for defconfig instead of final config file.
        """
        self.logger.info("Checking prerequisites...")

        compiler = f'{self.cross_compile}gcc' if self.cross_compile else 'gcc'
        required_tools = ['git', 'make', 'tar', compiler, 'patch']
        missing_tools = []

        for tool in required_tools:
            result = self.run_command(['which', tool], check=False)
            if result.returncode != 0:
                missing_tools.append(tool)

        if missing_tools:
            raise BuildError(f"Missing required tools: {', '.join(missing_tools)}")

        if run_olddefconfig:
            defconfig_file = self.config.build_dir / "configs" / "defconfig"
            if not defconfig_file.exists():
                raise BuildError(f"defconfig file not found: {defconfig_file}")
        else:
            if not self.config.config_file.exists():
                raise BuildError(f"Config file not found: {self.config.config_file}")

        if not self.config.patches_dir.exists():
            raise BuildError(f"Patches directory not found: {self.config.patches_dir}")

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Prerequisites check passed")

    def apply_patches(self) -> PatchStats:
        """Apply kernel patches from the patches directory."""
        self.logger.info("Applying MNT kernel patches...")

        # Get sorted list of patch files
        patch_files = sorted(self.config.patches_dir.rglob("*.patch"))

        if not patch_files:
            self.logger.warning(f"No patches found in {self.config.patches_dir}")
            return PatchStats()

        self.logger.info(f"Found {len(patch_files)} patches to apply")

        stats = PatchStats()
        failed_log_path = self.config.linux_dir / "failed.log"

        # Remove old failed.log if it exists
        if failed_log_path.exists():
            failed_log_path.unlink()

        failed_log_entries = []

        for patch_file in patch_files:
            patch_name = patch_file.name
            self.logger.debug(f"Processing patch: {patch_name}")

            # Read patch content
            with open(patch_file, 'r') as f:
                patch_content = f.read()

            # Try dry-run first
            dry_run_result = self.run_command(
                    ['patch', '-p1', '--dry-run'],
                    cwd=self.config.linux_dir,
                    input_data=patch_content,
                    check=False
                    )

            if dry_run_result.returncode == 0:
                # Apply the patch for real
                apply_result = self.run_command(
                        ['patch', '-p1'],
                        cwd=self.config.linux_dir,
                        input_data=patch_content,
                        check=False
                        )

                if apply_result.returncode == 0:
                    self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Applied: {patch_name}")
                    stats.add_success()
                else:
                    self.logger.warning(f"{Colors.RED}✗{Colors.RESET} Failed to apply: {patch_name}")
                    stats.add_failure(patch_name)
                    failed_log_entries.append(self._format_failed_patch(patch_name, apply_result))
            else:
                self.logger.warning(f"{Colors.RED}✗{Colors.RESET} Failed (dry-run): {patch_name}")
                stats.add_failure(patch_name)
                failed_log_entries.append(self._format_failed_patch(patch_name, dry_run_result))

        # Write failed patches log if there were failures
        if failed_log_entries:
            with open(failed_log_path, 'w') as f:
                f.write('\n'.join(failed_log_entries))

        # Summary
        self.logger.info("")
        self.logger.info("Patch application complete!")
        self.logger.info(f"Succeeded: {stats.success}")
        self.logger.info(f"Failed:    {stats.failed}")
        self.logger.info(f"Total:     {stats.total}")

        if stats.failed > 0:
            self.logger.warning(f"Failed patches logged to: {failed_log_path}")

        return stats

    def _format_failed_patch(self, patch_name: str, result: subprocess.CompletedProcess) -> str:
        """Format a failed patch entry for the log file."""
        return (
                f"{'=' * 60}\n"
                f"Failed patch: {patch_name}\n"
                f"{'-' * 60}\n"
                f"{result.stdout}\n"
                f"{result.stderr}\n"
                )

    def checkout_kernel_version(self):
        self.logger.info("Resetting repository state...")
        self.run_command(['git', 'reset', '--hard', 'HEAD'])
        self.run_command(['git', 'clean', '-fd'])
        self.run_command(['git', 'checkout', 'master'])
        self.run_command(['git', 'tag', '-d', f'v{self.config.version}'], check=False)

        self.logger.info("Fetching git tags...")
        self.run_command(['git', 'fetch', '--tags'])

        branch_name = f"pocket-reform-{self.config.version}"
        self.logger.info(f"Checking out kernel version v{self.config.version}...")

        self.run_command(['git', 'branch', '-D', branch_name], check=False)
        self.run_command(['git', 'checkout', '-b', branch_name, f'tags/v{self.config.version}'])

    def setup_custom_dts_files(self):
        """Copy custom DTS files and update vendor Makefiles.
        Copies DTS files to the kernel source tree and adds corresponding
        entries to vendor-specific Makefiles for DTB creation.
        """
        self.logger.info(f"Adding {len(DTS_CONFIGS)} custom DTS files...")

        for dts_config in DTS_CONFIGS:
            # Copy DTS file
            custom_dts = self.config.build_dir / f"reform-debian-packages/linux/{dts_config['name']}"
            dts_dest = self.config.linux_dir / f"arch/arm64/boot/dts/{dts_config['vendor']}/{dts_config['name']}"

            if not custom_dts.exists():
                raise BuildError(f"Custom DTS file not found: {custom_dts}")

            self.run_command(["cp", str(custom_dts), str(dts_dest)])
            self.logger.info(f"  Copied {dts_config['name']} to {dts_config['vendor']}/")

        # Update Makefiles (group by vendor to avoid processing same file multiple times)
        vendors_to_update = {}
        for dts_config in DTS_CONFIGS:
            vendor = dts_config['vendor']
            if vendor not in vendors_to_update:
                vendors_to_update[vendor] = []
            dtb_name = dts_config['name'].replace('.dts', '.dtb')
            vendors_to_update[vendor].append((dtb_name, dts_config['config']))

        for vendor, dtb_entries in vendors_to_update.items():
            self.logger.info(f"Modifying {vendor} dts Makefile...")
            makefile = self.config.linux_dir / f"arch/arm64/boot/dts/{vendor}/Makefile"
            makefile_content = makefile.read_text() if makefile.exists() else ""

            entries_to_add = []
            for dtb_name, config in dtb_entries:
                if dtb_name not in makefile_content:
                    entries_to_add.append(f"dtb-$({config}) += {dtb_name}\n")
                    self.logger.info(f"  Adding {dtb_name} to {vendor} Makefile")

            if entries_to_add:
                with open(makefile, "a") as f:
                    f.write("\n" + "".join(entries_to_add))

    def update_config_with_olddefconfig(self, skip_git_operations: bool = False):
        """Update kernel config using olddefconfig.

        Prepares the kernel to the same state as when it will be built,
        then run olddefconfig.

        """
        self.logger.info("Updating kernel config with olddefconfig...")
        self.logger.info("Preparing kernel to build state before running olddefconfig...")

        os.chdir(self.config.linux_dir)

        if not skip_git_operations:
            self.checkout_kernel_version()

        patch_stats = self.apply_patches()
        if patch_stats.failed > 0:
            self.logger.warning(
                    f"{patch_stats.failed} patches failed to apply. "
                    "Config update will continue, but may not be accurate."
                    )

        self.setup_custom_dts_files()

        defconfig_path = self.config.build_dir / "configs" / "defconfig"
        if not defconfig_path.exists():
            raise BuildError(f"defconfig not found: {defconfig_path}")
        self.logger.info(f"Copying {defconfig_path} to .config...")
        self.run_command(['cp', str(defconfig_path), str(self.config.linux_dir / '.config')])

        self.logger.info("Running olddefconfig to update config defaults...")
        self.run_command([
            'make',
            f'ARCH={self.arch}',
            f'CROSS_COMPILE={self.cross_compile}',
            'olddefconfig'
        ], cwd=self.config.linux_dir)

        self.logger.info(f"Saving updated config to {self.config.config_file}...")
        self.config.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.run_command(['cp', str(self.config.linux_dir / '.config'), str(self.config.config_file)])

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Config updated successfully")

    def build_kernel(self, skip_git_operations: bool = False, run_olddefconfig: bool = False):
        """Build the Linux kernel.

        Args:
            skip_git_operations: If True, skip git reset/checkout operations.
                                Assumes kernel is already at the correct version.
            run_olddefconfig: If True, update config using olddefconfig before building.
        """
        self.logger.info(f"Building kernel {self.config.version}...")
        start_time = datetime.now()

        os.chdir(self.config.linux_dir)

        # Update config with olddefconfig if requested (this prepares the kernel state)
        if run_olddefconfig:
            self.update_config_with_olddefconfig(skip_git_operations=skip_git_operations)
        else:
            # If not running olddefconfig, we still need to prepare the kernel state
            if not skip_git_operations:
                self.checkout_kernel_version()

            # Apply patches
            patch_stats = self.apply_patches()
            if patch_stats.failed > 0:
                self.logger.warning(
                        f"{patch_stats.failed} patches failed to apply. "
                        "Build will continue, but may fail or produce unexpected results."
                        )

            # Setup custom DTS
            self.setup_custom_dts_files()

        # Copy config
        self.logger.info("Copying kernel config...")
        self.run_command(['cp', str(self.config.config_file), '.config'])

        # Commit changes
        self.logger.info("Create git tag and commit.")
        self.run_command(['git', 'add', '--all'])
        self.run_command(['git', 'commit', '-s', '-m', f'MNT Pocket Arch {self.config.version}'])
        self.run_command(['git', 'tag', '-d', f'v{self.config.version}'], check=False)
        self.run_command(['git', 'tag', '-a', f'v{self.config.version}', '-m', f'MNT Pocket Arch {self.config.version}'])

        # Compile kernel
        self.logger.info(f"Compiling kernel with {self.config.jobs} jobs (this may take a while)...")
        self.run_command(
                [
                    'make',
                    f'-j{self.config.jobs}',
                    f'ARCH={self.arch}',
                    f'CROSS_COMPILE={self.cross_compile}',
                    'Image',
                    'dtbs',
                    'modules'
                    ],
                cwd=self.config.linux_dir,
                stream_output=True
                )

        # Install modules to temporary location
        modules_dir = self.config.linux_dir / "modules"
        self.logger.info(f"Installing modules to {modules_dir}...")
        if modules_dir.exists():
            shutil.rmtree(modules_dir)
        self.run_command(
                [
                    'make',
                    f'ARCH={self.arch}',
                    f'CROSS_COMPILE={self.cross_compile}',
                    f'INSTALL_MOD_PATH={modules_dir}',
                    'modules_install'
                    ],
                stream_output=True,
                cwd=self.config.linux_dir
                )

        elapsed = (datetime.now() - start_time).total_seconds()
        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Kernel built in {elapsed:.0f} seconds")

    def build_lpc_module(self):
        """Build the LPC kernel module."""
        self.logger.info("Building LPC module...")
        lpc_dir = self.config.build_dir / "reform-tools" / "lpc"

        if not lpc_dir.exists():
            raise BuildError(f"LPC module directory not found: {lpc_dir}")

        self.run_command([
            'make',
            f'ARCH={self.arch}',
            f'CROSS_COMPILE={self.cross_compile}',
            f'-C{self.config.linux_dir}',
            f'M={lpc_dir}',
            f'-j{self.config.jobs}'
            ],
        cwd=lpc_dir
        )

        if not (lpc_dir / "reform2_lpc.ko").exists():
            raise BuildError("LPC module build failed - reform2_lpc.ko not found")

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} LPC module built")

    def build_qcacld2_module(self):
        """Build the QCACLD2 WiFi module."""
        self.logger.info("Building QCACLD2 WiFi module...")
        qca_dir = self.config.build_dir / "qcacld2"

        if not qca_dir.exists():
            raise BuildError(f"QCACLD2 module directory not found: {qca_dir}")

        # Recreate qcacld2/build.sh behavior here so this build path
        # respects --cross-compile and native builds.
        make_args = [
            f"ARCH={self.arch}",
            f"KERNEL_SRC={self.config.linux_dir}",
            "CONFIG_CLD_HL_SDIO_CORE=y",
            "CONFIG_FORCE_MLO_SUPPORT=y",
        ]
        if self.cross_compile:
            make_args.append(f"CROSS_COMPILE={self.cross_compile}")

        self.run_command(
                ["make", *make_args, "clean"],
                cwd=qca_dir
                )
        self.run_command(
                ["make", *make_args, f"-j{self.config.jobs}"],
                cwd=qca_dir
                )

        if not (qca_dir / "wlan.ko").exists():
            raise BuildError("QCACLD2 module build failed - wlan.ko not found")

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} QCACLD2 module built")

    def install_extmod_build_tree(self, dest_dir: Optional[Path] = None) -> Path:
        """Install a kernel header tree suitable for out-of-tree module builds.

        This wraps the upstream kernel helper scripts/package/install-extmod-build
        and prepares the source tree first (prepare/modules_prepare).
        """
        self.logger.info("Installing external-module build tree...")

        if dest_dir is None:
            dest_dir = self.config.build_dir / "linux-headers-extmod"

        install_script = self.config.linux_dir / "scripts" / "package" / "install-extmod-build"
        if not install_script.exists():
            raise BuildError(f"install-extmod-build script not found: {install_script}")

        # Ensure generated headers/metadata are up to date before install.
        prep_args = ['make', f'-j{self.config.jobs}', f'ARCH={self.arch}']
        if self.cross_compile:
            prep_args.append(f'CROSS_COMPILE={self.cross_compile}')

        self.run_command([*prep_args, 'prepare'], 
                         stream_output=True,
                         cwd=self.config.linux_dir)
        self.run_command([*prep_args, 'modules_prepare'],
                         stream_output=True,
                         cwd=self.config.linux_dir)

        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        dest_dir.parent.mkdir(parents=True, exist_ok=True)

        # Match kernel CC conventions: target compiler comes from CROSS_COMPILE,
        # host tools are built by host compiler unless explicitly overridden.
        cc = f'{self.cross_compile}gcc' if self.cross_compile else 'gcc'
        hostcc = os.environ.get('HOSTCC', 'gcc')

        self.run_command(
                [
                    'env',
                    f'ARCH={self.arch}',
                    f'SRCARCH={self.arch}',
                    f'srctree={self.config.linux_dir}',
                    'MAKE=make',
                    f'CC={cc}',
                    f'HOSTCC={hostcc}',
                    str(install_script),
                    str(dest_dir),
                ],
                stream_output=True,
                cwd=self.config.linux_dir
                )

        required_paths = [
            dest_dir / 'Makefile',
            dest_dir / 'include',
            dest_dir / 'scripts',
            dest_dir / 'Module.symvers',
        ]
        missing = [p for p in required_paths if not p.exists()]
        if missing:
            missing_str = ', '.join(str(p) for p in missing)
            raise BuildError(f"install-extmod-build output incomplete, missing: {missing_str}")

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Installed extmod build tree: {dest_dir}")
        return dest_dir

    def create_headers_tarball(self, headers_dir: Optional[Path] = None):
        """Create headers tarball from the extmod build tree."""
        self.logger.info("Creating headers tarball...")

        if headers_dir is None:
            headers_dir = self.config.build_dir / "linux-headers-extmod"

        if not headers_dir.exists():
            raise BuildError(f"Headers directory not found: {headers_dir}")

        if self.config.output_headers_tar.exists():
            self.config.output_headers_tar.unlink()

        with tarfile.open(self.config.output_headers_tar, 'w:gz') as tar:
            tar.add(headers_dir, arcname=f"linux-{self.config.version}")

        dest_path = self.config.build_dir / self.config.output_headers_tar.name
        if dest_path.exists():
            dest_path.unlink()
        self.config.output_headers_tar.rename(dest_path)

        size_mb = dest_path.stat().st_size / (1024 * 1024)
        self.logger.info(
                f"{Colors.GREEN}✓{Colors.RESET} Headers tarball created: "
                f"{dest_path.name} ({size_mb:.1f} MB)"
                )

    def create_tarball(self):
        """Create deployment tarball."""
        self.logger.info("Creating deployment tarball...")

        def exclude_build(tarinfo):
            if tarinfo.issym() and tarinfo.name.endswith("/build"):
                return None
            return tarinfo

        os.chdir(self.config.linux_dir)

        # Verify all required files exist
        required_files = {
            'kernel': self.config.linux_dir / "arch/arm64/boot/Image",
            'config': self.config.config_file,
            'lpc_module': self.config.build_dir / "reform-tools/lpc/reform2_lpc.ko",
            'wifi_module': self.config.build_dir / "qcacld2/wlan.ko",
            'modules': self.config.linux_dir / "modules/lib/modules"
        }

        # Add all DTB files to required files check
        for i, dtb_path in enumerate(self.config.dtb_files):
            required_files[f'dtb_{i}'] = dtb_path

        for name, path in required_files.items():
            if not path.exists():
                raise BuildError(f"Required file missing ({name}): {path}")

        # Remove old tarball if exists
        if self.config.output_tar.exists():
            self.config.output_tar.unlink()

        # Create tarball
        with tarfile.open(self.config.output_tar, 'w:gz') as tar:
            # Add kernel image
            tar.add(
                    self.config.linux_dir / "arch/arm64/boot/Image",
                    arcname="arch/arm64/boot/Image"
                    )

            # Add all DTB files
            for dtb_path in self.config.dtb_files:
                dtb_filename = dtb_path.name.replace('.dtb', f'-{self.config.version}.dtb')
                tar.add(
                        dtb_path,
                        arcname=dtb_filename
                        )
                self.logger.info(f"  Added DTB: {dtb_filename}")

            # Add LPC module
            tar.add(
                    self.config.build_dir / "reform-tools/lpc/reform2_lpc.ko",
                    arcname="reform2_lpc.ko"
                    )

            # Add WiFi module
            tar.add(
                    self.config.build_dir / "qcacld2/wlan.ko",
                    arcname="wlan.ko"
                    )

            # Add WiFi firmware
            tar.add(
                    self.config.build_dir / "qcacld2/debian-meta/usr",
                    arcname="usr"
                    )

            # Add atheros blacklist
            tar.add(
                    self.config.build_dir / "qcacld2/debian-meta/etc/modprobe.d/reform-qcacld2.conf",
                    arcname="etc/modprobe.d/reform-qcacld2.conf"
                    )

            # Add modules directory
            tar.add(
                    self.config.linux_dir / "modules/lib/modules",
                    arcname="lib/modules",
                    filter=exclude_build
                    )

            # Add the config file
            tar.add(
                    self.config.config_file,
                    arcname=f"config-{self.config.version}-mnt-reform-arm64"
                    )

        dest_path = self.config.build_dir / self.config.output_tar.name
        if dest_path.exists():
            dest_path.unlink()
        self.config.output_tar.rename(dest_path)

        # Report size
        size_mb = dest_path.stat().st_size / (1024 * 1024)
        self.logger.info(
                f"{Colors.GREEN}✓{Colors.RESET} Tarball created: "
                f"{self.config.output_tar.name} ({size_mb:.1f} MB)"
                )


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

    # Create configuration
    config = BuildConfig.create(
            version=version,
            build_dir=build_dir,
            jobs=jobs,
            pkgrel=pkgrel
            )

    # Setup logging
    config.build_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(config.log_file)

    # Create builder
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

        # Check prerequisites
        builder.check_prerequisites(run_olddefconfig=run_olddefconfig)

        if dry_run:
            logger.info("Dry run mode - exiting after prerequisites check")
            return 0

        # Build everything
        builder.build_kernel(skip_git_operations=skip_git_operations, run_olddefconfig=run_olddefconfig)
        builder.build_lpc_module()
        builder.build_qcacld2_module()
        if with_headers:
            headers_dir = builder.install_extmod_build_tree()
            builder.create_headers_tarball(headers_dir)
        builder.create_tarball()

        # Summary
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info("=" * 60)
        logger.info(f"{Colors.GREEN}✓ Build completed successfully in {elapsed:.0f} seconds!{Colors.RESET}")
        logger.info(f"Output: {config.output_tar}")
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
