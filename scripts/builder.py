# SPDX-License-Identifier: MIT
import logging
import os
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from config import BuildConfig, DEFAULT_CROSS_COMPILE, DEFAULT_KERNEL_ONLY, DTS_CONFIGS
from errors import BuildError, PatchStats
from logging_setup import Colors


class KernelBuilder:
    def __init__(self, config: BuildConfig, logger: logging.Logger,
                 cross_compile: str = DEFAULT_CROSS_COMPILE,
                 kernel_only: bool = DEFAULT_KERNEL_ONLY):
        self.config = config
        self.logger = logger
        self.arch = "arm64"
        self.cross_compile = cross_compile.strip() if cross_compile else ""
        self.kernel_only = kernel_only
        self.patch_dirs_used: List[Path] = []

    def _make_kernel_vars(self) -> List[str]:
        """Return kernel make variable assignments with optional toolchain prefix."""
        args = [f"ARCH={self.arch}"]
        if self.cross_compile:
            args.append(f"CROSS_COMPILE={self.cross_compile}")
        return args

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def run_command(self, cmd: List[str], cwd: Optional[Path] = None,
                    check: bool = True, input_data: Optional[str] = None,
                    stream_output: bool = False,
                    log_cmd: bool = True) -> subprocess.CompletedProcess:
        """Run a shell command.

        If stream_output is True, stdout/stderr are streamed live to the logger
        and the log file rather than being captured.
        If log_cmd is False, the command string is logged at DEBUG level instead
        of INFO (useful for high-frequency calls like patch dry-runs).
        """
        cwd = cwd or Path.cwd()
        cmd_str = ' '.join(cmd)
        (self.logger.info if log_cmd else self.logger.debug)(f"$ {cmd_str}")
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

            if input_data is not None:
                try:
                    proc.stdin.write(input_data)
                    proc.stdin.close()
                except Exception:
                    pass

            assert proc.stdout is not None
            for line in iter(proc.stdout.readline, ''):
                self.logger.info(line.rstrip())
                logfile.write(line)

            proc.wait()
            ret = proc.returncode

        if ret != 0 and check:
            raise BuildError(f"Command failed (exit {ret}): {cmd_str}")

        return subprocess.CompletedProcess(cmd, ret, stdout=None, stderr=None)

    # ------------------------------------------------------------------
    # Prerequisites
    # ------------------------------------------------------------------

    def check_prerequisites(self, run_olddefconfig: bool = False):
        """Verify all required tools and files exist."""
        self.logger.info("Checking prerequisites...")

        # Verify the kernel tree looks valid before invoking make
        if not (self.config.linux_dir / "Makefile").exists():
            raise BuildError(f"Kernel source not found at: {self.config.linux_dir}")

        if run_olddefconfig:
            defconfig_file = self.config.build_dir / "configs" / "defconfig"
            if not defconfig_file.exists():
                raise BuildError(f"defconfig file not found: {defconfig_file}")
        else:
            if not self.config.config_file.exists():
                raise BuildError(f"Config file not found: {self.config.config_file}")

        if not self.config.patches_dir.exists():
            raise BuildError(f"Patches directory not found: {self.config.patches_dir}")

        self.logger.info("Verifying build toolchain via 'make kernelversion'...")
        make_cmd = ['make', *self._make_kernel_vars(), 'kernelversion']

        result = self.run_command(make_cmd, cwd=self.config.linux_dir, check=False)
        if result.returncode != 0:
            raise BuildError(
                "Toolchain check failed ('make kernelversion' did not succeed). "
                "Ensure build tools are installed and CROSS_COMPILE is set correctly."
            )

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Toolchain check passed "
                         f"(kernel version: {result.stdout.strip()})")

    # ------------------------------------------------------------------
    # Patching
    # ------------------------------------------------------------------

    def apply_patches(self) -> PatchStats:
        """Apply kernel patches from patches_dir, then xtra_patches_dir if present."""
        stats = PatchStats()

        self.logger.info("Applying MNT kernel patches...")
        failed_log_path = self.config.linux_dir / "failed.log"
        self._apply_patch_set(
            self.config.patches_dir,
            failed_log_path,
            label="",
            on_success=stats.add_success,
            on_failure=stats.add_failure,
        )

        xtra_dir = getattr(self.config, 'xtra_patches_dir', None)
        if xtra_dir is not None and xtra_dir.exists():
            self.logger.info("")
            self.logger.info("Applying extra kernel patches...")
            xtra_failed_log_path = self.config.linux_dir / "failed_xtra.log"
            self._apply_patch_set(
                xtra_dir,
                xtra_failed_log_path,
                label="extra",
                on_success=stats.add_xtra_success,
                on_failure=stats.add_xtra_failure,
            )

        self.logger.info("")
        self.logger.info("Patch application complete!")
        if stats.has_xtra:
            self.logger.info(f"Succeeded: {stats.success}, Succeeded Extra: {stats.xtra_success}")
            self.logger.info(f"Failed:    {stats.failed}, Failed Extra: {stats.xtra_failed}")
            self.logger.info(f"Total:     {stats.total}, Extra Total: {stats.xtra_total}")
        else:
            self.logger.info(f"Succeeded: {stats.success}")
            self.logger.info(f"Failed:    {stats.failed}")
            self.logger.info(f"Total:     {stats.total}")

        return stats

    def _apply_patch_set(
        self,
        patches_dir: Path,
        failed_log_path: Path,
        label: str,
        on_success,
        on_failure,
    ) -> None:
        """Apply all *.patch files from patches_dir, recording results via callbacks.

        Args:
            patches_dir:      Directory to search recursively for .patch files.
            failed_log_path:  File to write failure details to (cleared before use).
            label:            Human-readable qualifier for log messages (e.g. "extra").
                              Pass an empty string for the primary patch set.
            on_success:       Callable invoked (no args) for each successful patch.
            on_failure:       Callable invoked (patch_name) for each failed patch.
        """
        qualifier = f" ({label})" if label else ""
        patch_files = sorted(patches_dir.rglob("*.patch"))

        if not patch_files:
            self.logger.warning(f"No patches found in {patches_dir}")
            return

        if patches_dir not in self.patch_dirs_used:
            self.patch_dirs_used.append(patches_dir)

        self.logger.info(f"Found {len(patch_files)}{qualifier} patches to apply")

        if failed_log_path.exists():
            failed_log_path.unlink()

        failed_log_entries = []

        for patch_file in patch_files:
            patch_name = patch_file.name
            self.logger.debug(f"Processing{qualifier} patch: {patch_name}")

            with open(patch_file, 'r') as f:
                patch_content = f.read()

            dry_run_result = self.run_command(
                ['patch', '-p1', '--dry-run'],
                cwd=self.config.linux_dir,
                input_data=patch_content,
                check=False,
                log_cmd=False
            )

            if dry_run_result.returncode == 0:
                apply_result = self.run_command(
                    ['patch', '-p1'],
                    cwd=self.config.linux_dir,
                    input_data=patch_content,
                    check=False,
                    log_cmd=False
                )
                if apply_result.returncode == 0:
                    self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Applied{qualifier}: {patch_name}")
                    on_success()
                else:
                    self.logger.warning(f"{Colors.RED}✗{Colors.RESET} Failed to apply{qualifier}: {patch_name}")
                    on_failure(patch_name)
                    failed_log_entries.append(self._format_failed_patch(patch_name, apply_result))
            else:
                self.logger.warning(f"{Colors.RED}✗{Colors.RESET} Failed{qualifier} (dry-run): {patch_name}")
                on_failure(patch_name)
                failed_log_entries.append(self._format_failed_patch(patch_name, dry_run_result))

        if failed_log_entries:
            with open(failed_log_path, 'w') as f:
                f.write('\n'.join(failed_log_entries))
            self.logger.warning(f"Failed{qualifier} patches logged to: {failed_log_path}")

    def _format_failed_patch(self, patch_name: str, result: subprocess.CompletedProcess) -> str:
        """Format a failed patch entry for the log file."""
        return (
            f"{'=' * 60}\n"
            f"Failed patch: {patch_name}\n"
            f"{'-' * 60}\n"
            f"{result.stdout}\n"
            f"{result.stderr}\n"
        )

    # ------------------------------------------------------------------
    # Git / source tree setup
    # ------------------------------------------------------------------

    def checkout_kernel_version(self):
        """Reset the git repo and check out the target kernel version."""
        self.logger.info("Resetting repository state...")
        self.run_command(['git', 'reset', '--hard', 'HEAD'])
        self.run_command(['git', 'clean', '-fd'])
        self.run_command(['git', 'checkout', 'master'])
        self.run_command(['git', 'tag', '-d', f'v{self.config.version}'], check=False)

        self.logger.info("Fetching git tags...")
        self.run_command(['git', 'fetch', '--tags'])

        branch_name = f"mnt-reform-{self.config.version}"
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
            custom_dts = self.config.build_dir / f"reform-debian-packages/linux/{dts_config['name']}"
            dts_dest = self.config.linux_dir / f"arch/arm64/boot/dts/{dts_config['vendor']}/{dts_config['name']}"

            if not custom_dts.exists():
                raise BuildError(f"Custom DTS file not found: {custom_dts}")

            self.run_command(["cp", str(custom_dts), str(dts_dest)])
            self.logger.info(f"  Copied {dts_config['name']} to {dts_config['vendor']}/")

        # Group by vendor to avoid processing the same Makefile multiple times
        vendors_to_update: dict = {}
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

    # ------------------------------------------------------------------
    # Config management
    # ------------------------------------------------------------------

    def update_config_with_olddefconfig(self, skip_git_operations: bool = False):
        """Update kernel config using olddefconfig.

        Prepares the kernel to the same state as a normal build, then runs
        olddefconfig and saves the result back to the configs directory.
        """
        self.logger.info("Updating kernel config with olddefconfig...")
        self.logger.info("Preparing kernel to build state before running olddefconfig...")

        os.chdir(self.config.linux_dir)

        defconfig_path = self.config.build_dir / "configs" / "defconfig"
        if not defconfig_path.exists():
            raise BuildError(f"defconfig not found: {defconfig_path}")
        self.logger.info(f"Copying {defconfig_path} to .config...")
        self.run_command(['cp', str(defconfig_path), str(self.config.linux_dir / '.config')])

        self.logger.info("Running olddefconfig to update config defaults...")
        self.run_command([
            'make',
            *self._make_kernel_vars(),
            'olddefconfig'
        ], cwd=self.config.linux_dir)

        self.logger.info(f"Saving updated config to {self.config.config_file}...")
        self.config.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.run_command(['cp', str(self.config.linux_dir / '.config'), str(self.config.config_file)])

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Config updated successfully")

    # ------------------------------------------------------------------
    # Kernel build
    # ------------------------------------------------------------------

    def build_kernel(self, skip_git_operations: bool = False, run_olddefconfig: bool = False):
        """Build the Linux kernel.

        Args:
            skip_git_operations: If True, skip git reset/checkout operations.
                                  Assumes the kernel is already at the correct version.
            run_olddefconfig: If True, update config using olddefconfig before building.
        """
        self.logger.info(f"Building kernel {self.config.version}...")
        start_time = datetime.now()

        os.chdir(self.config.linux_dir)
        if not skip_git_operations:
            self.checkout_kernel_version()

        patch_stats = self.apply_patches()
        if patch_stats.failed > 0:
            self.logger.warning(
                f"{patch_stats.failed} patches failed to apply. "
                "Build will continue, but may fail or produce unexpected results."
            )

        self.setup_custom_dts_files()

        if run_olddefconfig:
            self.update_config_with_olddefconfig(skip_git_operations=skip_git_operations)
        else:
            self.logger.info("Copying kernel config...")
            self.run_command(['cp', str(self.config.config_file), '.config'])

        # Commit changes and tag, this is to avoid -dirty in our kernel name
        # Ideally we'd build the kernel outside a git repo, but for now, this
        self.logger.info("Create git tag and commit.")
        self.run_command(['git', 'add', '--all'])
        self.run_command(['git', 'commit', '-s', '-m', f'MNT Reform Linux v{self.config.version}'])
        self.run_command(['git', 'tag', '-d', f'v{self.config.version}'], check=False)
        self.run_command(['git', 'tag', '-a', f'v{self.config.version}', '-m', f'MNT Reform Linux v{self.config.version}'])

        # Compile
        if self.kernel_only:
            self.logger.info(
                f"Compiling kernel image only with {self.config.jobs} jobs "
                "(kernel_only=True, skipping dtbs and modules)..."
            )
            make_targets = ['Image']
        else:
            self.logger.info(f"Compiling kernel with {self.config.jobs} jobs (this may take a while)...")
            make_targets = ['Image', 'dtbs', 'modules']

        self.run_command(
            [
                'make',
                f'-j{self.config.jobs}',
                *self._make_kernel_vars(),
                *make_targets,
            ],
            cwd=self.config.linux_dir,
            stream_output=True
        )

        # Install modules to a temporary location if not "kernel only"
        if not self.kernel_only:
            modules_dir = self.config.linux_dir / "modules"
            self.logger.info(f"Installing modules to {modules_dir}...")
            if modules_dir.exists():
                shutil.rmtree(modules_dir)
            self.run_command(
                [
                    'make',
                    *self._make_kernel_vars(),
                    f'INSTALL_MOD_PATH={modules_dir}',
                    'modules_install'
                ],
                stream_output=True,
                cwd=self.config.linux_dir
            )

        elapsed = (datetime.now() - start_time).total_seconds()
        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Kernel built in {elapsed:.0f} seconds")

    # ------------------------------------------------------------------
    # Out-of-tree module builds
    # ------------------------------------------------------------------

    def build_lpc_module(self):
        """Build the LPC kernel module."""
        self.logger.info("Building LPC module...")
        lpc_dir = self.config.build_dir / "reform-tools" / "lpc"

        if not lpc_dir.exists():
            raise BuildError(f"LPC module directory not found: {lpc_dir}")

        self.run_command([
            'make',
            *self._make_kernel_vars(),
            f'-C{self.config.linux_dir}',
            f'M={lpc_dir}',
            f'-j{self.config.jobs}'
        ], cwd=lpc_dir)

        if not (lpc_dir / "reform2_lpc.ko").exists():
            raise BuildError("LPC module build failed - reform2_lpc.ko not found")

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} LPC module built")

    def build_qcacld2_module(self):
        """Build the QCACLD2 WiFi module."""
        self.logger.info("Building QCACLD2 WiFi module...")
        qca_dir = self.config.build_dir / "qcacld2"

        if not qca_dir.exists():
            raise BuildError(f"QCACLD2 module directory not found: {qca_dir}")

        make_args = [
            *self._make_kernel_vars(),
            f"KERNEL_SRC={self.config.linux_dir}",
            "CONFIG_CLD_HL_SDIO_CORE=y",
            "CONFIG_FORCE_MLO_SUPPORT=y",
        ]

        self.run_command(["make", *make_args, "clean"], cwd=qca_dir)
        self.run_command(["make", *make_args, f"-j{self.config.jobs}"], cwd=qca_dir)

        if not (qca_dir / "wlan.ko").exists():
            raise BuildError("QCACLD2 module build failed - wlan.ko not found")

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} QCACLD2 module built")

    # ------------------------------------------------------------------
    # Headers tree
    # ------------------------------------------------------------------

    def install_extmod_build_tree(self, dest_dir: Optional[Path] = None) -> Path:
        """Install a kernel header tree suitable for out-of-tree module builds.

        Wraps scripts/package/install-extmod-build and runs prepare/modules_prepare
        first to ensure generated headers are up to date.
        """
        self.logger.info("Installing external-module build tree...")

        if dest_dir is None:
            dest_dir = self.config.build_dir / "headers-extmod"

        install_script = self.config.linux_dir / "scripts" / "package" / "install-extmod-build"
        if not install_script.exists():
            raise BuildError(f"install-extmod-build script not found: {install_script}")

        config_path = self.config.linux_dir / ".config"
        if not config_path.exists():
            raise BuildError(f"Kernel config not found: {config_path}")

        original_config = config_path.read_bytes()

        cc = f'{self.cross_compile}gcc' if self.cross_compile else 'gcc'
        hostcc = os.environ.get('HOSTCC', 'gcc')
        try:
            self.run_command(
                ['make', f'-j{self.config.jobs}', *self._make_kernel_vars(), 'prepare'],
                stream_output=True,
                cwd=self.config.linux_dir
            )
            self.run_command(
                ['make', f'-j{self.config.jobs}', *self._make_kernel_vars(), 'modules_prepare'],
                stream_output=True,
                cwd=self.config.linux_dir
            )

            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            dest_dir.parent.mkdir(parents=True, exist_ok=True)

            env_cmd = [
                'env',
                f'ARCH={self.arch}',
                f'SRCARCH={self.arch}',
                f'srctree={self.config.linux_dir}',
                'MAKE=make',
                f'CC={cc}',
                f'HOSTCC={hostcc}',
            ]
            if self.cross_compile:
                env_cmd.append(f'CROSS_COMPILE={self.cross_compile}')

            self.run_command(
                [
                    *env_cmd,
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
        finally:
            current_config = config_path.read_bytes()
            if current_config != original_config:
                config_path.write_bytes(original_config)
                self.logger.warning("Header preparation modified .config; restored original build config.")

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Installed extmod build tree: {dest_dir}")
        return dest_dir

    # ------------------------------------------------------------------
    # Tarball creation
    # ------------------------------------------------------------------

    def _finalize_tarball(self, output_path: Path, label: str) -> Path:
        dest_path = self.config.build_dir / output_path.name
        if dest_path.exists():
            dest_path.unlink()
        output_path.rename(dest_path)
        size_mb = dest_path.stat().st_size / (1024 * 1024)
        self.logger.info(
            f"{Colors.GREEN}✓{Colors.RESET} {label} tarball created: "
            f"{dest_path.name} ({size_mb:.1f} MB)"
        )
        return dest_path

    def create_headers_tarball(self, headers_dir: Optional[Path] = None):
        """Create a headers tarball from the extmod build tree."""
        self.logger.info("Creating headers tarball...")

        if headers_dir is None:
            headers_dir = self.config.build_dir / "headers-extmod"

        if not headers_dir.exists():
            raise BuildError(f"Headers directory not found: {headers_dir}")

        if self.config.output_headers_tar.exists():
            self.config.output_headers_tar.unlink()

        with tarfile.open(self.config.output_headers_tar, 'w:gz') as tar:
            tar.add(headers_dir, arcname=f"linux-{self.config.version}")

        self._finalize_tarball(self.config.output_headers_tar, "Headers")

    def create_module_tarballs(self):
        """Create separate tarballs for out-of-tree modules."""
        self.logger.info("Creating module tarballs...")

        module_specs = [
            (
                "LPC module",
                self.config.output_lpc_module_tar,
                [
                    (
                        self.config.build_dir / "reform-tools/lpc/reform2_lpc.ko",
                        "reform2_lpc.ko",
                    ),
                ],
            ),
            (
                "WiFi module",
                self.config.output_wifi_module_tar,
                [
                    (
                        self.config.build_dir / "qcacld2/wlan.ko",
                        "wlan.ko",
                    ),
                    (
                        self.config.build_dir / "qcacld2/debian-meta/usr",
                        "usr",
                    ),
                    (
                        self.config.build_dir / "qcacld2/debian-meta/etc/modprobe.d/reform-qcacld2.conf",
                        "etc/modprobe.d/reform-qcacld2.conf",
                    ),
                ],
            ),
        ]

        for module_name, output_path, tar_members in module_specs:
            for source_path, _ in tar_members:
                if not source_path.exists():
                    raise BuildError(f"Required file missing ({module_name}): {source_path}")

            if output_path.exists():
                output_path.unlink()

            with tarfile.open(output_path, 'w:gz') as tar:
                for source_path, arcname in tar_members:
                    tar.add(source_path, arcname=arcname)

            self._finalize_tarball(output_path, module_name)

    def create_tarball(self):
        """Create the main deployment tarball (kernel image + DTBs + modules + config)."""
        self.logger.info("Creating deployment tarball...")

        def exclude_build(tarinfo):
            if tarinfo.issym() and tarinfo.name.endswith("/build"):
                return None
            return tarinfo

        os.chdir(self.config.linux_dir)

        required_files = {
            'kernel': self.config.linux_dir / "arch/arm64/boot/Image",
            'config': self.config.config_file,
            'modules': self.config.linux_dir / "modules/lib/modules"
        }
        for i, dtb_path in enumerate(self.config.dtb_files):
            required_files[f'dtb_{i}'] = dtb_path

        for name, path in required_files.items():
            if not path.exists():
                raise BuildError(f"Required file missing ({name}): {path}")

        if self.config.output_tar.exists():
            self.config.output_tar.unlink()

        with tarfile.open(self.config.output_tar, 'w:gz') as tar:
            tar.add(
                self.config.linux_dir / "arch/arm64/boot/Image",
                arcname="arch/arm64/boot/Image"
            )

            for dtb_path in self.config.dtb_files:
                dtb_filename = dtb_path.name.replace('.dtb', f'-{self.config.version}.dtb')
                tar.add(dtb_path, arcname=dtb_filename)
                self.logger.info(f"  Added DTB: {dtb_filename}")

            tar.add(
                self.config.linux_dir / "modules/lib/modules",
                arcname="lib/modules",
                filter=exclude_build
            )

            tar.add(
                self.config.config_file,
                arcname=f"config-{self.config.version}-mnt-reform-arm64"
            )

            for patch_dir in self.patch_dirs_used:
                patches_arcname = f"patches/{patch_dir.name}"
                tar.add(patch_dir, arcname=patches_arcname)
                self.logger.info(f"  Added patch directory: {patches_arcname}")

        self._finalize_tarball(self.config.output_tar, "Deployment")
