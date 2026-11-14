# SPDX-License-Identifier: MIT
from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
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
BOILERPLATE = ("Makefile", "Kconfig")  # file name prefixes: Makefile, Makefile.rockchip, Kconfig.debug


@dataclass
class Item:
    source: str
    ref: str
    subject: str
    category: str
    patch_id: str
    files: list[str]
    author: tuple[str, str]
    upstream: str = "no"
    in_origin: bool = False
    related: list[tuple[str, str]] = field(default_factory=list)
    touched: list[tuple[str, str, str]] = field(default_factory=list)


def categorize(files: list[str]) -> str:
    if all(MNT_ONLY.match(f) for f in files):
        return "MNT-only"
    if all(MNT_ONLY.match(f) or BOARD.match(f) for f in files):
        return "board"
    return "shared code"


def significant(files) -> set[str]:
    real = {f for f in files if not Path(f).name.startswith(BOILERPLATE)}
    return real or set(files)


def same_author(a: tuple[str, str], b: tuple[str, str]) -> bool:
    return a[0].lower() == b[0].lower() or a[1].lower() == b[1].lower()


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
        items = []
        for commit, author, subject, files in self._log_entries(
                "--reverse", rev_range):
            items.append(Item("origin", commit[:10], subject, categorize(files),
                              ids.get(commit, ""), files, author))
        return items

    def _log_entries(self, *args: str) -> list[tuple[str, tuple[str, str], str, list[str]]]:
        log = self._git("log", "--no-merges", "--name-only",
                        "--format=%x00%H%x00%an%x00%ae%x00%s", *args)
        parts = log.split("\0")
        entries = []
        for i in range(1, len(parts) - 3, 4):
            commit, name, email, tail = parts[i:i + 4]
            subject, _, files = tail.partition("\n")
            entries.append((commit, (name, email), subject, [f for f in files.split("\n") if f]))
        return entries

    def _xtra_items(self) -> list[Item]:
        items = []
        for path in sorted(self.patches_dir.glob("*.patch")) if self.patches_dir.is_dir() else []:
            text = path.read_text(errors="replace")
            subject = re.search(r"^Subject: (?:\[[^\]]*\]\s*)?(.*(?:\n[ \t].*)*)", text, re.M)
            files = re.findall(r"^diff --git a/\S+ b/(\S+)", text, re.M)
            pid = self._git("patch-id", "--stable", input=text).split()
            author = re.search(r"^From: (.*?)\s*<([^>]*)>", text, re.M)
            items.append(Item("xtra", path.name,
                              " ".join(subject.group(1).split()) if subject else path.name,
                              categorize(files), pid[0] if pid else "", files,
                              (author.group(1), author.group(2)) if author else ("", "")))
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
        upstream_commits = self._log_entries(up_range)
        for item in items:
            if item.patch_id in upstream_ids:
                item.upstream = "yes"
            elif item.subject in upstream_subjects:
                item.upstream = "subject"
            else:
                mine = significant(item.files)
                for commit, author, subject, files in upstream_commits:
                    if not mine & significant(files):
                        continue
                    if same_author(item.author, author):
                        item.related.append((commit[:10], subject))
                    else:
                        item.touched.append((commit[:10], author[0], subject))
                if item.related:
                    item.upstream = "related"
                elif item.touched:
                    item.upstream = "touched"

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
        header = f"{'':<13}{'total':>6}{'upstream':>10}{'same subject':>14}{'related':>9}{'TOUCHED':>9}{'not upstream':>14}"
        print(header)
        for category in CATEGORIES:
            rows = [i for i in items if i.category == category]
            print(f"{category:<13}{len(rows):>6}"
                  f"{sum(i.upstream == 'yes' for i in rows):>10}"
                  f"{sum(i.upstream == 'subject' for i in rows):>14}"
                  f"{sum(i.upstream == 'related' for i in rows):>9}"
                  f"{sum(i.upstream == 'touched' for i in rows):>9}"
                  f"{sum(i.upstream == 'no' for i in rows):>14}")
        xtra = [i for i in items if i.source == "xtra"]
        total_xtra = len(xtra) + info["duplicates"]
        print(f"\nxtra-patches: {total_xtra}, {info['duplicates']} already in {self.origin_ref} "
              f"(counted once, as origin commits)")
        if any(i.upstream == "related" for i in items):
            print("related: upstream has commits by the same author touching the same files, "
                  "often the same fix in another form. Check them with --verbose.")
        if any(i.touched for i in items):
            print("TOUCHED: upstream changed the same files, but the commits are by a DIFFERENT "
                  "AUTHOR than the patch. They may supersede it, conflict with it, or have "
                  "nothing to do with it. Read them with --verbose before deciding anything.")

        if verbose:
            print()
            for category in CATEGORIES:
                for item in (i for i in items if i.category == category):
                    mark = {"yes": "upstream", "subject": "subject ", "related": "related ",
                            "touched": "TOUCHED ", "no": "        "}[item.upstream]
                    print(f"{category:<12} {mark}  {item.source:<6} {item.ref[:24]:<24} {item.subject}")
                    pad = f"{'':<12} {'':<8}  {'':<6} {'':<24} "
                    for commit, subject in item.related[:3]:
                        print(f"{pad}-> {commit} {subject}")
                    for commit, name, subject in item.touched[:3]:
                        print(f"{pad}-> {commit} [DIFFERENT AUTHOR: {name}] {subject}")
                    if len(item.touched) > 3:
                        print(f"{pad}   (+{len(item.touched) - 3} more by other authors)")
        return 0
