#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
MNT Pocket Reform Kernel Auto-Build Script
Compiles kernel, out-of-tree modules, and creates deployment tarball.
Assumes an automated build, with reduced args / options from build.py.
Assumes we checked out the correct SHA of the kernel for building.
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

__version__ = "0.1.0"

DEFAULT_KERNEL_VERSION = '6.17.9'


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
    dtb_file: Path
    output_tar: Path
    log_file: Path
    jobs: int

    @classmethod
    def create(cls, version: str, build_dir: Optional[Path] = None, jobs: Optional[int] = None):
        """Create build configuration with sensible defaults."""
        if build_dir is None:
            build_dir = Path.home() / "mnt-build"

        if jobs is None:
            jobs = os.cpu_count() or 4

        linux_dir = build_dir / "linux"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        # Extract major.minor version (e.g., "6.17" from "6.17.8")
        version_parts = version.split('.')
        major_minor = f"{version_parts[0]}.{version_parts[1]}"

        return cls(
                version=version,
                build_dir=build_dir,
                linux_dir=linux_dir,
                patches_dir=build_dir / "reform-debian-packages" / "linux" / f"patches{major_minor}",
                config_file=build_dir / "configs" / f"config-{version}-mnt-reform-arm64",
                dtb_file=linux_dir / "arch/arm64/boot/dts/freescale/imx8mp-mnt-pocket-reform.dtb",
                output_tar=linux_dir / f"kernel-{version}-mnt.tar.gz",
                log_file=build_dir / f"build-{version}-{timestamp}.log",
                jobs=jobs
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

    def __init__(self, config: BuildConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.arch = "arm64"
        self.cross_compile = "aarch64-linux-gnu-"

    def run_command(self, cmd: List[str], cwd: Optional[Path] = None,
                    check: bool = True, input_data: Optional[str] = None,
                    stream_output: bool = True) -> subprocess.CompletedProcess:
        """Run a shell command. If stream_output is True, stream stdout/stderr live to logger and file."""
        cwd = cwd or Path.cwd()
        self.logger.debug(f"Running: {' '.join(cmd)} (in {cwd})")

        if not stream_output:
            # existing behavior (short commands)
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
                self.logger.error(f"Command failed: {' '.join(cmd)}")
                self.logger.error(f"Exit code: {e.returncode}")
                self.logger.error(f"stdout: {e.stdout}")
                self.logger.error(f"stderr: {e.stderr}")
                raise BuildError(f"Command failed: {' '.join(cmd)}") from e

        # stream_output == True: use Popen and stream lines to logger + file
        logfile_path = Path(self.config.log_file)
        with open(logfile_path, "a", buffering=1) as logfile:  # line-buffered
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
            raise BuildError(f"Command failed (exit {ret}): {' '.join(cmd)}")

        # Return a fake CompletedProcess-like object with stdout/stderr empty (we logged to file)
        cp = subprocess.CompletedProcess(cmd, ret, stdout=None, stderr=None)
        return cp


    def check_prerequisites(self):
        """Verify all required tools and files exist."""
        self.logger.info("Checking prerequisites...")

        required_tools = ['git', 'make', 'tar', 'aarch64-linux-gnu-gcc', 'patch']
        missing_tools = []

        for tool in required_tools:
            result = self.run_command(['which', tool], check=False)
            if result.returncode != 0:
                missing_tools.append(tool)

        if missing_tools:
            raise BuildError(f"Missing required tools: {', '.join(missing_tools)}")

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

    def build_kernel(self):
        """Build the Linux kernel."""
        self.logger.info(f"Building kernel {self.config.version}...")
        start_time = datetime.now()

        os.chdir(self.config.linux_dir)


        # Apply patches
        patch_stats = self.apply_patches()
        if patch_stats.failed > 0:
            self.logger.warning(
                    f"{patch_stats.failed} patches failed to apply. "
                    "Build will continue, but kernel may not work correctly."
                    )

        # Copy DTS
        self.logger.info("Adding custom DTS file...")
        custom_dts = self.config.build_dir / "reform-debian-packages/linux/imx8mp-mnt-pocket-reform.dts"
        dts_dest = self.config.linux_dir / "arch/arm64/boot/dts/freescale/imx8mp-mnt-pocket-reform.dts"
        self.run_command(["cp", str(custom_dts), str(dts_dest)])

        # Update the Freescale Makefile for the DTB creation
        self.logger.info("Modifying freescale dts makefile...")
        makefile = self.config.linux_dir / "arch/arm64/boot/dts/freescale/Makefile"
        with open(makefile, "a") as f:
            f.write("\ndtb-$(CONFIG_ARCH_MXC) += imx8mp-mnt-pocket-reform.dtb\n")

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
                    f'ARCH={self.arch}',
                    f'CROSS_COMPILE={self.cross_compile}',
                    f'-j{self.config.jobs}',
                    'Image', 'modules', 'dtbs'
                    ],
                cwd=self.config.linux_dir,
                stream_output=True)

        elapsed = (datetime.now() - start_time).total_seconds()
        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Kernel compiled in {elapsed:.0f} seconds")

        # Install modules
        self.logger.info("Installing modules...")
        modules_path = self.config.linux_dir / "modules"
        self.run_command([
            'make',
            f'ARCH={self.arch}',
            f'CROSS_COMPILE={self.cross_compile}',
            'modules_install',
            f'INSTALL_MOD_PATH={modules_path}',
            f'-j{self.config.jobs}'
            ])

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Kernel build complete")

    def build_lpc_module(self):
        """Build the LPC module."""
        self.logger.info("Building LPC module...")
        lpc_dir = self.config.build_dir / "reform-tools" / "lpc"

        os.chdir(lpc_dir)

        # Build module
        self.logger.info("Compiling LPC module...")
        self.run_command([
            'make',
            f'ARCH={self.arch}',
            f'CROSS_COMPILE={self.cross_compile}',
            f'-C{self.config.linux_dir}',
            f'M={lpc_dir}',
            f'-j{self.config.jobs}'
            ])

        # Verify output
        if not (lpc_dir / "reform2_lpc.ko").exists():
            raise BuildError("LPC module build failed - reform2_lpc.ko not found")

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} LPC module built")

    def build_qcacld2_module(self):
        """Build the QCACLD2 WiFi module."""
        self.logger.info("Building QCACLD2 WiFi module...")
        qcacld2_dir = self.config.build_dir / "qcacld2"

        os.chdir(qcacld2_dir)

        # Build module
        self.logger.info("Compiling QCACLD2 module...")
        self.run_command(
            ["bash", "./build.sh"],
            cwd=Path(qcacld2_dir),
            stream_output=False
        )

        # Verify output
        if not (qcacld2_dir / "wlan.ko").exists():
            raise BuildError("QCACLD2 module build failed - wlan.ko not found")

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} QCACLD2 module built")

    def create_tarball(self):
        """Create deployment tarball."""
        self.logger.info("Creating deployment tarball...")

        os.chdir(self.config.linux_dir)

        # Verify all required files exist
        required_files = {
                'kernel': self.config.linux_dir / "arch/arm64/boot/Image",
                'dtb': self.config.dtb_file,
                'config': self.config.config_file,
                'lpc_module': self.config.build_dir / "reform-tools/lpc/reform2_lpc.ko",
                'wifi_module': self.config.build_dir / "qcacld2/wlan.ko",
                'modules': self.config.linux_dir / "modules/lib/modules"
                }

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

            # Add DTB
            tar.add(
                    self.config.dtb_file,
                    arcname=f"imx8mp-mnt-pocket-reform-{self.config.version}.dtb"
                    )

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
                    arcname="lib/modules"
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


def main():
    config = BuildConfig.create(
            version=DEFAULT_KERNEL_VERSION,
            build_dir=None,
            jobs=None
            )

    config.build_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(config.log_file)

    builder = KernelBuilder(config, logger)

    try:
        logger.info("=" * 60)
        logger.info("Starting kernel build process")
        logger.info(f"Version: {config.version}")
        logger.info(f"Build directory: {config.build_dir}")
        logger.info(f"Patches directory: {config.patches_dir}")
        logger.info(f"Log file: {config.log_file}")
        logger.info(f"Parallel jobs: {config.jobs}")
        logger.info("=" * 60)

        start_time = datetime.now()

        builder.check_prerequisites()

        builder.build_kernel()
        builder.build_lpc_module()
        builder.build_qcacld2_module()
        builder.create_tarball()

        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info("=" * 60)
        logger.info(f"{Colors.GREEN}✓ Build completed successfully in {elapsed:.0f} seconds!{Colors.RESET}")
        logger.info(f"Output: {config.output_tar}")
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


if __name__ == '__main__':
    sys.exit(main())
