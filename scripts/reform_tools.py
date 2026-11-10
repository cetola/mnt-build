#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Check out the pinned reform-tools release in the build directory."""
import argparse
import subprocess
import sys
from pathlib import Path
from typing import Callable

from config import (REFORM_TOOLS_DIR, REFORM_TOOLS_TAG, REMOTES_FILE,
                    load_reform_tools_url)
from errors import BuildError


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def ensure_reform_tools(build_dir: Path, log: Callable[[str], None] = print) -> str:
    """Put reform-tools at REFORM_TOOLS_TAG, cloning it first if missing.

    Returns the commit SHA that is checked out.
    """
    tag = REFORM_TOOLS_TAG
    repo = build_dir / REFORM_TOOLS_DIR

    if not (repo / ".git").exists():
        if repo.exists() and any(repo.iterdir()):
            raise BuildError(
                f"{repo} is not empty and not a git repository. Move it aside "
                "and mnt-build will clone reform-tools there."
            )
        try:
            url = load_reform_tools_url(build_dir / REMOTES_FILE)
        except ValueError as e:
            raise BuildError(str(e))
        log(f"No reform-tools at {repo}. Cloning {tag} from {url}...")
        result = _git(build_dir, "clone", "--depth", "1", "--branch", tag,
                      "--origin", "origin", url, str(repo))
        if result.returncode != 0:
            raise BuildError(f"Could not clone {tag} of {url} into {repo}. git said:\n{result.stderr}")
    else:
        wanted = f"refs/tags/{tag}^{{commit}}"
        if _git(repo, "rev-parse", "--verify", "--quiet", wanted).returncode != 0:
            log(f"reform-tools has no tag {tag}. Fetching it from origin...")
            result = _git(repo, "fetch", "origin", "tag", tag)
            if result.returncode != 0:
                raise BuildError(f"Could not fetch tag {tag} in {repo}. git said:\n{result.stderr}")
        target = _git(repo, "rev-parse", "--verify", wanted).stdout.strip()
        if not target:
            raise BuildError(f"reform-tools has no tag {tag} in {repo}.")
        if _git(repo, "rev-parse", "HEAD").stdout.strip() != target:
            if _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip():
                raise BuildError(
                    f"{repo} has uncommitted changes and is not at {tag}. Commit or "
                    "discard them, or build with --skip-git-ops to use it as it is."
                )
            result = _git(repo, "switch", "--detach", tag)
            if result.returncode != 0:
                raise BuildError(f"Could not check out {tag} in {repo}. git said:\n{result.stderr}")

    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["ensure"])
    parser.add_argument("--build-dir", type=Path,
                        default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    try:
        ensure_reform_tools(args.build_dir.resolve(), lambda m: print(m, file=sys.stderr))
    except BuildError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
