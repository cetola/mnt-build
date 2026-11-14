# SPDX-License-Identifier: MIT
from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ORIGIN_BRANCH = "main"
UPSTREAM_BRANCH = "master"
RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")

# Paths only MNT will ever want, and paths that belong to a board. A commit
# touching anything else changes shared code. These are a guess to triage with.
MNT_ONLY = re.compile(
    r"^(build\.sh|\.gitlab-ci\.yml|README-mnt\.md|mnt-reform-defconfig|firmware/|rkbin|.*\.bin)")
BOARD = re.compile(
    r"^(arch/arm/boards/(mnt|.*reform|Makefile|Kconfig)|arch/arm/dts/(.*(mnt|reform)|Makefile)"
    r"|arch/arm/mach-[^/]+/Kconfig|images/|Documentation/boards/)")

CATEGORIES = ("MNT-only", "board", "shared code")


@dataclass
class Item:
    source: str
    ref: str
    subject: str
    category: str
    patch_id: str
    upstream: str = "no"
    in_origin: bool = False


def categorize(files: list[str]) -> str:
    if all(MNT_ONLY.match(f) for f in files):
        return "MNT-only"
    if all(MNT_ONLY.match(f) or BOARD.match(f) for f in files):
        return "board"
    return "shared code"


class BareboxStatus:
    """How far MNT's barebox is from upstream, and what is left to upstream."""

    def __init__(self, checkout: Path, patches_dir: Path, origin: str, upstream: str):
        self.checkout = checkout
        self.patches_dir = patches_dir
        self.origin_ref = f"{origin}/{ORIGIN_BRANCH}"
        self.upstream_ref = f"{upstream}/{UPSTREAM_BRANCH}"

    def _git(self, *args: str, input: str | None = None, check: bool = True) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.checkout), *args],
            capture_output=True, text=True, input=input, check=check,
        )
        return result.stdout.strip()

    def _patch_ids(self, rev_range: str) -> dict[str, str]:
        diffs = self._git("log", "--no-merges", "-p", rev_range)
        output = self._git("patch-id", "--stable", input=diffs + "\n")
        return {line.split()[0]: line.split()[1] for line in output.splitlines() if line.strip()}

    def _origin_items(self) -> list[Item]:
        rev_range = f"{self.upstream_ref}..{self.origin_ref}"
        ids = {commit: pid for pid, commit in self._patch_ids(rev_range).items()}
        log = self._git("log", "--no-merges", "--reverse", "--name-only",
                        "--format=%x00%H%x00%s", rev_range)
        parts = log.split("\0")
        items = []
        for i in range(1, len(parts) - 1, 2):
            commit, (subject, _, files) = parts[i], parts[i + 1].partition("\n")
            items.append(Item("origin", commit[:10], subject,
                              categorize([f for f in files.split("\n") if f]),
                              ids.get(commit, "")))
        return items

    def _xtra_items(self) -> list[Item]:
        items = []
        for path in sorted(self.patches_dir.glob("*.patch")) if self.patches_dir.is_dir() else []:
            text = path.read_text(errors="replace")
            subject = re.search(r"^Subject: (?:\[[^\]]*\]\s*)?(.*(?:\n[ \t].*)*)", text, re.M)
            files = re.findall(r"^diff --git a/\S+ b/(\S+)", text, re.M)
            pid = self._git("patch-id", "--stable", input=text).split()
            items.append(Item("xtra", path.name,
                              " ".join(subject.group(1).split()) if subject else path.name,
                              categorize(files), pid[0] if pid else ""))
        return items

    def collect(self) -> tuple[list[Item], dict]:
        base = self._git("merge-base", self.origin_ref, self.upstream_ref)
        up_range = f"{base}..{self.upstream_ref}"
        upstream_ids = self._patch_ids(up_range)
        upstream_subjects = set(self._git("log", "--no-merges", "--format=%s", up_range).splitlines())

        origin_items = self._origin_items()
        origin_ids = {i.patch_id for i in origin_items if i.patch_id}
        items = origin_items
        duplicates = 0
        for item in self._xtra_items():
            if item.patch_id and item.patch_id in origin_ids:
                duplicates += 1
                continue
            items.append(item)
        for item in items:
            if item.patch_id in upstream_ids:
                item.upstream = "yes"
            elif item.subject in upstream_subjects:
                item.upstream = "subject"

        releases = [t for t in self._git("tag", "--list", "--sort=-v:refname", "v*").splitlines()
                    if RELEASE_TAG.match(t)]
        info = {
            "base": self._git("describe", "--tags", "--match", "v*", base, check=False) or base[:10],
            "behind": self._git("rev-list", "--count", up_range),
            "release": releases[0] if releases else "none",
            "duplicates": duplicates,
        }
        return items, info

    def _last_fetch(self) -> str:
        fetch_head = self.checkout / ".git" / "FETCH_HEAD"
        if not fetch_head.is_file():
            return "never"
        days = int((time.time() - fetch_head.stat().st_mtime) // 86400)
        return "today" if days == 0 else f"{days} day(s) ago"

    def show(self, pin: str | None = None, verbose: bool = False) -> int:
        for ref in (self.origin_ref, self.upstream_ref):
            if self._git("rev-parse", "--verify", "-q", ref, check=False) == "":
                print(f"[barebox] ERROR: {ref} not found. "
                      "Run: mnt-build barebox add-remotes, then mnt-build barebox fetch",
                      file=sys.stderr)
                return 1

        items, info = self.collect()
        print(f"Common base   {info['base']}")
        print(f"origin        {self.origin_ref}, {sum(i.source == 'origin' for i in items)} "
              "commits not in upstream")
        print(f"upstream      {self.upstream_ref}, {info['behind']} commits past the common base, "
              f"latest release {info['release']}")
        print(f"Last fetch    {self._last_fetch()}")
        if pin:
            ahead = self._git("rev-list", "--count", f"{pin}..{self.origin_ref}", check=False)
            print(f"Pinned SHA    {pin[:10]}, {self.origin_ref} is {ahead or '?'} commits ahead")

        print()
        header = f"{'':<13}{'total':>6}{'upstream':>10}{'same subject':>14}{'not upstream':>14}"
        print(header)
        for category in CATEGORIES:
            rows = [i for i in items if i.category == category]
            print(f"{category:<13}{len(rows):>6}"
                  f"{sum(i.upstream == 'yes' for i in rows):>10}"
                  f"{sum(i.upstream == 'subject' for i in rows):>14}"
                  f"{sum(i.upstream == 'no' for i in rows):>14}")
        xtra = [i for i in items if i.source == "xtra"]
        total_xtra = len(xtra) + info["duplicates"]
        print(f"\nxtra-patches: {total_xtra}, {info['duplicates']} already in {self.origin_ref} "
              f"(counted once, as origin commits)")

        if verbose:
            print()
            for category in CATEGORIES:
                for item in (i for i in items if i.category == category):
                    mark = {"yes": "upstream", "subject": "subject ", "no": "        "}[item.upstream]
                    print(f"{category:<12} {mark}  {item.source:<6} {item.ref[:24]:<24} {item.subject}")
        return 0
