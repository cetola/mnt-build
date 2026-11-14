# SPDX-License-Identifier: MIT
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from config import GitRemote, load_remotes, normalize_git_url
from errors import BuildError
from logging_setup import Colors


class GitRemotes:
    """The remote marked "is-origin" is "origin" in the checkout, whatever it is listed as."""

    def __init__(self, repo_dir: Path, remotes_file: Path, checkout: str,
                 run: Callable, logger: logging.Logger):
        self.repo_dir = repo_dir
        self.remotes_file = remotes_file
        self.checkout = checkout
        self.run = run
        self.logger = logger

    def load(self) -> Tuple[List[GitRemote], Dict[str, str]]:
        if not (self.repo_dir / ".git").exists():
            raise BuildError(f"Not a git repository: {self.repo_dir}")
        if not self.remotes_file.is_file():
            raise BuildError(f"Remotes file not found: {self.remotes_file}")
        try:
            wanted = load_remotes(self.remotes_file, self.checkout)
        except ValueError as e:
            raise BuildError(str(e))

        existing = {}
        for name in self.run(['git', 'remote'], cwd=self.repo_dir, log_cmd=False).stdout.split():
            existing[name] = self.run(
                ['git', 'remote', 'get-url', name], cwd=self.repo_dir, log_cmd=False
            ).stdout.strip()
        return wanted, existing

    def check_origin(self):
        wanted, existing = self.load()
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

    def resolve(self, listed: str) -> Optional[str]:
        wanted, existing = self.load()
        url = {r.name: r.url for r in wanted}.get(listed)
        if url is None:
            return None
        if listed in existing:
            return listed
        return next(
            (n for n, u in existing.items() if normalize_git_url(u) == normalize_git_url(url)),
            None
        )

    def origin(self) -> Optional[str]:
        wanted, _ = self.load()
        listed = next((r.name for r in wanted if r.is_origin), None)
        return self.resolve(listed) if listed else None

    def ensure(self):
        wanted, existing = self.load()

        added = 0
        for listed in wanted:
            url, push_url = listed.url, listed.push_url
            name = "origin" if listed.is_origin else listed.name
            target = name
            if name in existing:
                if normalize_git_url(existing[name]) != normalize_git_url(url):
                    raise BuildError(
                        f"Remote '{name}' in {self.repo_dir} points at {existing[name]}, "
                        f"but {self.remotes_file.name} expects {url}. Fix one of them by hand."
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
                    self.run(['git', 'remote', 'add', name, url], cwd=self.repo_dir)
                    existing[name] = url
                    added += 1
                    self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} {name}: added ({url})")

            if push_url:
                current_push = self.run(
                    ['git', 'remote', 'get-url', '--push', target], cwd=self.repo_dir, log_cmd=False
                ).stdout.strip()
                if current_push != push_url:
                    self.run(['git', 'remote', 'set-url', '--push', target, push_url],
                             cwd=self.repo_dir)
                    self.logger.info(f"  {target}: push URL set to {push_url}")

        self.logger.info(f"Remotes added: {added}")

    def fetch(self, args_for: Optional[Callable[[str], List[str]]] = None):
        _, existing = self.load()
        failed = []
        for name in existing:
            self.logger.info(f"Fetching '{name}'...")
            args = args_for(name) if args_for else [name]
            result = self.run(
                ['git', 'fetch', '--no-progress', *args],
                cwd=self.repo_dir, check=False, stream_output=True
            )
            if result.returncode != 0:
                failed.append(name)
                self.logger.error(f"Fetch from '{name}' failed")
            else:
                self.logger.info(f"{Colors.GREEN}✓{Colors.RESET} {name}: fetched")

        if failed:
            raise BuildError(f"Could not fetch from: {', '.join(failed)}")
