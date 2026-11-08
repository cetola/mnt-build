# SPDX-License-Identifier: MIT
import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Sequence

from config import (
    BuildConfig, DEFAULT_KERNEL_VERSION, DEFAULT_LOCALVERSION_NAME,
    DEFAULT_LOCALVERSION_REV, MNT_BASE_TRAILER, MNT_KERNEL_REMOTE,
    REBASED_FROM_TRAILER, REMOTES_FILE, STABLE_KERNEL_REMOTE,
    load_kernel_remotes, normalize_git_url, parse_kernel_tag, version_key,
)
from errors import BuildError
from logging_setup import Colors, Spinner
from runner import CommandRunner

# Seconds to wait for a remote to answer a status check before calling it unreachable.
REMOTE_STATUS_TIMEOUT = 10
# Seconds to wait for a single-branch fetch before giving up on that remote.
REMOTE_FETCH_TIMEOUT = 600


def kversion_flag(version: str, localversion_rev: int = DEFAULT_LOCALVERSION_REV) -> str:
    flags = "" if version == DEFAULT_KERNEL_VERSION else f" --kversion {version}"
    if localversion_rev != DEFAULT_LOCALVERSION_REV:
        flags += f" --localversion-rev {localversion_rev}"
    return flags



class KernelRepo:
    """The mnt-linux git checkout: remotes, tags, release status, rebase and tag."""

    def __init__(self, config: BuildConfig, logger: logging.Logger,
                 runner: Optional[CommandRunner] = None):
        self.config = config
        self.logger = logger
        self.run_command = (runner or CommandRunner(config, logger)).run
        # git ls-remote answers, keyed by (remote, args). One status run asks
        # each remote once, however many checks need the answer.
        self._ls_remote_cache: dict = {}

    def clean(self):
        """Discard uncommitted changes and untracked files in the kernel checkout.

        Leaves commits, branches and tags alone. Ignored files (build outputs,
        .config) stay too.
        """
        linux_dir = self.config.linux_dir
        if not (linux_dir / ".git").exists():
            raise BuildError(f"Not a git repository: {linux_dir}")
        if self.rebase_in_progress():
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
        """Return (wanted, existing): REMOTES_FILE entries and the checkout's remotes."""
        linux_dir = self.config.linux_dir
        if not (linux_dir / ".git").exists():
            raise BuildError(f"Not a git repository: {linux_dir}")

        remotes_file = self.config.build_dir / REMOTES_FILE
        if not remotes_file.is_file():
            raise BuildError(f"Remotes file not found: {remotes_file}")
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

    def check_origin_remote(self):
        """Warn unless the checkout's remote 'origin' is the repo marked is-origin."""
        wanted, existing = self._load_kernel_remotes()
        wanted_origin = next((r for r in wanted if r.is_origin), None)
        if wanted_origin is None:
            return
        origin = existing.get('origin')
        if origin is not None and normalize_git_url(origin) == normalize_git_url(wanted_origin.url):
            return

        self.logger.warning(
            f"origin is not {wanted_origin.name} ({wanted_origin.url}). "
            f"Current origin is: {origin or 'not set'}"
        )

    def _ls_remote(self, name: str, *args: str) -> Optional[dict]:
        """Return {ref: sha} from git ls-remote, or None if the remote did not answer."""
        if (name, args) not in self._ls_remote_cache:
            self._ls_remote_cache[(name, args)] = self._run_ls_remote(name, *args)
        return self._ls_remote_cache[(name, args)]

    def _stable_tag_query(self) -> tuple:
        return ('--tags', '--refs', f'v{self._series()}', f'v{self._series()}.*')

    def _prefetch_remote_refs(self, remotes: List[str]):
        # Asked one after another, an unreachable remote alone would cost
        # REMOTE_STATUS_TIMEOUT seconds.
        queries = [(name, ('--heads',)) for name in remotes]
        stable = self._resolve_kernel_remote(STABLE_KERNEL_REMOTE)
        if stable:
            queries.append((stable, self._stable_tag_query()))
        origin = self._origin_remote()
        if origin:
            queries.append((origin, self._origin_tag_query()))
        with ThreadPoolExecutor(max_workers=len(queries) or 1) as pool:
            answers = pool.map(lambda q: self._run_ls_remote(q[0], *q[1]), queries)
        self._ls_remote_cache.update(zip(queries, answers))

    def _run_ls_remote(self, name: str, *args: str) -> Optional[dict]:
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

    def show_remotes(self, offline: bool = False):
        wanted, existing = self._load_kernel_remotes()
        by_name = {r.name: r.url for r in wanted}
        by_repo = {normalize_git_url(r.url): r.name for r in wanted}

        if not offline:
            with Spinner("Checking remotes..."):
                self._prefetch_remote_refs(list(existing))

        rows = []
        covered = set()
        for name, url in existing.items():
            state = self._kernel_remote_state(name, offline)
            if name in by_name:
                covered.add(name)
                if normalize_git_url(by_name[name]) != normalize_git_url(url):
                    state += f", {Colors.RED}URL differs from {REMOTES_FILE}{Colors.RESET}"
            elif normalize_git_url(url) in by_repo:
                listed_as = by_repo[normalize_git_url(url)]
                covered.add(listed_as)
                state += f", listed as '{listed_as}'"
            else:
                state += f", not in {REMOTES_FILE}"
            rows.append((name, url, state))
        for r in wanted:
            if r.name not in covered:
                rows.append((r.name, r.url, f"{Colors.YELLOW}not added{Colors.RESET}"))

        name_w = max((len(r[0]) for r in rows), default=0)
        url_w = max((len(r[1]) for r in rows), default=0)
        for name, url, state in rows:
            self.logger.info(f"{name:<{name_w}}  {url:<{url_w}}  {state}")

        missing = sum(1 for r in wanted if r.name not in covered)
        if missing:
            self.logger.info(
                f"{missing} remote(s) from {REMOTES_FILE} not added. "
                "Run: mnt-build dev-kernel add-remotes"
            )

    def fetch_remotes(self):
        linux_dir = self.config.linux_dir
        _, existing = self._load_kernel_remotes()

        # The kernel tags point at commits no branch has, so a plain fetch
        # would not bring them.
        origin = self._origin_remote()
        failed = []
        for name in existing:
            self.logger.info(f"Fetching '{name}'...")
            tags = ['--tags'] if name == origin else []
            result = self.run_command(
                ['git', 'fetch', '--no-progress', *tags, name],
                cwd=linux_dir, check=False, stream_output=True
            )
            if result.returncode != 0:
                failed.append(name)
                self.logger.error(f"Fetch from '{name}' failed")
            else:
                self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} {name}: fetched")

        if failed:
            raise BuildError(f"Could not fetch from: {', '.join(failed)}")

    def ensure_remotes(self):
        linux_dir = self.config.linux_dir
        wanted, existing = self._load_kernel_remotes()
        remotes_file = self.config.build_dir / REMOTES_FILE

        added = 0
        for listed in wanted:
            url, push_url = listed.url, listed.push_url
            # The is-origin repo goes in as "origin", whatever it is listed as.
            name = "origin" if listed.is_origin else listed.name
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
        """Name in the checkout of a remote listed in REMOTES_FILE, if it is set up."""
        wanted, existing = self._load_kernel_remotes()
        url = {r.name: r.url for r in wanted}.get(listed)
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
        remotes = [self._resolve_kernel_remote(listed) for listed in self._kernel_branch_remote_names()]
        return list(dict.fromkeys(r for r in remotes if r))

    def _kernel_branch_remote_names(self) -> List[str]:
        """Names in REMOTES_FILE of the repos that carry mnt-v branches: the origin, then MNT's."""
        wanted, _ = self._load_kernel_remotes()
        names = [r.name for r in wanted if r.is_origin] + [MNT_KERNEL_REMOTE]
        return list(dict.fromkeys(names))

    def _origin_remote(self) -> Optional[str]:
        """Name in the checkout of the is-origin repo."""
        wanted, _ = self._load_kernel_remotes()
        listed = next((r.name for r in wanted if r.is_origin), None)
        return self._resolve_kernel_remote(listed) if listed else None

    def _ref_exists(self, ref: str) -> bool:
        return self._ref_sha(ref) is not None

    def _ref_sha(self, ref: str) -> Optional[str]:
        result = self.run_command(
            ['git', 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}'],
            cwd=self.config.linux_dir, check=False, log_cmd=False
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _series(self) -> str:
        match = re.match(r'(\d+)\.(\d+)', self.config.version)
        if not match:
            raise BuildError(f"--kversion {self.config.version} is not a kernel version like 7.2.10")
        return f"{match.group(1)}.{match.group(2)}"

    def _series_tag_patterns(self) -> List[str]:
        series = self._series()
        return [f'mnt-v{series}-*', f'mnt-v{series}.*']

    def _origin_tag_query(self) -> tuple:
        return ('--tags', '--refs', *self._series_tag_patterns())

    def _kernel_tags(self, names: Optional[set] = None) -> List[tuple]:
        """(version_key, rev, name), newest first. Local tags unless names are given."""
        if names is None:
            names = self.run_command(
                ['git', 'for-each-ref', '--format=%(refname:short)',
                 *(f'refs/tags/{p}' for p in self._series_tag_patterns())],
                cwd=self.config.linux_dir, log_cmd=False
            ).stdout.split()
        series = version_key(self._series())
        found = []
        for name in names:
            parsed = parse_kernel_tag(name)
            key = parsed and version_key(parsed[0])
            if key and key[:2] == series:
                found.append((key, parsed[1], name))
        found.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return found

    def _tags_for_version(self, version: str, names: Optional[set] = None) -> List[str]:
        """Local tags unless names are given, highest revision first."""
        if names is None:
            names = self.run_command(
                ['git', 'for-each-ref', '--format=%(refname:short)', f'refs/tags/mnt-v{version}-*'],
                cwd=self.config.linux_dir, log_cmd=False
            ).stdout.split()
        found = [(p[1], n) for n in names if (p := parse_kernel_tag(n)) and p[0] == version]
        return [n for _, n in sorted(found, reverse=True)]

    def _tag_trailer(self, tag: str, trailer: str) -> Optional[tuple]:
        """(ref name, sha) from a trailer in an annotated tag's message."""
        text = self.run_command(
            ['git', 'tag', '-l', '--format=%(contents)', tag],
            cwd=self.config.linux_dir, log_cmd=False
        ).stdout
        for line in text.splitlines():
            if line.startswith(f"{trailer}: "):
                parts = line[len(trailer) + 2:].split()
                if len(parts) == 2:
                    return parts[0], parts[1]
        return None

    def _newest_mnt_branch(self, offline: bool) -> Optional[tuple]:
        """(name, sha) of MNT's newest mnt-v branch of this series, or None."""
        mnt = self._resolve_kernel_remote(MNT_KERNEL_REMOTE)
        if mnt is None:
            return None
        branches = None
        if not offline:
            refs = self._ls_remote(mnt, '--heads')
            if refs is None:
                self.logger.warning(f"Could not reach {mnt}. Going by what was fetched earlier.")
            else:
                branches = {r[len('refs/heads/'):]: sha for r, sha in refs.items()}
        if branches is None:
            branches = {}
            lines = self.run_command(
                ['git', 'for-each-ref', '--format=%(refname) %(objectname)', f'refs/remotes/{mnt}/'],
                cwd=self.config.linux_dir, log_cmd=False
            ).stdout.splitlines()
            for line in lines:
                ref, sha = line.split(' ', 1)
                branches[ref[len(f'refs/remotes/{mnt}/'):]] = sha
        series = version_key(self._series())
        found = []
        for name, sha in branches.items():
            key = version_key(name[len('mnt-v'):]) if name.startswith('mnt-v') else None
            if key and key[:2] == series:
                found.append((key, name, sha))
        if not found:
            return None
        _, name, sha = max(found)
        return name, sha

    def _mnt_advice(self, tag: str, base: Optional[tuple], newest: tuple,
                    version: str, rev: int) -> str:
        name, sha = newest
        mnt = self._resolve_kernel_remote(MNT_KERNEL_REMOTE) or MNT_KERNEL_REMOTE
        if base is None:
            text = f"{tag} records no {MNT_BASE_TRAILER}, so it is unknown whether it has {name}.\n"
        else:
            text = (f"MNT has patches the tags may not have: {tag} carries {base[0]} "
                    f"({base[1][:12]}), MNT's newest is {name} ({sha[:12]}).\n")
            old, new = base[0][len('mnt-v'):], name[len('mnt-v'):]
            if version_key(old) and version_key(new):
                text += f"Compare them:\n  git range-diff v{old}..{base[1][:12]} v{new}..{mnt}/{name}\n"
        text += (
            f"To take MNT's patches, rebase from their branch and carry your own fixes over:\n"
            f"  mnt-build dev-kernel rebase{kversion_flag(version, rev)} --from {mnt}/{name}\n"
            f"If there is nothing to take, name {tag} with --from on the next rebase, and "
            f"record that you checked on the next tag:\n"
            f"  mnt-build dev-kernel tag --mnt-base {mnt}/{name}"
        )
        return text

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
        stable = self._resolve_kernel_remote(STABLE_KERNEL_REMOTE)
        tags = None
        if not offline and stable:
            refs = self._ls_remote(stable, *self._stable_tag_query())
            if refs is not None:
                tags = [ref[len('refs/tags/'):] for ref in refs]
        if tags is None:
            tags = self.run_command(
                ['git', 'tag', '-l', f'v{series}', f'v{series}.*'],
                cwd=self.config.linux_dir, log_cmd=False
            ).stdout.split()
        versions = [(key, t[1:]) for t in tags if (key := version_key(t[1:])) is not None]
        return max(versions)[1] if versions else None

    def show_versions(self, offline: bool = False):
        series = self._series()
        linux_dir = self.config.linux_dir

        latest = self._latest_stable_version(offline)
        if latest is None:
            self.logger.warning(
                f"No v{series} release tags known. An -rc series has none until its "
                "release; otherwise run: mnt-build dev-kernel fetch"
            )
            return
        source = "tags fetched earlier, not checked" if offline else "stable remote"
        self.logger.info(f"Latest stable {series}: v{latest} ({source})")

        origin = self._origin_remote()
        local_tags = {name for _, _, name in self._kernel_tags()}
        origin_tags = None
        if origin and not offline:
            refs = self._ls_remote(origin, *self._origin_tag_query())
            if refs is None:
                self.logger.warning(f"Could not reach {origin}. Going by the tags here.")
            else:
                origin_tags = {ref[len('refs/tags/'):] for ref in refs}
        tags = self._kernel_tags(local_tags | (origin_tags or set()))

        def where(name: str) -> str:
            places = ["local"] if name in local_tags else []
            if origin_tags and name in origin_tags:
                places.append(origin)
            return ', '.join(places)

        newest = self._newest_mnt_branch(offline)
        mnt = self._resolve_kernel_remote(MNT_KERNEL_REMOTE) or MNT_KERNEL_REMOTE
        latest_tags = [t for t in tags if t[0] == version_key(latest)]
        next_rev = latest_tags[0][1] + 1 if latest_tags else DEFAULT_LOCALVERSION_REV
        if latest_tags:
            _, rev, name = latest_tags[0]
            self.logger.info(
                f"{Colors.GREEN}✓{Colors.RESET} v{latest} is tagged: {name} ({where(name)}). "
                f"Build with: mnt-build build{kversion_flag(latest, rev)}"
            )
        else:
            self.logger.info(f"{Colors.YELLOW}v{latest} is not tagged yet{Colors.RESET}")
            newest_key = None
            if tags:
                newest_key = tags[0][0]
                self.logger.info(f"Newest {series} tag: {tags[0][2]} ({where(tags[0][2])})")
            else:
                self.logger.info(f"No mnt-v{series} tags known here.")
                branches = self._mnt_linux_branches(self._kernel_branch_remotes())
                if branches:
                    newest_key, ref = branches[0]
                    known = '.'.join(map(str, newest_key))
                    base = f"{mnt}/{newest[0]}" if newest else f"{mnt}/mnt-v{series}.N"
                    self.logger.info(f"Newest {series} branch known here: {ref}")
                    self.logger.info(
                        f"To start the tags from it, check it out and tag it, naming the MNT "
                        f"branch it carries:\n"
                        f"  git -C {linux_dir} switch --detach {ref}\n"
                        f"  mnt-build dev-kernel tag{kversion_flag(known)} --mnt-base {base}"
                    )
            if newest_key is None or newest_key < version_key(latest):
                self.logger.info(f"Rebase with: mnt-build dev-kernel rebase{kversion_flag(latest)}")
            else:
                self.logger.info(
                    f"Nothing here is older than v{latest} to rebase. If a newer release is out, "
                    "fetch its tag: mnt-build dev-kernel fetch"
                )
        if origin_tags is not None:
            for _, _, name in tags:
                if name not in origin_tags:
                    self.logger.info(
                        f"{Colors.YELLOW}{name} is not on {origin} yet.{Colors.RESET} "
                        f"Publish it with: git -C {linux_dir} push {origin} {name}"
                    )

        if newest is None:
            self.logger.info(f"No MNT mnt-v{series}.x branch known.")
            return
        self.logger.info(f"Newest MNT {series} branch: {newest[0]} ({newest[1][:12]})")
        if not tags:
            return
        tag = tags[0][2]
        if tag not in local_tags:
            self.logger.info(f"Fetch {tag} to compare it with MNT's: mnt-build dev-kernel fetch")
            return
        base = self._tag_trailer(tag, MNT_BASE_TRAILER)
        if base == newest:
            self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} {tag} carries it")
        else:
            for line in self._mnt_advice(tag, base, newest, latest, next_rev).splitlines():
                self.logger.info(f"{Colors.YELLOW}{line}{Colors.RESET}" if not line.startswith(' ') else line)

    def _ensure_stable_tag(self, tag: str):
        """Make sure a stable release tag is in the checkout, fetching it if needed."""
        if self._ref_exists(f'refs/tags/{tag}'):
            return
        stable = self._resolve_kernel_remote(STABLE_KERNEL_REMOTE)
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

    def rebase_in_progress(self) -> bool:
        for name in ('rebase-merge', 'rebase-apply'):
            path = self.run_command(
                ['git', 'rev-parse', '--git-path', name],
                cwd=self.config.linux_dir, log_cmd=False
            ).stdout.strip()
            if (self.config.linux_dir / path).exists():
                return True
        return False

    def _require_git_identity(self, action: str):
        # A rebase writes commits and a tag records its tagger. Without an
        # identity git stops partway.
        if self.run_command(
            ['git', 'var', 'GIT_COMMITTER_IDENT'], cwd=self.config.linux_dir, check=False, log_cmd=False
        ).returncode != 0:
            raise BuildError(
                f"git has no committer identity here, so it cannot {action}. Set one with:\n"
                '  git config --global user.name "Your Name"\n'
                '  git config --global user.email "you@example.com"'
            )

    def rebase(self, source: Optional[str] = None):
        linux_dir = self.config.linux_dir
        if not (linux_dir / ".git").exists():
            raise BuildError(f"Not a git repository: {linux_dir}")

        version = self.config.version
        tag = f"v{version}"
        branch = f"mnt-v{version}"
        flag = kversion_flag(version, self.config.localversion_rev)

        self._require_git_identity("rebase")
        self.require_clean()

        tagged = self._tags_for_version(version)
        if tagged and parse_kernel_tag(tagged[0])[1] >= self.config.localversion_rev:
            rev = parse_kernel_tag(tagged[0])[1]
            raise BuildError(
                f"v{version} is already tagged ({', '.join(tagged)}). Nothing to rebase. "
                f"Build it with: mnt-build build{kversion_flag(version, rev)}\n"
                f"To make a new stack for it, rebase with --localversion-rev {rev + 1}, or "
                f"commit on top of the tag and tag the result with --localversion-rev {rev + 1}."
            )
        remotes = self._kernel_branch_remotes()
        if self._ref_exists(f'refs/heads/{branch}'):
            raise BuildError(
                f"Local branch {branch} already exists. If it is the stack you want, build it "
                f"with 'mnt-build build{flag} --skip-git-ops', then tag it with: "
                f"mnt-build dev-kernel tag{flag}\n"
                f"To start over, delete it first: git -C {linux_dir} branch -D {branch}"
            )

        tags = self._kernel_tags()
        newest_mnt = self._newest_mnt_branch(offline=True)
        unabsorbed = None
        if tags and newest_mnt:
            base = self._tag_trailer(tags[0][2], MNT_BASE_TRAILER)
            if base != newest_mnt:
                unabsorbed = self._mnt_advice(
                    tags[0][2], base, newest_mnt, version, self.config.localversion_rev)

        if source is None:
            target = version_key(version)
            if target is None:
                raise BuildError(f"{version} is not a release. Name the stack to rebase with --from.")
            if unabsorbed:
                raise BuildError(unabsorbed)
            # A newer stack would be rebased backwards.
            candidates = [((key, 0, -rev), name) for key, rev, name in tags if key < target]
            candidates += [
                ((key, 1, rank), ref)
                for rank, (key, ref) in enumerate(self._mnt_linux_branches(remotes)) if key < target
            ]
            if not candidates:
                raise BuildError(
                    f"No mnt-v{self._series()}.x tag or branch older than {version} is known in "
                    f"{linux_dir} to rebase from. "
                    "Run 'mnt-build dev-kernel fetch', or name one with --from."
                )
            candidates.sort(key=lambda c: (tuple(-n for n in c[0][0]), c[0][1], c[0][2]))
            source = candidates[0][1]
        elif not self._ref_exists(source):
            raise BuildError(f"--from {source}: no such tag, branch or commit in {linux_dir}")
        elif unabsorbed:
            self.logger.warning(unabsorbed.splitlines()[0])

        match = re.search(
            rf'mnt-v(\d+\.\d+(?:\.\d+)?)(?:-{DEFAULT_LOCALVERSION_NAME}\d+)?$', source
        )
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
            raise BuildError(
                f"{source} is already based on {tag}. Nothing to rebase. Check it out, "
                f"build it with 'mnt-build build{flag} --skip-git-ops', then tag it with: "
                f"mnt-build dev-kernel tag{flag}"
            )

        self._ensure_stable_tag(tag)

        source_sha = self._ref_sha(source)
        mnt = self._resolve_kernel_remote(MNT_KERNEL_REMOTE)
        if mnt and source.startswith(f'{mnt}/'):
            mnt_base = (source[len(mnt) + 1:], source_sha)
        elif self._ref_exists(f'refs/tags/{source.removeprefix("refs/tags/")}'):
            mnt_base = self._tag_trailer(source.removeprefix('refs/tags/'), MNT_BASE_TRAILER)
        else:
            mnt_base = None

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
        # Kept with the branch for 'dev-kernel tag', which writes them into the tag.
        self.run_command(
            ['git', 'config', f'branch.{branch}.rebasedFrom', f'{source} {source_sha}'], cwd=linux_dir
        )
        if mnt_base:
            self.run_command(
                ['git', 'config', f'branch.{branch}.mntBase', f'{mnt_base[0]} {mnt_base[1]}'],
                cwd=linux_dir
            )
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
        self.logger.info("The branch is local only. Nothing was pushed or tagged.")
        olddefconfig = "" if self.config.config_file.exists() else " --olddefconfig"
        self.logger.info(f"Next, build and test it: mnt-build build{flag} --skip-git-ops{olddefconfig}")
        self.logger.info(f"Then tag it: mnt-build dev-kernel tag{flag}")

    def tag(self, mnt_base: Optional[str] = None):
        linux_dir = self.config.linux_dir
        if not (linux_dir / ".git").exists():
            raise BuildError(f"Not a git repository: {linux_dir}")
        version = self.config.version
        tag = self.config.tag
        next_rev = f"--localversion-rev {self.config.localversion_rev + 1}"

        self._require_git_identity("tag")
        self.require_clean()

        if self._ref_exists(f'refs/tags/{tag}'):
            raise BuildError(
                f"{tag} already exists here. Tags are never moved. "
                f"Tag a changed stack as the next revision with {next_rev}."
            )
        origin = self._origin_remote()
        if origin:
            refs = self._run_ls_remote(origin, '--tags', '--refs', f'refs/tags/{tag}')
            if refs is None:
                self.logger.warning(f"Could not reach {origin} to check that {tag} is new there.")
            elif refs:
                raise BuildError(
                    f"{tag} already exists on {origin}. Tags are never moved. Fetch it with "
                    f"'mnt-build dev-kernel fetch', or tag a changed stack with {next_rev}."
                )
        self.check_kernel_version()

        result = self.run_command(
            ['git', 'symbolic-ref', '--short', '-q', 'HEAD'], cwd=linux_dir, check=False, log_cmd=False
        )
        branch = result.stdout.strip() if result.returncode == 0 else None

        def branch_config(key: str) -> Optional[str]:
            if branch is None:
                return None
            value = self.run_command(
                ['git', 'config', '--get', f'branch.{branch}.{key}'],
                cwd=linux_dir, check=False, log_cmd=False
            ).stdout.strip()
            return value or None

        base = None
        if mnt_base:
            sha = self._ref_sha(mnt_base)
            if sha is None:
                raise BuildError(f"--mnt-base {mnt_base}: no such branch or commit in {linux_dir}")
            name = mnt_base.removeprefix('refs/remotes/')
            mnt = self._resolve_kernel_remote(MNT_KERNEL_REMOTE)
            if mnt:
                name = name.removeprefix(f'{mnt}/')
            base = f"{name} {sha}"
        else:
            base = branch_config('mntBase')
        rebased_from = branch_config('rebasedFrom')
        if base is None:
            self.logger.warning(
                f"No MNT branch is recorded for this stack, so the status report cannot tell "
                f"whether MNT has newer patches. Name the one it carries with --mnt-base, "
                f"e.g. --mnt-base {MNT_KERNEL_REMOTE}/mnt-v{self._series()}.N"
            )

        trailers = []
        if base:
            trailers.append(f"{MNT_BASE_TRAILER}: {base}")
        if rebased_from:
            trailers.append(f"{REBASED_FROM_TRAILER}: {rebased_from}")
        message = f"MNT Reform patch stack on Linux v{version}"
        if trailers:
            message += "\n\n" + "\n".join(trailers)
        self.run_command(['git', 'tag', '--annotate', '--file=-', tag], cwd=linux_dir,
                         input_data=message)

        head = self.run_command(
            ['git', 'log', '-1', '--format=%h %s', tag], cwd=linux_dir, log_cmd=False
        ).stdout.strip()
        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Tagged {head} as {tag}")
        for line in trailers:
            self.logger.info(f"  {line}")
        self.logger.info("The tag is local only. Nothing was pushed.")
        self.logger.info(f"Publish it with: git -C {linux_dir} push {origin or 'origin'} {tag}")

    def untracked_files(self) -> set:
        return set(self.run_command(
            ['git', 'ls-files', '--others', '--exclude-standard', '-z'],
            cwd=self.config.linux_dir, log_cmd=False
        ).stdout.split('\0')) - {''}

    def require_clean(self):
        """Refuse to build on a persistent kernel checkout with uncommitted changes.

        xtra-patches and custom DTS files are applied to the working tree and
        taken back out by restore_kernel_tree(). That is only safe if nothing
        else was modified first.
        """
        if self.rebase_in_progress():
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

    def ensure_checkout(self, shallow: bool = False):
        # A shallow clone can build but not dev-kernel rebase.
        linux_dir = self.config.linux_dir
        if (linux_dir / ".git").exists():
            return
        if linux_dir.exists() and any(linux_dir.iterdir()):
            raise BuildError(
                f"{linux_dir} is not empty and not a git repository. Move it aside "
                "and mnt-build will clone the kernel there."
            )

        remotes_file = self.config.build_dir / REMOTES_FILE
        if not remotes_file.is_file():
            raise BuildError(f"No kernel at {linux_dir}, and no {remotes_file} to clone it from.")
        try:
            origin = next((r for r in load_kernel_remotes(remotes_file) if r.is_origin), None)
        except ValueError as e:
            raise BuildError(str(e))
        if origin is None:
            raise BuildError(
                f"No kernel at {linux_dir}, and no remote in {remotes_file} "
                'has "is-origin": true to clone it from.'
            )

        tag = self.config.tag
        if shallow:
            found = self.run_command(['git', 'ls-remote', '--tags', origin.url, f'refs/tags/{tag}'],
                                     cwd=self.config.build_dir, check=False)
            if found.returncode == 0 and not found.stdout.strip():
                raise BuildError(
                    f"{origin.url} has no tag {tag}. Push the tag, or build a version "
                    "and revision that has one."
                )
            self.logger.info(f"No kernel at {linux_dir}. Cloning {tag} only from {origin.url}...")
            cmd = ['git', 'clone', '--depth', '1', '--branch', tag, '--origin', 'origin',
                   origin.url, str(linux_dir)]
        else:
            self.logger.info(f"No kernel at {linux_dir}. Cloning {origin.url}...")
            cmd = ['git', 'clone', '--no-checkout', '--origin', 'origin',
                   origin.url, str(linux_dir)]
        result = self.run_command(cmd, cwd=self.config.build_dir, check=False)
        if result.returncode != 0:
            detail = f" git said:\n{result.stderr}" if result.stderr else ""
            raise BuildError(f"Could not clone {origin.url} into {linux_dir}.{detail}")
        if origin.push_url:
            self.run_command(['git', 'remote', 'set-url', '--push', 'origin', origin.push_url],
                             cwd=linux_dir, log_cmd=False)

        if not shallow:
            if not self._ref_exists(f'refs/tags/{tag}'):
                raise BuildError(self._missing_kernel_tag_message('origin'))
            result = self.run_command(['git', 'switch', '--detach', tag], cwd=linux_dir, check=False)
            if result.returncode != 0:
                raise BuildError(f"Could not check out {tag} in {linux_dir}. git said:\n{result.stderr}")
        self.logger.info(
            f"{Colors.GREEN}✓{Colors.RESET} Cloned the kernel at {tag}. "
            "For the other remotes run: mnt-build dev-kernel add-remotes"
        )

    def checkout_tag(self):
        """Detach HEAD at the build tag. The caller checks the tree is clean first."""
        linux_dir = self.config.linux_dir
        if not (linux_dir / ".git").exists():
            raise BuildError(f"Not a git repository: {linux_dir}")

        tag = self.config.tag

        if not self._ref_exists(f'refs/tags/{tag}'):
            origin = self._origin_remote()
            if origin is None:
                raise BuildError(
                    f"No tag {tag} here, and the origin remote that carries the kernel tags "
                    f"is not set up in {linux_dir}. Run: mnt-build dev-kernel add-remotes"
                )
            self.logger.info(f"No local tag {tag}, checking {origin}...")
            found = self._fetch_ref(origin, f'refs/tags/{tag}', f'refs/tags/{tag}')
            if found is None:
                raise BuildError(
                    f"Could not reach {origin}, and tag {tag} is not here. "
                    "Try again when it is reachable, or check out a commit by hand "
                    "and build it with --skip-git-ops."
                )
            if not found:
                raise BuildError(self._missing_kernel_tag_message(origin))
            self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Fetched {tag} from {origin}")

        target = self._ref_sha(f'refs/tags/{tag}')
        if self._ref_sha('HEAD') == target:
            self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Already at {tag}")
            return

        current = self.run_command(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=linux_dir, log_cmd=False
        ).stdout.strip()
        self.logger.info(f"Switching from {current} to {tag} (from --kversion {self.config.version})...")
        result = self.run_command(['git', 'switch', '--detach', tag], cwd=linux_dir, check=False)
        if result.returncode != 0:
            raise BuildError(
                f"Could not switch to {tag} in {linux_dir}. git said:\n{result.stderr}"
            )
        self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} Checked out {tag} ({target[:12]})")

    def _fetch_ref(self, remote: str, src: str, dst: str) -> Optional[bool]:
        """Fetch one ref. True if fetched, False if the remote lacks it, None if unreachable."""
        cmd = ['git', 'fetch', '--no-tags', remote, f'+{src}:{dst}']
        # Not run_command: needs a timeout and must never stop for a password prompt.
        self.logger.debug(f"$ {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd, cwd=self.config.linux_dir, capture_output=True, text=True,
                timeout=REMOTE_FETCH_TIMEOUT,
                # LC_ALL=C: the "no such ref" check below matches git's English message.
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C',
                     'GIT_SSH_COMMAND': os.environ.get('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes')},
            )
        except subprocess.TimeoutExpired:
            self.logger.debug(f"No answer from {remote} in {REMOTE_FETCH_TIMEOUT}s")
            return None
        if result.returncode == 0:
            return True
        self.logger.debug(f"stderr: {result.stderr.strip()}")
        if "couldn't find remote ref" in result.stderr:
            self.logger.info(f"{remote} has no {src.removeprefix('refs/tags/')}")
            return False
        return None

    def _missing_kernel_tag_message(self, origin: str) -> str:
        version = self.config.version
        flag = kversion_flag(version, self.config.localversion_rev)
        names = set(self.run_command(
            ['git', 'for-each-ref', '--format=%(refname:short)', f'refs/tags/mnt-v{version}-*'],
            cwd=self.config.linux_dir, log_cmd=False
        ).stdout.split())
        refs = self._run_ls_remote(origin, '--tags', '--refs', f'mnt-v{version}-*')
        names |= {ref[len('refs/tags/'):] for ref in (refs or {})}
        others = self._tags_for_version(version, names)

        message = (
            f"No tag {self.config.tag} locally or on {origin}. "
            "mnt-build builds tagged kernels only.\n"
        )
        if others:
            rev = parse_kernel_tag(others[0])[1]
            message += (
                f"Tags for v{version}: {', '.join(others)}. Build the newest with:\n"
                f"  mnt-build build{kversion_flag(version, rev)}\n"
            )
        elif branch := next(
            (ref for key, ref in self._mnt_linux_branches(self._kernel_branch_remotes())
             if ref.endswith(f'mnt-v{version}')), None
        ):
            message += (
                f"Branch {branch} has a stack for this release. To build it as a tag, "
                "check it out, test it, then tag it:\n"
                f"  git -C {self.config.linux_dir} switch --detach {branch}\n"
                f"  mnt-build build{flag} --skip-git-ops\n"
                f"  mnt-build dev-kernel tag{flag} --mnt-base {MNT_KERNEL_REMOTE}/<the MNT branch it carries>\n"
            )
        else:
            message += (
                "To make it, rebase the patch stack, build and test it, then tag it:\n"
                f"  mnt-build dev-kernel rebase{flag}\n"
                f"  mnt-build build{flag} --skip-git-ops\n"
                f"  mnt-build dev-kernel tag{flag}\n"
            )
        message += "To build an untagged commit, check it out and pass --skip-git-ops."
        return message

    def check_kernel_version(self, make_vars: Sequence[str] = ()):
        result = self.run_command(
            ['make', *make_vars, 'kernelversion'],
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
