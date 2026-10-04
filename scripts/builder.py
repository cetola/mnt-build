# SPDX-License-Identifier: MIT
import logging
import os
import re
import shutil
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from config import (
    BuildConfig, DEFAULT_CROSS_COMPILE, DEFAULT_KERNEL_ONLY, EXTRA_DTB_PATHS,
    KERNEL_BRANCH_REMOTES, KERNEL_REMOTES_FILE, VENDOR_CONFIG_MAP, load_kernel_remotes,
    normalize_git_url, version_key,
)
from errors import BuildError, PatchStats
from logging_setup import Colors, Spinner

# Seconds to wait for a remote to answer a status check before calling it unreachable.
REMOTE_STATUS_TIMEOUT = 10
# Seconds to wait for a single-branch fetch before giving up on that remote.
REMOTE_FETCH_TIMEOUT = 600


class KernelBuilder:
    def __init__(self, config: BuildConfig, logger: logging.Logger,
                 arch: str = "arm64",
                 cross_compile: str = DEFAULT_CROSS_COMPILE,
                 kernel_only: bool = DEFAULT_KERNEL_ONLY,
                 dtbs_only: bool = False,
                 modules_only: bool = False):
        self.config = config
        self.logger = logger
        self.arch = arch
        self.cross_compile = cross_compile.strip() if cross_compile else ""
        self.kernel_only = kernel_only
        self.dtbs_only = dtbs_only
        self.modules_only = modules_only
        self.patch_dirs_used: List[Path] = []
        self.patch_stats: Optional[PatchStats] = None
        # Untracked files in the kernel tree before source prep. None until the
        # tree has been verified clean, so restore_kernel_tree() never runs on
        # a tree that held someone's uncommitted work.
        self._untracked_before: Optional[set] = None
        self._created_files: set = set()

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
        # log_file is None when file logging is off
        logfile_path = Path(self.config.log_file or os.devnull)
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
        """Apply xtra-patches, if there are any, on top of the checked-out branch."""
        stats = PatchStats()

        self.logger.info("Applying extra kernel patches...")
        stats.set_found(self._apply_xtra_patch_sets(stats))

        self.patch_stats = stats
        self._record_created_files()

        if not stats.found:
            self.logger.info("No patches to apply. Building the branch as committed.")
            return stats

        self.logger.info("Patch application complete!")
        self.logger.info(f"Succeeded: {stats.success}")
        self.logger.info(f"Failed:    {stats.failed}")
        self.logger.info(f"Total:     {stats.total}")

        return stats

    def _apply_xtra_patch_sets(self, stats: PatchStats) -> int:
        """Apply versioned extra patches to linux or supported sibling trees."""
        xtra_dir = self.config.xtra_patches_dir
        if not xtra_dir.exists():
            self.logger.info(f"No patches found in {xtra_dir} (directory does not exist)")
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
                on_success=stats.add_success,
                on_failure=stats.add_failure,
                patch_files=spec["patch_files"],
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

            with open(patch_file, 'r') as f:
                patch_content = f.read()

            dry_run_result = self.run_command(
                ['patch', '-p1', '--no-backup-if-mismatch', '--dry-run'],
                cwd=target_dir,
                input_data=patch_content,
                check=False,
                log_cmd=False
            )

            if dry_run_result.returncode == 0:
                apply_result = self.run_command(
                    ['patch', '-p1', '--no-backup-if-mismatch'],
                    cwd=target_dir,
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

        return len(patch_files)

    def _format_failed_patch(self, patch_name: str, result: subprocess.CompletedProcess) -> str:
        return (
            f"{'=' * 60}\n"
            f"Failed patch: {patch_name}\n"
            f"{'-' * 60}\n"
            f"{result.stdout}\n"
            f"{result.stderr}\n"
        )

    _PATCH_SUBJECT_PREFIX_RE = re.compile(r'^\[PATCH[^\]]*\]\s*')

    # ------------------------------------------------------------------
    # Kernel checkout
    # ------------------------------------------------------------------

    def clean_kernel_repo(self):
        """Discard uncommitted changes and untracked files in the kernel checkout.

        Leaves commits, branches and tags alone. Ignored files (build outputs,
        .config) stay too.
        """
        linux_dir = self.config.linux_dir
        if not (linux_dir / ".git").exists():
            raise BuildError(f"Not a git repository: {linux_dir}")
        if self._rebase_in_progress():
            raise BuildError(
                f"A rebase is in progress in {linux_dir}. Finish it with "
                "'git rebase --continue' or drop it with 'git rebase --abort'."
            )

        self.logger.info("Discarding local tracked/untracked changes...")
        self.run_command(['git', 'reset', '--hard', 'HEAD'], cwd=linux_dir)
        self.run_command(['git', 'clean', '-fd'], cwd=linux_dir)

        branch = self.run_command(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=linux_dir, log_cmd=False
        ).stdout.strip()
        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Kernel repo cleaned, still on {branch}")

    def _load_kernel_remotes(self):
        """Return (wanted, existing): kernel-remotes.data entries and the checkout's remotes."""
        linux_dir = self.config.linux_dir
        if not (linux_dir / ".git").exists():
            raise BuildError(f"Not a git repository: {linux_dir}")

        remotes_file = self.config.build_dir / KERNEL_REMOTES_FILE
        if not remotes_file.is_file():
            raise BuildError(f"Kernel remotes file not found: {remotes_file}")
        try:
            wanted = load_kernel_remotes(remotes_file)
        except ValueError as e:
            raise BuildError(str(e))

        existing = {}
        for name in self.run_command(['git', 'remote'], cwd=linux_dir, log_cmd=False).stdout.split():
            existing[name] = self.run_command(
                ['git', 'remote', 'get-url', name], cwd=linux_dir, log_cmd=False
            ).stdout.strip()
        return wanted, existing

    def _ls_remote(self, name: str, *args: str) -> Optional[dict]:
        """Return {ref: sha} from git ls-remote, or None if the remote did not answer."""
        flags = [a for a in args if a.startswith('-')]
        patterns = [a for a in args if not a.startswith('-')]
        cmd = ['git', 'ls-remote', *flags, name, *patterns]
        # Not run_command: needs a timeout and must never stop for a password prompt.
        self.logger.debug(f"$ {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd, cwd=self.config.linux_dir, capture_output=True, text=True,
                timeout=REMOTE_STATUS_TIMEOUT,
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0',
                     'GIT_SSH_COMMAND': os.environ.get('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes')},
            )
        except subprocess.TimeoutExpired:
            self.logger.debug(f"No answer from {name} in {REMOTE_STATUS_TIMEOUT}s")
            return None
        if result.returncode != 0:
            self.logger.debug(f"stderr: {result.stderr.strip()}")
            return None

        refs = {}
        for line in result.stdout.splitlines():
            sha, ref = line.split(None, 1)
            refs[ref] = sha
        return refs

    def _kernel_remote_state(self, name: str, offline: bool) -> str:
        linux_dir = self.config.linux_dir
        local = {}
        refs = self.run_command(
            ['git', 'for-each-ref', '--format=%(objectname) %(refname)', f'refs/remotes/{name}/'],
            cwd=linux_dir, log_cmd=False
        ).stdout.splitlines()
        for line in refs:
            sha, ref = line.split(' ', 1)
            branch = ref[len(f'refs/remotes/{name}/'):]
            if branch != 'HEAD':
                local[branch] = sha

        if offline:
            if not local:
                return "never fetched"
            return f"fetched ({len(local)} branches), not checked"

        remote = self._ls_remote(name, '--heads')
        if remote is None:
            return f"{Colors.RED}unreachable{Colors.RESET}"
        remote = {ref[len('refs/heads/'):]: sha for ref, sha in remote.items()}

        if not remote and not local:
            return "empty remote"
        if not local:
            return f"{Colors.YELLOW}never fetched{Colors.RESET} ({len(remote)} branches on remote)"
        differing = sum(1 for b in set(local) | set(remote) if local.get(b) != remote.get(b))
        if differing:
            return (f"{Colors.YELLOW}out of date{Colors.RESET} "
                    f"({differing} of {len(set(local) | set(remote))} branches differ)")
        return f"{Colors.GREEN}up to date{Colors.RESET} ({len(local)} branches)"

    def show_kernel_remotes(self, offline: bool = False):
        wanted, existing = self._load_kernel_remotes()
        by_name = {name: url for name, url, _ in wanted}
        by_repo = {normalize_git_url(url): name for name, url, _ in wanted}

        rows = []
        covered = set()
        for name, url in existing.items():
            if offline:
                state = self._kernel_remote_state(name, offline)
            else:
                with Spinner(f"Checking {name}..."):
                    state = self._kernel_remote_state(name, offline)
            if name in by_name:
                covered.add(name)
                if normalize_git_url(by_name[name]) != normalize_git_url(url):
                    state += f", {Colors.RED}URL differs from {KERNEL_REMOTES_FILE}{Colors.RESET}"
            elif normalize_git_url(url) in by_repo:
                listed_as = by_repo[normalize_git_url(url)]
                covered.add(listed_as)
                state += f", listed as '{listed_as}'"
            else:
                state += f", not in {KERNEL_REMOTES_FILE}"
            rows.append((name, url, state))
        for name, url, _ in wanted:
            if name not in covered:
                rows.append((name, url, f"{Colors.YELLOW}not added{Colors.RESET}"))

        name_w = max((len(r[0]) for r in rows), default=0)
        url_w = max((len(r[1]) for r in rows), default=0)
        for name, url, state in rows:
            self.logger.info(f"{name:<{name_w}}  {url:<{url_w}}  {state}")

        missing = sum(1 for name, _, _ in wanted if name not in covered)
        if missing:
            self.logger.info(
                f"{missing} remote(s) from {KERNEL_REMOTES_FILE} not added. "
                "Run: mnt-build dev-kernel add-remotes"
            )

    def fetch_kernel_remotes(self):
        linux_dir = self.config.linux_dir
        _, existing = self._load_kernel_remotes()

        failed = []
        for name in existing:
            self.logger.info(f"Fetching '{name}'...")
            result = self.run_command(
                ['git', 'fetch', '--no-progress', name],
                cwd=linux_dir, check=False, stream_output=True
            )
            if result.returncode != 0:
                failed.append(name)
                self.logger.error(f"Fetch from '{name}' failed")
            else:
                self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} {name}: fetched")

        if failed:
            raise BuildError(f"Could not fetch from: {', '.join(failed)}")

    def ensure_kernel_remotes(self):
        linux_dir = self.config.linux_dir
        wanted, existing = self._load_kernel_remotes()
        remotes_file = self.config.build_dir / KERNEL_REMOTES_FILE

        added = 0
        for name, url, push_url in wanted:
            target = name
            if name in existing:
                if normalize_git_url(existing[name]) != normalize_git_url(url):
                    raise BuildError(
                        f"Remote '{name}' in {linux_dir} points at {existing[name]}, "
                        f"but {remotes_file.name} expects {url}. Fix one of them by hand."
                    )
                self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} {name}: present")
            else:
                # The same repo may already be here under another name (e.g. origin).
                alias = next(
                    (n for n, u in existing.items()
                     if normalize_git_url(u) == normalize_git_url(url)),
                    None
                )
                if alias:
                    target = alias
                    self.logger.info(
                        f"{Colors.GREEN}✓{Colors.RESET} {name}: present as '{alias}'"
                    )
                else:
                    self.run_command(['git', 'remote', 'add', name, url], cwd=linux_dir)
                    existing[name] = url
                    added += 1
                    self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} {name}: added ({url})")

            if push_url:
                current_push = self.run_command(
                    ['git', 'remote', 'get-url', '--push', target], cwd=linux_dir, log_cmd=False
                ).stdout.strip()
                if current_push != push_url:
                    self.run_command(
                        ['git', 'remote', 'set-url', '--push', target, push_url], cwd=linux_dir
                    )
                    self.logger.info(f"  {target}: push URL set to {push_url}")

        self.logger.info(f"Remotes added: {added}")

    def _resolve_kernel_remote(self, listed: str) -> Optional[str]:
        """Name in the checkout of a remote listed in kernel-remotes.data, if it is set up."""
        wanted, existing = self._load_kernel_remotes()
        url = {name: url for name, url, _ in wanted}.get(listed)
        if url is None:
            return None
        if listed in existing:
            return listed
        # The same repo may already be here under another name (e.g. origin).
        return next(
            (n for n, u in existing.items() if normalize_git_url(u) == normalize_git_url(url)),
            None
        )

    def _kernel_branch_remotes(self) -> List[str]:
        """Remotes in the checkout to look for mnt-v branches on, in order of preference."""
        remotes = [self._resolve_kernel_remote(listed) for listed in KERNEL_BRANCH_REMOTES]
        return [r for r in remotes if r]

    def _ref_exists(self, ref: str) -> bool:
        return self.run_command(
            ['git', 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}'],
            cwd=self.config.linux_dir, check=False, log_cmd=False
        ).returncode == 0

    def _series(self) -> str:
        parts = self.config.version.split('.')
        return f"{parts[0]}.{parts[1]}"

    def _mnt_linux_branches(self, remotes: List[str]) -> List[tuple]:
        """mnt-v branches of this kernel series known here, newest first.

        Returns (version_key, ref) pairs. For one version, the local branch
        sorts ahead of the remotes, which sort in order of preference.
        """
        series = self._series()
        places = ['refs/heads/'] + [f'refs/remotes/{r}/' for r in remotes]
        found = []
        for rank, place in enumerate(places):
            refs = self.run_command(
                ['git', 'for-each-ref', '--format=%(refname)', f'{place}mnt-v{series}.*'],
                cwd=self.config.linux_dir, log_cmd=False
            ).stdout.split()
            for ref in refs:
                key = version_key(ref[len(place) + len('mnt-v'):])
                if key is not None:
                    short = ref.removeprefix('refs/heads/').removeprefix('refs/remotes/')
                    found.append((key, rank, short))
        found.sort(key=lambda f: (tuple(-n for n in f[0]), f[1]))
        return [(key, short) for key, _, short in found]

    def _latest_stable_version(self, offline: bool) -> Optional[str]:
        """Newest stable release of this kernel series, as tagged on the stable remote."""
        series = self._series()
        stable = self._resolve_kernel_remote("stable")
        tags = None
        if not offline and stable:
            refs = self._ls_remote(stable, '--tags', '--refs', f'v{series}', f'v{series}.*')
            if refs is not None:
                tags = [ref[len('refs/tags/'):] for ref in refs]
        if tags is None:
            tags = self.run_command(
                ['git', 'tag', '-l', f'v{series}', f'v{series}.*'],
                cwd=self.config.linux_dir, log_cmd=False
            ).stdout.split()
        versions = [t[1:] for t in tags if version_key(t[1:]) is not None]
        return max(versions, key=version_key, default=None)

    def show_kernel_versions(self, offline: bool = False):
        """Report whether an mnt-v branch exists for the latest stable kernel of this series."""
        series = self._series()
        remotes = self._kernel_branch_remotes()

        if offline:
            latest = self._latest_stable_version(offline)
        else:
            with Spinner("Checking stable..."):
                latest = self._latest_stable_version(offline)
        if latest is None:
            self.logger.warning(
                f"No stable v{series} tags known. Run: mnt-build dev-kernel fetch"
            )
            return
        source = "tags fetched earlier, not checked" if offline else "stable remote"
        self.logger.info(f"Latest stable {series}: v{latest} ({source})")

        branch = f"mnt-v{latest}"
        have = []
        if self._ref_exists(f'refs/heads/{branch}'):
            have.append("local")
        for remote in remotes:
            if offline:
                found = self._ref_exists(f'refs/remotes/{remote}/{branch}')
            else:
                with Spinner(f"Checking {remote}..."):
                    refs = self._ls_remote(remote, '--heads', branch)
                if refs is None:
                    found = self._ref_exists(f'refs/remotes/{remote}/{branch}')
                    self.logger.warning(
                        f"Could not reach {remote}. Going by what was fetched earlier."
                    )
                else:
                    found = bool(refs)
            if found:
                have.append(remote)

        if have:
            self.logger.info(
                f"{Colors.GREEN}✓{Colors.RESET} {branch} is available ({', '.join(have)}). "
                f"Build with: mnt-build build --kversion {latest}"
            )
            return

        where = ' or '.join(['locally', *(f'on {r}' for r in remotes)])
        self.logger.info(f"{Colors.YELLOW}{branch} does not exist{Colors.RESET} {where}")
        known = self._mnt_linux_branches(remotes)
        if known:
            self.logger.info(f"Newest {series} branch known here: {known[0][1]}")
            self.logger.info(f"Rebase it with: mnt-build dev-kernel rebase --kversion {latest}")
        else:
            self.logger.info(
                f"No mnt-v{series}.x branch is known here. Run: mnt-build dev-kernel fetch"
            )

    def _ensure_stable_tag(self, tag: str):
        """Make sure a stable release tag is in the checkout, fetching it if needed."""
        if self._ref_exists(f'refs/tags/{tag}'):
            return
        stable = self._resolve_kernel_remote("stable")
        if stable is None:
            raise BuildError(
                f"Tag {tag} is not in {self.config.linux_dir} and no 'stable' remote is set up "
                "to fetch it from. Run: mnt-build dev-kernel add-remotes"
            )
        self.logger.info(f"Fetching {tag} from '{stable}'...")
        result = self.run_command(
            ['git', 'fetch', '--no-progress', stable, f'refs/tags/{tag}:refs/tags/{tag}'],
            cwd=self.config.linux_dir, check=False
        )
        if result.returncode != 0:
            raise BuildError(
                f"Could not fetch tag {tag} from '{stable}'. Has it been released? "
                f"git said:\n{result.stderr.strip()}"
            )

    def _rebase_in_progress(self) -> bool:
        for name in ('rebase-merge', 'rebase-apply'):
            path = self.run_command(
                ['git', 'rev-parse', '--git-path', name],
                cwd=self.config.linux_dir, log_cmd=False
            ).stdout.strip()
            if (self.config.linux_dir / path).exists():
                return True
        return False

    def rebase_mnt_linux_branch(self, source: Optional[str] = None):
        """Create mnt-v{version} by rebasing an existing mnt-v branch onto v{version}.

        Always works on a new local branch. The source branch is not modified
        and nothing is pushed. On a conflict the rebase is left in progress.
        """
        linux_dir = self.config.linux_dir
        if not (linux_dir / ".git").exists():
            raise BuildError(f"Not a git repository: {linux_dir}")

        version = self.config.version
        tag = f"v{version}"
        branch = f"mnt-v{version}"

        # A rebase writes new commits. Without an identity it stops on the first one.
        if self.run_command(
            ['git', 'var', 'GIT_COMMITTER_IDENT'], cwd=linux_dir, check=False, log_cmd=False
        ).returncode != 0:
            raise BuildError(
                "git has no committer identity here, so it cannot rebase. Set one with:\n"
                '  git config --global user.name "Your Name"\n'
                '  git config --global user.email "you@example.com"'
            )
        self.require_clean_kernel_tree()
        # Nothing here is taken back out afterwards.
        self._untracked_before = None

        remotes = self._kernel_branch_remotes()
        for ref in [f'refs/heads/{branch}'] + [f'refs/remotes/{r}/{branch}' for r in remotes]:
            if self._ref_exists(ref):
                short = ref.removeprefix('refs/heads/').removeprefix('refs/remotes/')
                raise BuildError(
                    f"{branch} already exists ({short}). Nothing to rebase. Build it with: "
                    f"mnt-build build --kversion {version}"
                )

        if source is None:
            known = [ref for key, ref in self._mnt_linux_branches(remotes)]
            if not known:
                raise BuildError(
                    f"No mnt-v{self._series()}.x branch is known in {linux_dir} to rebase from. "
                    "Run 'mnt-build dev-kernel fetch', or name a branch with --from."
                )
            source = known[0]
        elif not self._ref_exists(source):
            raise BuildError(f"--from {source}: no such branch or commit in {linux_dir}")

        # The stable tag the source branch sits on.
        match = re.search(r'mnt-v(\d+\.\d+(?:\.\d+)?)$', source)
        if match:
            old_tag = f"v{match.group(1)}"
            self._ensure_stable_tag(old_tag)
        else:
            old_tag = self.run_command(
                ['git', 'describe', '--tags', '--abbrev=0', '--match', 'v[0-9]*', source],
                cwd=linux_dir, check=False
            ).stdout.strip()
            if not old_tag:
                raise BuildError(f"Could not find the stable tag that {source} is based on.")
        is_base = self.run_command(
            ['git', 'merge-base', '--is-ancestor', old_tag, source], cwd=linux_dir, check=False
        ).returncode == 0
        if not is_base:
            raise BuildError(f"{source} is not based on {old_tag}. Refusing to guess its base.")
        if old_tag == tag:
            raise BuildError(f"{source} is already based on {tag}.")

        self._ensure_stable_tag(tag)

        def count(rev_range: str) -> int:
            return int(self.run_command(
                ['git', 'rev-list', '--count', '--no-merges', rev_range],
                cwd=linux_dir, log_cmd=False
            ).stdout.strip())

        before = count(f'{old_tag}..{source}')
        self.logger.info(
            f"Rebasing {before} commits from {source} ({old_tag}) onto {tag} as new branch {branch}..."
        )
        self.run_command(['git', 'switch', '--no-track', '--create', branch, source], cwd=linux_dir)
        result = self.run_command(
            ['git', 'rebase', '--onto', tag, old_tag], cwd=linux_dir,
            check=False, stream_output=True
        )
        if result.returncode != 0:
            raise BuildError(
                f"The rebase stopped. {linux_dir} is on {branch} with the rebase in progress.\n"
                "Resolve it there:\n"
                "  git status                # what is in conflict\n"
                "  git rebase --continue     # after fixing and 'git add'\n"
                "  git rebase --skip         # drop this commit, e.g. it went upstream\n"
                "To give up instead:\n"
                f"  git rebase --abort && git switch - && git branch -D {branch}"
            )

        after = count(f'{tag}..{branch}')
        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} {branch} created: {after} commits on {tag}")
        if after != before:
            self.logger.info(
                f"{before - after} commit(s) from {source} were dropped as already in {tag}"
            )
        self.logger.info("The branch is local only. Nothing was pushed.")
        olddefconfig = "" if self.config.config_file.exists() else " --olddefconfig"
        self.logger.info(f"Next: mnt-build build --kversion {version}{olddefconfig}")

    def _untracked_files(self) -> set:
        return set(self.run_command(
            ['git', 'ls-files', '--others', '--exclude-standard', '-z'],
            cwd=self.config.linux_dir, log_cmd=False
        ).stdout.split('\0')) - {''}

    def require_clean_kernel_tree(self):
        """Refuse to build on a persistent kernel checkout with uncommitted changes.

        xtra-patches and custom DTS files are applied to the working tree and
        taken back out by restore_kernel_tree(). That is only safe if nothing
        else was modified first.
        """
        if self._rebase_in_progress():
            raise BuildError(
                f"A rebase is in progress in {self.config.linux_dir}. Finish it with "
                "'git rebase --continue' or drop it with 'git rebase --abort'."
            )
        changed = self.run_command(
            ['git', 'status', '--porcelain', '--untracked-files=no'],
            cwd=self.config.linux_dir, log_cmd=False
        ).stdout.splitlines()
        if changed:
            shown = '\n'.join(f"  {line}" for line in changed[:10])
            more = f"\n  ... and {len(changed) - 10} more" if len(changed) > 10 else ""
            raise BuildError(
                f"{self.config.linux_dir} has uncommitted changes:\n{shown}{more}\n"
                "Commit, stash, or discard them before building. If a previous build "
                "was killed before it could take its patches back out, discard them with:\n"
                f"  git -C {self.config.linux_dir} checkout HEAD -- ."
            )
        self._untracked_before = self._untracked_files()

    def _record_created_files(self):
        """Note files that patching or DTS setup added to a persistent kernel checkout."""
        if self._untracked_before is None:
            return
        self._created_files |= self._untracked_files() - self._untracked_before

    def restore_kernel_tree(self):
        """Take xtra-patches and custom DTS files back out of a persistent kernel checkout."""
        if self._untracked_before is None:
            return
        self._untracked_before = None

        self.logger.info(f"Restoring {self.config.linux_dir} to its committed state...")
        self.run_command(['git', 'checkout', 'HEAD', '--', '.'], cwd=self.config.linux_dir)
        for name in sorted(self._created_files):
            (self.config.linux_dir / name).unlink(missing_ok=True)
        self.logger.info(
            f"{Colors.GREEN}✓{Colors.RESET} Kernel tree restored "
            f"({len(self._created_files)} added file(s) removed)"
        )
        self._created_files = set()

    def checkout_mnt_linux_branch(self):
        """Switch to the mnt-v{version} branch named by --kversion.

        Uses the local branch if there is one, otherwise the first of
        KERNEL_BRANCH_REMOTES that has it. Never force-resets, force-checks-out
        or rebases.
        """
        if not (self.config.linux_dir / ".git").exists():
            raise BuildError(f"Not a git repository: {self.config.linux_dir}")

        self.require_clean_kernel_tree()

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
            remotes = self._kernel_branch_remotes()
            if not remotes:
                raise BuildError(
                    f"None of the remotes that carry mnt-v branches "
                    f"({', '.join(KERNEL_BRANCH_REMOTES)}) are set up in "
                    f"{self.config.linux_dir}. Run: mnt-build dev-kernel add-remotes"
                )
            for remote in remotes:
                if self._fetch_mnt_linux_branch(remote, branch):
                    self.run_command(
                        ['git', 'switch', '--create', branch, '--track', f'{remote}/{branch}'],
                        cwd=self.config.linux_dir
                    )
                    self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Checked out {branch} from {remote}")
                    return
            raise BuildError(self._missing_mnt_linux_branch_message(branch, remotes))

        checkout_result = self.run_command(
            ['git', 'checkout', branch], cwd=self.config.linux_dir, check=False
        )
        if checkout_result.returncode != 0:
            raise BuildError(
                f"Could not switch to {branch} in {self.config.linux_dir}. "
                f"git said:\n{checkout_result.stderr}"
            )

        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Checked out {branch}")

    def _fetch_mnt_linux_branch(self, remote: str, branch: str) -> bool:
        """Fetch branch from remote. True if refs/remotes/{remote}/{branch} is usable."""
        self.logger.info(f"No local branch {branch}, checking {remote}...")
        cmd = ['git', 'fetch', remote, f'+refs/heads/{branch}:refs/remotes/{remote}/{branch}']
        # Not run_command: needs a timeout and must never stop for a password prompt.
        self.logger.debug(f"$ {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd, cwd=self.config.linux_dir, capture_output=True, text=True,
                timeout=REMOTE_FETCH_TIMEOUT,
                # LC_ALL=C: the "no such branch" check below matches git's English message.
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C',
                     'GIT_SSH_COMMAND': os.environ.get('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes')},
            )
            if result.returncode == 0:
                return True
            self.logger.debug(f"stderr: {result.stderr.strip()}")
            # "couldn't find remote ref" means the remote answered and has no such branch.
            reachable = "couldn't find remote ref" in result.stderr
        except subprocess.TimeoutExpired:
            self.logger.debug(f"No answer from {remote} in {REMOTE_FETCH_TIMEOUT}s")
            reachable = False

        if reachable:
            self.logger.info(f"{remote} has no {branch}")
            return False

        fetched_before = self.run_command(
            ['git', 'rev-parse', '--verify', '--quiet', f'refs/remotes/{remote}/{branch}'],
            cwd=self.config.linux_dir, check=False, log_cmd=False
        ).returncode == 0
        if fetched_before:
            self.logger.warning(
                f"Could not reach {remote}. Using the copy of {branch} fetched earlier, "
                "which may be out of date."
            )
            return True
        self.logger.warning(f"Could not reach {remote}, and {branch} was never fetched from it")
        return False

    def _missing_mnt_linux_branch_message(self, branch: str, remotes: List[str]) -> str:
        version_parts = self.config.version.split('.')
        series = f"mnt-v{version_parts[0]}.{version_parts[1]}."
        refs = self.run_command(
            ['git', 'for-each-ref', '--format=%(refname:short)', '--sort=-version:refname',
             f'refs/heads/{series}*', *(f'refs/remotes/{r}/{series}*' for r in remotes)],
            cwd=self.config.linux_dir, log_cmd=False
        ).stdout.split()

        message = (
            f"No {branch} branch locally or on {' or '.join(remotes)}. "
            f"mnt-build does not rebase the MNT patch stack as part of a build.\n"
        )
        if refs:
            message += (
                f"Branches known here for this series: {', '.join(refs)}\n"
                f"Rebase the newest one onto v{self.config.version}, then build again:\n"
                f"  mnt-build dev-kernel rebase --kversion {self.config.version}\n"
            )
        else:
            message += (
                f"No {series}x branch is known here either. Run "
                "'mnt-build dev-kernel fetch', then build again. "
            )
        message += (
            "Or pass the --kversion of a branch that exists."
        )
        return message

    def prepare_kernel_source(self, skip_git_operations: bool = False):
        """Get the kernel checkout onto the commit to build.

        Normally that is branch mnt-v{version}. With skip_git_operations the
        checkout is built as it stands, e.g. a submodule pinned by CI.
        """
        if skip_git_operations:
            if not (self.config.linux_dir / ".git").exists():
                raise BuildError(f"Not a git repository: {self.config.linux_dir}")
            self.require_clean_kernel_tree()
            head = self.run_command(
                ['git', 'log', '-1', '--format=%h %s'], cwd=self.config.linux_dir, log_cmd=False
            ).stdout.strip()
            self.logger.info(f"Skipping git checkout. Building what is checked out: {head}")
        else:
            self.checkout_mnt_linux_branch()

        result = self.run_command(
            ['make', *self._make_kernel_vars(), 'kernelversion'],
            cwd=self.config.linux_dir, log_cmd=False
        )
        found = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""

        def padded(version: str) -> Optional[tuple]:
            key = version_key(version)
            return key and (key + (0,))[:3]

        if padded(found) != padded(self.config.version):
            raise BuildError(
                f"{self.config.linux_dir} is kernel {found or '(unknown)'}, but --kversion is "
                f"{self.config.version}. Check out the matching commit or pass the right --kversion."
            )

    def setup_custom_dts_files(self):
        """Copy not-yet-committed DTS files from xtra-dtbs/ and update vendor Makefiles.

        Board DTS files that MNT ships are commits in the kernel branch already.
        """
        if not self._uses_dtbs():
            self.logger.info(f"Skipping custom DTS setup for ARCH={self.arch}")
            return

        # List of (source_path, name, vendor, config_sym)
        all_dts: list[tuple] = []

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
            if all_dts:
                self.logger.info(f"Found {len(all_dts)} extra DTS file(s) in {xtra_dir}")

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

        self._record_created_files()

    # ------------------------------------------------------------------
    # Config management
    # ------------------------------------------------------------------

    def update_config_with_olddefconfig(self):
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
            skip_git_operations: If True, do not switch branches. Build the
                                  kernel checkout as it stands.
            run_olddefconfig: If True, update config using olddefconfig before building.
        """
        self.logger.info(f"Building kernel {self.config.version}...")
        start_time = datetime.now()

        self.log_phase("Source Prep")
        self.prepare_kernel_source(skip_git_operations)

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
            self.update_config_with_olddefconfig()
        else:
            self.logger.info("Copying kernel config...")
            shutil.copy2(self.config.config_file, self.config.linux_dir / '.config')

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
