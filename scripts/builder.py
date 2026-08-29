# SPDX-License-Identifier: MIT
import logging
import os
import re
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from config import BuildConfig, DEFAULT_CROSS_COMPILE, DEFAULT_KERNEL_ONLY, DTS_CONFIGS, EXTRA_DTB_PATHS, VENDOR_CONFIG_MAP
from errors import BuildError, PatchStats
from logging_setup import Colors

# mnt-overrides files that don't look like a real patch are treated as a skip
# marker. Their text is logged as the skip reason, truncated to this length.
MNT_OVERRIDE_SKIP_REASON_MAX_LEN = 200


class KernelBuilder:
    def __init__(self, config: BuildConfig, logger: logging.Logger,
                 arch: str = "arm64",
                 cross_compile: str = DEFAULT_CROSS_COMPILE,
                 kernel_only: bool = DEFAULT_KERNEL_ONLY,
                 dtbs_only: bool = False,
                 modules_only: bool = False,
                 verruckt: bool = False):
        self.config = config
        self.logger = logger
        self.arch = arch
        self.cross_compile = cross_compile.strip() if cross_compile else ""
        self.kernel_only = kernel_only
        self.dtbs_only = dtbs_only
        self.modules_only = modules_only
        self.verruckt = verruckt
        self.patch_dirs_used: List[Path] = []
        self.patch_stats: Optional[PatchStats] = None

    def log_phase(self, name: str):
        self.logger.info("=" * 60)
        self.logger.info(f"Phase: {name}")
        self.logger.info("=" * 60)

    def _make_kernel_vars(self) -> List[str]:
        args = [f"ARCH={self.arch}", f"LOCALVERSION={self.config.localversion}"]
        if self.cross_compile:
            args.append(f"CROSS_COMPILE={self.cross_compile}")
        return args

    def _uses_dtbs(self) -> bool:
        return self.arch == "arm64"

    def kernel_image_path(self) -> Path:
        return self.config.linux_dir / self._kernel_image_relative_path()

    def modules_install_path(self) -> Path:
        return self.config.linux_dir / "modules" / "lib" / "modules" / self.config.kernel_release

    def _kernel_image_make_target(self) -> str:
        if self.arch == "x86_64":
            return "bzImage"
        return "Image"

    def _kernel_image_relative_path(self) -> Path:
        if self.arch == "x86_64":
            return Path("arch/x86/boot/bzImage")
        return Path(f"arch/{self.arch}/boot/{self._kernel_image_make_target()}")

    def _kernel_srcarch(self) -> str:
        if self.arch == "x86_64":
            return "x86"
        return self.arch

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def run_command(self, cmd: List[str], cwd: Path,
                    check: bool = True, input_data: Optional[str] = None,
                    stream_output: bool = False,
                    log_cmd: bool = True) -> subprocess.CompletedProcess:
        """Run a shell command.

        If stream_output is True, stdout/stderr are streamed live to the logger
        and the log file rather than being captured.
        If log_cmd is False, the command string is logged at DEBUG level instead
        of INFO (useful for high-frequency calls like patch dry-runs).
        """
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

        # stream_output is True here. Use Popen, stream lines to logger + file.
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
                except OSError as e:
                    proc.kill()
                    proc.wait()
                    raise BuildError(f"Failed to write input to command: {cmd_str}") from e

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
        self.logger.info("Checking prerequisites...")

        if not (self.config.linux_dir / "Makefile").exists():
            raise BuildError(f"Kernel source not found at: {self.config.linux_dir}")

        if run_olddefconfig:
            if not self.config.defconfig_file.exists():
                raise BuildError(f"defconfig file not found: {self.config.defconfig_file}")
        else:
            if not self.config.config_file.exists():
                raise BuildError(f"Config file not found: {self.config.config_file}")

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

        failed_log_path = self.config.failed_patch_log()
        self.logger.info("Applying MNT kernel patches...")
        mnt_patch_count = self._apply_patch_set(
            self.config.patches_dir,
            self.config.linux_dir,
            failed_log_path,
            label="",
            on_success=stats.add_success,
            on_failure=stats.add_failure,
            overrides_dir=self.config.mnt_overrides_dir,
            on_skip=stats.add_skipped,
            on_verruckt_no_author=stats.add_verruckt_no_author,
        )
        stats.set_mnt_found(mnt_patch_count)

        xtra_dir = getattr(self.config, 'xtra_patches_dir', None)
        xtra_patch_count = 0
        if xtra_dir is not None:
            self.logger.info("Applying extra kernel patches...")
            xtra_patch_count = self._apply_xtra_patch_sets(stats)
            stats.set_xtra_found(xtra_patch_count)

        if not stats.has_any:
            raise BuildError(
                "No kernel patches found in either "
                f"{self.config.patches_dir} or {self.config.xtra_patches_dir}"
            )

        self.patch_stats = stats

        self.logger.info("Patch application complete!")
        if stats.has_xtra:
            self.logger.info(f"Succeeded: {stats.success}, Succeeded Extra: {stats.xtra_success}")
            self.logger.info(f"Failed:    {stats.failed}, Failed Extra: {stats.xtra_failed}")
            self.logger.info(f"Total:     {stats.total}, Extra Total: {stats.xtra_total}")
        else:
            self.logger.info(f"Succeeded: {stats.success}")
            self.logger.info(f"Failed:    {stats.failed}")
            self.logger.info(f"Total:     {stats.total}")
        if stats.skipped:
            self.logger.info(f"Skipped (mnt-overrides): {stats.skipped} ({', '.join(stats.skipped_patches)})")

        if self.verruckt:
            committed = stats.success + stats.xtra_success
            missing = len(stats.verruckt_no_author)
            if missing:
                self.logger.warning(
                    f"verruckt: {missing} of {committed} commits had no author/date in "
                    "their patch header, committed with the local git identity instead:"
                )
                for patch_name in stats.verruckt_no_author:
                    self.logger.warning(f"  - {patch_name}")
            else:
                self.logger.info(
                    f"{Colors.GREEN}✓{Colors.RESET} verruckt: all {committed} commits got a "
                    "proper author/date from their patch header"
                )

        return stats

    def _apply_xtra_patch_sets(self, stats: PatchStats) -> int:
        """Apply versioned extra patches to linux or supported sibling trees."""
        xtra_dir = self.config.xtra_patches_dir
        if not xtra_dir.exists():
            self.logger.warning(f"No patches found in {xtra_dir} (directory does not exist)")
            return 0

        target_map = {
            "qcacld2": self.config.qcacld_dir,
            "reform-tools": self.config.reform_tools_dir,
        }
        target_specs = []
        linux_patch_files: list[Path] = []

        for patch_file in sorted(xtra_dir.rglob("*.patch")):
            relative_patch = patch_file.relative_to(xtra_dir)
            top_level = relative_patch.parts[0]

            if top_level in target_map:
                continue

            linux_patch_files.append(patch_file)

        target_specs.append(
            {
                "patches_dir": xtra_dir,
                "target_dir": self.config.linux_dir,
                "failed_log_path": self.config.failed_patch_log("-xtra"),
                "label": "extra",
                "patch_files": linux_patch_files,
            }
        )

        for bucket_name, target_dir in target_map.items():
            bucket_dir = xtra_dir / bucket_name
            bucket_patch_files = sorted(bucket_dir.rglob("*.patch")) if bucket_dir.exists() else []
            target_specs.append(
                {
                    "patches_dir": bucket_dir,
                    "target_dir": target_dir,
                    "failed_log_path": self.config.failed_patch_log(f"-xtra-{bucket_name}"),
                    "label": f"extra:{bucket_name}",
                    "patch_files": bucket_patch_files,
                }
            )

        total_patch_count = 0
        for spec in target_specs:
            total_patch_count += self._apply_patch_set(
                spec["patches_dir"],
                spec["target_dir"],
                spec["failed_log_path"],
                spec["label"],
                on_success=stats.add_xtra_success,
                on_failure=stats.add_xtra_failure,
                patch_files=spec["patch_files"],
                on_verruckt_no_author=stats.add_verruckt_no_author,
            )

        return total_patch_count

    def _apply_patch_set(
        self,
        patches_dir: Path,
        target_dir: Path,
        failed_log_path: Path,
        label: str,
        on_success,
        on_failure,
        patch_files: Optional[List[Path]] = None,
        overrides_dir: Optional[Path] = None,
        on_skip: Optional[Callable[[str], None]] = None,
        on_verruckt_no_author: Optional[Callable[[str], None]] = None,
    ) -> int:
        """Apply all *.patch files from patches_dir, recording results via callbacks.

        Args:
            patches_dir:      Directory to search recursively for .patch files.
            target_dir:       Repository root to apply patches against.
            failed_log_path:  File to write failure details to (cleared before use).
            label:            Human-readable qualifier for log messages (e.g. "extra").
                              Pass an empty string for the primary patch set.
            on_success:       Callable invoked (no args) for each successful patch.
            on_failure:       Callable invoked (patch_name) for each failed patch.
            overrides_dir:    Optional mnt-overrides tree, mirroring patches_dir's relative
                              layout. A file at the same relative path that looks like a real
                              patch (contains a unified-diff hunk header) replaces the upstream
                              patch's content. Any other file there (including an empty one)
                              skips the upstream patch entirely. Its text is logged as the skip
                              reason, truncated to MNT_OVERRIDE_SKIP_REASON_MAX_LEN characters.
            on_skip:          Callable invoked (patch_name) for each patch skipped via a
                              non-patch override file.
            on_verruckt_no_author: Callable invoked (patch_name), only when self.verruckt
                              is set, for each committed patch whose author/date couldn't
                              be parsed from its own header.
        """
        qualifier = f" ({label})" if label else ""
        if patch_files is None and not patches_dir.exists():
            self.logger.warning(f"No patches found in {patches_dir} (directory does not exist)")
            return 0

        if not target_dir.exists():
            self.logger.warning(f"Skipping{qualifier} patches from {patches_dir}; target does not exist: {target_dir}")
            return 0

        if patch_files is None:
            patch_files = sorted(patches_dir.rglob("*.patch"))

        if not patch_files:
            self.logger.warning(f"No patches found in {patches_dir}")
            return 0

        if patches_dir not in self.patch_dirs_used:
            self.patch_dirs_used.append(patches_dir)

        self.logger.info(f"Found {len(patch_files)}{qualifier} patches to apply to {target_dir}")

        if failed_log_path.exists():
            failed_log_path.unlink()

        failed_log_entries = []

        for patch_file in patch_files:
            patch_name = str(patch_file.relative_to(patches_dir))
            self.logger.debug(f"Processing{qualifier} patch: {patch_name}")

            patch_source = patch_file
            if overrides_dir is not None:
                override_file = overrides_dir / patch_name
                if override_file.exists():
                    with open(override_file, 'r') as f:
                        override_content = f.read()

                    if self._is_patch_content(override_content):
                        self.logger.info(
                            f"{Colors.YELLOW}↺{Colors.RESET} Using mnt-overrides patch for{qualifier}: {patch_name}"
                        )
                        patch_source = override_file
                    else:
                        reason = override_content.strip()
                        if len(reason) > MNT_OVERRIDE_SKIP_REASON_MAX_LEN:
                            reason = reason[:MNT_OVERRIDE_SKIP_REASON_MAX_LEN]
                            self.logger.warning(
                                f"mnt-overrides skip reason for {patch_name} exceeds "
                                f"{MNT_OVERRIDE_SKIP_REASON_MAX_LEN} characters; truncated"
                            )
                        reason_suffix = f": {reason}" if reason else " (no reason given)"
                        self.logger.info(
                            f"{Colors.YELLOW}⊘{Colors.RESET} Skipping{qualifier} "
                            f"(mnt-overrides) {patch_name}{reason_suffix}"
                        )
                        if on_skip is not None:
                            on_skip(patch_name)
                        continue

            with open(patch_source, 'r') as f:
                patch_content = f.read()

            dry_run_result = self.run_command(
                ['patch', '-p1', '--dry-run'],
                cwd=target_dir,
                input_data=patch_content,
                check=False,
                log_cmd=False
            )

            if dry_run_result.returncode == 0:
                apply_result = self.run_command(
                    ['patch', '-p1'],
                    cwd=target_dir,
                    input_data=patch_content,
                    check=False,
                    log_cmd=False
                )
                if apply_result.returncode == 0:
                    self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Applied{qualifier}: {patch_name}")
                    if self.verruckt:
                        had_author = self._commit_patch(target_dir, patch_name, patch_content, qualifier)
                        if not had_author and on_verruckt_no_author is not None:
                            on_verruckt_no_author(patch_name)
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

        return len(patch_files)

    @staticmethod
    def _is_patch_content(content: str) -> bool:
        """Guesses whether this looks like a real unified diff, not a skip-reason note."""
        return content.startswith("@@ -") or "\n@@ -" in content

    def _format_failed_patch(self, patch_name: str, result: subprocess.CompletedProcess) -> str:
        return (
            f"{'=' * 60}\n"
            f"Failed patch: {patch_name}\n"
            f"{'-' * 60}\n"
            f"{result.stdout}\n"
            f"{result.stderr}\n"
        )

    _PATCH_SUBJECT_PREFIX_RE = re.compile(r'^\[PATCH[^\]]*\]\s*')

    @classmethod
    def _parse_patch_metadata(cls, content: str, fallback_message: str) -> tuple:
        """Best-effort extraction of (author, date, message) from a patch file.

        This repo's patches use two header styles. One is git format-patch/am
        mbox headers (From:/Date:/Subject:). The other is `git log -p`-style
        headers (commit/Author:/Date: followed by an indented message). If
        neither is recognizable, this falls back to (None, None,
        fallback_message). The caller then commits with the local git
        identity and no explicit date.
        """
        lines = content.splitlines()

        diff_start = len(lines)
        for i, line in enumerate(lines):
            if line.startswith("diff --git ") or line.startswith("@@ -"):
                diff_start = i
                break
        header_lines = lines[:diff_start]

        # RFC 5322 header order isn't guaranteed. Some tools, or patches
        # saved straight from an email, put Subject before From/Date. So
        # scan the whole contiguous header block instead of assuming a
        # fixed position for each field.
        blank_idx = len(header_lines)
        for i, line in enumerate(header_lines):
            if not line.strip():
                blank_idx = i
                break
        mbox_header_lines = header_lines[:blank_idx]

        author = None
        date = None
        subject = None
        for i, line in enumerate(mbox_header_lines):
            if line.startswith("From:"):
                author = line[len("From:"):].strip()
            elif line.startswith("Date:"):
                date = line[len("Date:"):].strip()
            elif line.startswith("Subject:"):
                subject = line[len("Subject:"):].strip()
                j = i + 1
                while j < len(mbox_header_lines) and mbox_header_lines[j].startswith(" "):
                    subject += " " + mbox_header_lines[j].strip()
                    j += 1
                subject = cls._PATCH_SUBJECT_PREFIX_RE.sub('', subject).strip()

        if subject:
            j = blank_idx + 1
            while j < len(header_lines) and not header_lines[j].strip():
                j += 1
            body_lines = []
            while j < len(header_lines) and header_lines[j].strip() != "---":
                body_lines.append(header_lines[j])
                j += 1
            body = "\n".join(body_lines).strip()

            message = f"{subject}\n\n{body}" if body else subject
            return (author, date, message)

        for i, line in enumerate(header_lines):
            if line.startswith("Author:"):
                author = line[len("Author:"):].strip()
                date = None
                j = i + 1
                if j < len(header_lines) and header_lines[j].startswith("Date:"):
                    date = header_lines[j][len("Date:"):].strip()
                    j += 1
                while j < len(header_lines) and not header_lines[j].strip():
                    j += 1
                message_lines = [hl[4:] if hl.startswith("    ") else hl.strip()
                                for hl in header_lines[j:]]
                message = "\n".join(message_lines).strip()
                if message:
                    return (author, date, message)

        return (None, None, fallback_message)

    def _commit_patch(self, target_dir: Path, patch_name: str, patch_content: str, qualifier: str) -> bool:
        """For --verruckt, turn a just-applied patch into a real git commit.

        Returns True if a real author and date were extracted from the
        patch. Returns False if it fell back to the local git identity and
        a generic message. The caller uses this to report which patches
        need a properly formatted header.
        """
        author, date, message = self._parse_patch_metadata(
            patch_content, fallback_message=f"Apply {patch_name}"
        )
        had_author = author is not None

        add_result = self.run_command(
            ['git', 'add', '-A'], cwd=target_dir, check=False, log_cmd=False
        )
        if add_result.returncode != 0:
            self.logger.warning(f"verruckt: git add failed for{qualifier} {patch_name}, not committing")
            return had_author

        commit_cmd = ['git', 'commit', '--quiet']
        if author:
            commit_cmd += ['--author', author]
        if date:
            commit_cmd += ['--date', date]
        commit_cmd += ['-F', '-']

        commit_result = self.run_command(
            commit_cmd, cwd=target_dir, input_data=message, check=False, log_cmd=False
        )
        if commit_result.returncode != 0 and (author or date):
            # Author/date from the patch may be in a form git's commit
            # machinery rejects. Retry with just the message.
            commit_result = self.run_command(
                ['git', 'commit', '--quiet', '-F', '-'],
                cwd=target_dir, input_data=message, check=False, log_cmd=False
            )

        if commit_result.returncode == 0:
            self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} verruckt: committed{qualifier}: {patch_name}")
        else:
            self.logger.warning(
                f"verruckt: nothing to commit for{qualifier} {patch_name} "
                "(patch may not have changed any tracked files)"
            )

        return had_author

    # ------------------------------------------------------------------
    # Git / source tree setup
    # ------------------------------------------------------------------

    def _resolve_origin_default_branch(self) -> str:
        """Resolve default branch name from origin/HEAD (falls back to main or master)."""
        head_ref = self.run_command(
            ['git', 'symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD'],
            cwd=self.config.linux_dir,
            check=False
        )
        if head_ref.returncode == 0:
            ref = (head_ref.stdout or "").strip()
            if ref.startswith("origin/"):
                return ref.split("/", 1)[1]

        for candidate in ("main", "master"):
            exists = self.run_command(
                ['git', 'rev-parse', '--verify', f'origin/{candidate}'],
                cwd=self.config.linux_dir,
                check=False
            )
            if exists.returncode == 0:
                return candidate

        raise BuildError("Could not resolve origin default branch (tried origin/HEAD, main, master).")

    def clean_kernel_repo(self):
        """Return kernel repository to a clean development baseline.

        - Syncs tags/refs from origin (force, prune)
        - Resets local default branch to origin/<default>
        - Removes untracked files
        - Removes local-only branches (except default branch)
        - Removes local-only tags
        """
        self.logger.info("Cleaning kernel repository state...")

        if not (self.config.linux_dir / ".git").exists():
            raise BuildError(f"Not a git repository: {self.config.linux_dir}")

        self.logger.info("Fetching origin refs and tags (force/prune)...")
        self.run_command(
            ['git', 'fetch', 'origin', '--prune', '--prune-tags', '--tags', '--force'],
            cwd=self.config.linux_dir
        )

        default_branch = self._resolve_origin_default_branch()

        # Force clear local tracked/untracked changes before branch switch.
        # This command is intentionally destructive as part of explicit `clean`.
        self.logger.info("Discarding local tracked/untracked changes...")
        self.run_command(['git', 'reset', '--hard', 'HEAD'], cwd=self.config.linux_dir)
        self.run_command(['git', 'clean', '-fd'], cwd=self.config.linux_dir)

        self.logger.info(f"Resetting local {default_branch} to origin/{default_branch}...")
        self.run_command(
            ['git', 'checkout', '-f', '-B', default_branch, f'origin/{default_branch}'],
            cwd=self.config.linux_dir
        )
        self.run_command(['git', 'reset', '--hard', f'origin/{default_branch}'], cwd=self.config.linux_dir)
        self.run_command(['git', 'clean', '-fd'], cwd=self.config.linux_dir)

        remote_refs = self.run_command(
            ['git', 'for-each-ref', '--format=%(refname:short)', 'refs/remotes/origin'],
            cwd=self.config.linux_dir
        ).stdout.splitlines()
        remote_branch_names = {
            ref.split('/', 1)[1]
            for ref in remote_refs
            if ref.startswith('origin/') and ref != 'origin/HEAD'
        }

        # Delete local branches not present on origin (except default branch).
        local_branches = self.run_command(
            ['git', 'for-each-ref', '--format=%(refname:short)', 'refs/heads'],
            cwd=self.config.linux_dir
        ).stdout.splitlines()
        deleted_branches = []
        for branch in local_branches:
            if branch == default_branch:
                continue
            if branch not in remote_branch_names:
                self.run_command(['git', 'branch', '-D', branch], cwd=self.config.linux_dir, check=False)
                deleted_branches.append(branch)

        # Remove local-only tags. Keep only tags that exist on origin.
        remote_tags_output = self.run_command(
            ['git', 'ls-remote', '--tags', '--refs', 'origin'],
            cwd=self.config.linux_dir
        ).stdout.splitlines()
        remote_tags = {
            line.split('\t', 1)[1].removeprefix('refs/tags/')
            for line in remote_tags_output
            if '\t' in line and line.split('\t', 1)[1].startswith('refs/tags/')
        }

        local_tags = self.run_command(['git', 'tag', '-l'], cwd=self.config.linux_dir).stdout.splitlines()
        deleted_tags = []
        for tag in local_tags:
            if tag not in remote_tags:
                self.run_command(['git', 'tag', '-d', tag], cwd=self.config.linux_dir, check=False)
                deleted_tags.append(tag)

        # Final tag sync to ensure local tag objects track origin exactly.
        self.run_command(['git', 'fetch', 'origin', '--tags', '--force'], cwd=self.config.linux_dir)

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Kernel repo cleaned")
        self.logger.info(f"Deleted local-only branches: {len(deleted_branches)}")
        self.logger.info(f"Deleted local-only tags: {len(deleted_tags)}")

    def repair_kernel_version_tag(self, remote: str = "stable"):
        """Restore the v{version} tag on a persistent kernel checkout (mnt-linux).

        Currently works differently for mnt-linux and linux stable. May change.
        """
        self.logger.info("Repairing kernel version tag from upstream remote...")

        if not (self.config.linux_dir / ".git").exists():
            raise BuildError(f"Not a git repository: {self.config.linux_dir}")

        tag = f"v{self.config.version}"

        remotes = self.run_command(['git', 'remote'], cwd=self.config.linux_dir).stdout.split()
        if remote not in remotes:
            raise BuildError(
                f"Remote '{remote}' not found in {self.config.linux_dir}. "
                f"Expected a remote pointing at the real upstream kernel.org "
                f"release tags, e.g.:\n"
                f"  git -C {self.config.linux_dir} remote add {remote} "
                f"https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
            )

        self.logger.info(f"Deleting local tag {tag} (if repointed by a prior build)...")
        self.run_command(['git', 'tag', '-d', tag], cwd=self.config.linux_dir, check=False)

        self.logger.info(f"Fetching {tag} from '{remote}'...")
        fetch_result = self.run_command(
            ['git', 'fetch', remote, '--force', f'refs/tags/{tag}:refs/tags/{tag}'],
            cwd=self.config.linux_dir,
            check=False
        )
        if fetch_result.returncode != 0:
            raise BuildError(f"Could not fetch tag {tag} from remote '{remote}'.")

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Tag {tag} restored from '{remote}'")

    def reset_mnt_linux_branch(self, remote: str = "stable"):
        """Hard-reset the mnt-linux fork's branch back to a pristine v{version}.

        Note, this is destructive.
        """
        self.repair_kernel_version_tag(remote=remote)

        tag = f"v{self.config.version}"
        branch = f"mnt-v{self.config.version}"

        local_exists = self.run_command(
            ['git', 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}'],
            cwd=self.config.linux_dir, check=False
        ).returncode == 0
        if not local_exists:
            raise BuildError(
                f"Branch '{branch}' does not exist in {self.config.linux_dir}. "
                f"There is nothing to reset. Create the branch first, e.g.:\n"
                f"  git -C {self.config.linux_dir} switch --create {branch} {tag}"
            )

        self.logger.info(f"Force-checking out {branch}...")
        self.run_command(['git', 'checkout', '-f', branch], cwd=self.config.linux_dir)

        self.logger.info(f"Hard-resetting {branch} to {tag}...")
        self.run_command(['git', 'reset', '--hard', tag], cwd=self.config.linux_dir)
        self.run_command(['git', 'clean', '-ffdx'], cwd=self.config.linux_dir)

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} {branch} reset to {tag}")

    def sync_kernel_checkout(self):
        """Check out the branch/tag matching --kversion for self.config.kernel."""
        if self.config.kernel == "mnt-linux":
            self.checkout_mnt_linux_branch()
        else:
            self.checkout_kernel_version()

    def checkout_mnt_linux_branch(self):
        """Switch to the mnt-v{version} branch named by --kversion.

        Never force-resets or force-checks-out.
        """
        if not (self.config.linux_dir / ".git").exists():
            raise BuildError(f"Not a git repository: {self.config.linux_dir}")

        branch = f"mnt-v{self.config.version}"

        current = self.run_command(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=self.config.linux_dir
        ).stdout.strip()
        if current == branch:
            self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Already on {branch}")
            return

        self.logger.info(f"Switching from {current} to {branch} (from --kversion {self.config.version})...")

        local_exists = self.run_command(
            ['git', 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}'],
            cwd=self.config.linux_dir, check=False
        ).returncode == 0
        if not local_exists:
            raise BuildError(
                f"Branch '{branch}' does not exist in {self.config.linux_dir}. "
                f"--kversion is currently {self.config.version} -- pass the --kversion "
                f"this fork was actually branched at, or create the branch first, e.g.:\n"
                f"  git -C {self.config.linux_dir} fetch stable v{self.config.version}\n"
                f"  git -C {self.config.linux_dir} switch --create {branch} v{self.config.version}"
            )

        checkout_result = self.run_command(
            ['git', 'checkout', branch], cwd=self.config.linux_dir, check=False
        )
        if checkout_result.returncode != 0:
            raise BuildError(
                f"Could not switch to {branch} in {self.config.linux_dir} "
                f"(uncommitted changes on {current}?). git said:\n{checkout_result.stderr}"
            )

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Checked out {branch}")

    def checkout_kernel_version(self):
        """Reset the git repo and check out the target kernel version."""
        self.logger.info("Resetting repository state...")
        self.run_command(['git', 'reset', '--hard', 'HEAD'], cwd=self.config.linux_dir)
        self.run_command(['git', 'clean', '-fd'], cwd=self.config.linux_dir)
        # Ensure we are not on the branch we may delete/recreate below.
        self.run_command(['git', 'checkout', '--detach'], cwd=self.config.linux_dir, check=False)
        self.run_command(['git', 'tag', '-d', f'v{self.config.version}'], cwd=self.config.linux_dir, check=False)

        self.logger.info("Fetching git tags...")
        self.run_command(['git', 'fetch', 'origin', '--prune', '--tags', '--force'], cwd=self.config.linux_dir)

        branch_name = f"mnt-reform-{self.config.version}"
        self.logger.info(f"Checking out kernel version v{self.config.version}...")

        self.run_command(['git', 'branch', '-D', branch_name], cwd=self.config.linux_dir, check=False)
        self.run_command(['git', 'checkout', '-B', branch_name, f'tags/v{self.config.version}'], cwd=self.config.linux_dir)

    def setup_custom_dts_files(self):
        """Copy custom DTS files and update vendor Makefiles.

        Sources are reform-debian-packages (DTS_CONFIGS) and xtra-dtbs/ (optional).
        """
        if not self._uses_dtbs():
            self.logger.info(f"Skipping custom DTS setup for ARCH={self.arch}")
            return

        # Build unified list of (source_path, name, vendor, config_sym)
        all_dts: list[tuple] = []
        for dts_config in DTS_CONFIGS:
            source = self.config.build_dir / f"reform-debian-packages/linux/{dts_config['name']}"
            all_dts.append((source, dts_config['name'], dts_config['vendor'], dts_config['config']))

        xtra_dir = self.config.xtra_dtbs_dir
        if xtra_dir.exists():
            for vendor_dir in sorted(xtra_dir.iterdir()):
                if not vendor_dir.is_dir():
                    continue
                vendor = vendor_dir.name
                config_sym = VENDOR_CONFIG_MAP.get(vendor)
                if config_sym is None:
                    self.logger.warning(f"Unknown vendor '{vendor}' in xtra-dtbs, skipping")
                    continue
                for dts_file in sorted(vendor_dir.glob("*.dts")):
                    all_dts.append((dts_file, dts_file.name, vendor, config_sym))
            xtra_count = len(all_dts) - len(DTS_CONFIGS)
            if xtra_count:
                self.logger.info(f"Found {xtra_count} extra DTS file(s) in {xtra_dir}")

        self.logger.info(f"Adding {len(all_dts)} custom DTS files...")

        for source, name, vendor, _ in all_dts:
            if not source.exists():
                raise BuildError(f"Custom DTS file not found: {source}")
            dts_dest = self.config.linux_dir / f"arch/arm64/boot/dts/{vendor}/{name}"
            shutil.copy2(source, dts_dest)
            self.logger.info(f"  Copied {name} to {vendor}/")

        # Group by vendor to avoid processing the same Makefile multiple times
        vendors_to_update: dict = {}
        for _, name, vendor, config_sym in all_dts:
            if vendor not in vendors_to_update:
                vendors_to_update[vendor] = []
            vendors_to_update[vendor].append((name.replace('.dts', '.dtb'), config_sym))

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

        if EXTRA_DTB_PATHS:
            self.logger.info(f"Also shipping {len(EXTRA_DTB_PATHS)} upstream DTB(s) (built by kernel, not copied):")
            for path in EXTRA_DTB_PATHS:
                self.logger.info(f"  {Path(path).name}")

    # ------------------------------------------------------------------
    # Config management
    # ------------------------------------------------------------------

    def update_config_with_olddefconfig(self, skip_git_operations: bool = False):
        """Prepare the kernel like a normal build, run olddefconfig, then save
        the result back to the configs directory."""
        self.logger.info("Updating kernel config with olddefconfig...")
        self.logger.info("Preparing kernel to build state before running olddefconfig...")

        if not self.config.defconfig_file.exists():
            raise BuildError(f"defconfig not found: {self.config.defconfig_file}")
        self.logger.info(f"Copying {self.config.defconfig_file} to .config...")
        shutil.copy2(self.config.defconfig_file, self.config.linux_dir / '.config')

        self.logger.info("Running olddefconfig to update config defaults...")
        self.run_command([
            'make',
            *self._make_kernel_vars(),
            'olddefconfig'
        ], cwd=self.config.linux_dir)

        self.logger.info(f"Saving updated config to {self.config.config_file}...")
        self.config.config_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.config.linux_dir / '.config', self.config.config_file)

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Config updated successfully")

    def clean_in_tree_kernel_artifacts(self):
        """Remove in-tree Kbuild outputs while preserving the patched checkout."""
        self.logger.info("Cleaning in-tree kernel build artifacts...")
        self.run_command([
            'make',
            *self._make_kernel_vars(),
            'mrproper'
        ], cwd=self.config.linux_dir)
        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} In-tree kernel artifacts removed")

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

        self.log_phase("Source Prep")
        if self.config.kernel == "mnt-linux" or not skip_git_operations:
            self.sync_kernel_checkout()

        self.log_phase("Patching")
        patch_stats = self.apply_patches()
        if patch_stats.failed > 0:
            self.logger.warning(
                f"{patch_stats.failed} patches failed to apply. "
                "Build will continue, but may fail or produce unexpected results."
            )

        if self._uses_dtbs():
            self.log_phase("DTS Setup")
            self.setup_custom_dts_files()
        else:
            self.logger.info(f"Skipping DTS setup for ARCH={self.arch}")

        if run_olddefconfig:
            self.log_phase("Config Update")
            self.update_config_with_olddefconfig(skip_git_operations=skip_git_operations)
        else:
            self.logger.info("Copying kernel config...")
            shutil.copy2(self.config.config_file, self.config.linux_dir / '.config')

        if self.config.kernel == "mnt-linux":
            # Note the kernel release string may show -dirty or -g<hash>.
            self.logger.info("Skipping Git Snapshot for mnt-linux (would commit/retag real history).")
        else:
            # Commit and tag so the kernel version string doesn't end up -dirty.
            # Ideally we'd build outside a git repo entirely, but this works for now.
            self.log_phase("Git Snapshot")
            self.logger.info("Create git tag and commit.")
            self.run_command(['git', 'add', '--all'], cwd=self.config.linux_dir)
            self.run_command(['git', 'commit', '-s', '-m', f'MNT Reform Linux v{self.config.version}'], cwd=self.config.linux_dir)
            self.run_command(['git', 'tag', '-d', f'v{self.config.version}'], cwd=self.config.linux_dir, check=False)
            self.run_command(
                ['git', 'tag', '-a', f'v{self.config.version}', '-m', f'MNT Reform Linux v{self.config.version}'],
                cwd=self.config.linux_dir
            )

        if self.kernel_only:
            self.logger.info(
                f"Compiling kernel image only with {self.config.jobs} jobs "
                "(skipping dtbs and modules)..."
            )
            make_targets = [self._kernel_image_make_target()]
        elif self.dtbs_only:
            if not self._uses_dtbs():
                raise BuildError(f"--dtbs-only is not supported for ARCH={self.arch}")
            self.logger.info(f"Compiling DTBs only with {self.config.jobs} jobs...")
            make_targets = ['dtbs']
        elif self.modules_only:
            self.logger.info(f"Compiling modules only with {self.config.jobs} jobs...")
            make_targets = ['modules']
        else:
            self.logger.info(f"Compiling kernel with {self.config.jobs} jobs (this may take a while)...")
            make_targets = [self._kernel_image_make_target(), 'modules']
            if self._uses_dtbs():
                make_targets.insert(1, 'dtbs')

        self.log_phase("Compile")
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

        # Install modules when we built them (all modes except kernel-only and dtbs-only)
        if not self.kernel_only and not self.dtbs_only:
            modules_dir = self.config.linux_dir / "modules"
            self.logger.info(f"Installing modules to {modules_dir}...")
            if modules_dir.exists():
                shutil.rmtree(modules_dir)
            self.log_phase("Module Install")
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
                f'SRCARCH={self._kernel_srcarch()}',
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

    def collect_dtbs(self) -> Path:
        """Copy built DTBs into <build_dir>/dtbs/, named with the kernel release suffix."""
        dtbs_dir = self.config.build_dir / "dtbs"
        if dtbs_dir.exists():
            shutil.rmtree(dtbs_dir)
        dtbs_dir.mkdir(parents=True)

        for dtb_path in self.config.dtb_files:
            if not dtb_path.exists():
                raise BuildError(f"DTB not found: {dtb_path}")
            dest_name = dtb_path.name.replace('.dtb', f'-{self.config.kernel_release}.dtb')
            shutil.copy2(dtb_path, dtbs_dir / dest_name)
            self.logger.info(f"  {dest_name}")

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} DTBs collected: {dtbs_dir}")
        return dtbs_dir

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
        self.logger.info("Creating headers tarball...")

        if headers_dir is None:
            headers_dir = self.config.build_dir / "headers-extmod"

        if not headers_dir.exists():
            raise BuildError(f"Headers directory not found: {headers_dir}")

        if self.config.output_headers_tar.exists():
            self.config.output_headers_tar.unlink()

        with tarfile.open(self.config.output_headers_tar, 'w:gz') as tar:
            tar.add(headers_dir, arcname=f"linux-{self.config.build_version}")

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
                        self.config.build_dir / "qcacld2/debian/bdwlan30.bin",
                        "usr/lib/firmware/qcacld2/bdwlan30.bin",
                    ),
                    (
                        self.config.build_dir / "qcacld2/debian/otp30.bin",
                        "usr/lib/firmware/qcacld2/otp30.bin",
                    ),
                    (
                        self.config.build_dir / "qcacld2/debian/qwlan30.bin",
                        "usr/lib/firmware/qcacld2/qwlan30.bin",
                    ),
                    (
                        self.config.build_dir / "qcacld2/debian/cfg.dat",
                        "usr/lib/firmware/wlan/qcacld2/cfg.dat",
                    ),
                    (
                        self.config.build_dir / "qcacld2/debian/qcom_cfg.ini",
                        "usr/lib/firmware/wlan/qcacld2/qcom_cfg.ini",
                    ),
                    (
                        self.config.build_dir / "qcacld2/debian/reform-qcacld2.conf",
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

        kernel_image = self.config.linux_dir / self._kernel_image_relative_path()
        required_files = {
            'kernel': kernel_image,
            'config': self.config.config_file,
            'modules': self.config.linux_dir / "modules/lib/modules"
        }

        for name, path in required_files.items():
            if not path.exists():
                raise BuildError(f"Required file missing ({name}): {path}")

        dtbs_dir = None
        if self._uses_dtbs():
            self.log_phase("Collect DTBs")
            dtbs_dir = self.collect_dtbs()

        if self.config.output_tar.exists():
            self.config.output_tar.unlink()

        with tarfile.open(self.config.output_tar, 'w:gz') as tar:
            tar.add(
                kernel_image,
                arcname=str(self._kernel_image_relative_path())
            )

            if dtbs_dir is not None:
                for dtb_file in sorted(dtbs_dir.iterdir()):
                    tar.add(dtb_file, arcname=dtb_file.name)
                    self.logger.info(f"  Added DTB: {dtb_file.name}")

            tar.add(
                self.config.linux_dir / "modules/lib/modules",
                arcname="lib/modules",
                filter=exclude_build
            )

            tar.add(
                self.config.config_file,
                arcname=f"config-{self.config.version}-mnt-reform-{self.config.arch}"
            )

            for patch_dir in self.patch_dirs_used:
                patches_arcname = f"patches/{patch_dir.name}"
                tar.add(patch_dir, arcname=patches_arcname)
                self.logger.info(f"  Added patch directory: {patches_arcname}")

        self._finalize_tarball(self.config.output_tar, "Deployment")
