# SPDX-License-Identifier: MIT
"""U-Boot development workflow helpers."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SysimageUBootInfo:
    sysimage: str
    project: str
    tag: str
    patch_count: int
    has_checkout: bool
    config_source: str


class UBootManager:
    def __init__(self, mnt_build_root: Path):
        self.root = mnt_build_root
        self._query_script = mnt_build_root / "scripts" / "uboot-query.sh"
        self._patches_root = mnt_build_root / "xtra-uboot-patches"
        self._uboot_root = mnt_build_root / "uboot"

    def _run_query(self, *args: str) -> str:
        result = subprocess.run(
            ["bash", str(self._query_script), *args],
            capture_output=True, text=True, check=True,
        )
        return result.stdout

    def _supported_sysimages(self) -> list[str]:
        return [s for s in self._run_query().splitlines() if s.strip()]

    def _query_sysimage(self, sysimage: str) -> dict[str, str]:
        data: dict[str, str] = {}
        for line in self._run_query(sysimage).splitlines():
            key, _, val = line.partition("=")
            if key:
                data[key] = val
        return data

    def _patch_count(self, project: str) -> int:
        d = self._patches_root / project
        return len(list(d.glob("*.patch"))) if d.is_dir() else 0

    def _has_checkout(self, project: str) -> bool:
        return (self._uboot_root / project).is_dir()

    def _format_config_source(self, data: dict[str, str]) -> str:
        path_str = data.get("CONFIG_SOURCE_PATH", "")
        is_fallback = data.get("CONFIG_SOURCE_IS_FALLBACK", "0") == "1"
        if not path_str:
            return ""
        try:
            rel = str(Path(path_str).resolve().relative_to(self.root.resolve()))
        except ValueError:
            rel = path_str
        return rel + (" (fallback)" if is_fallback else "")

    def list_sysimages(self) -> list[SysimageUBootInfo]:
        results = []
        for sysimage in self._supported_sysimages():
            try:
                data = self._query_sysimage(sysimage)
            except subprocess.CalledProcessError as e:
                print(f"WARNING: failed to query config for {sysimage}: {e}", file=sys.stderr)
                continue
            project = data.get("BOOTLOADER_PROJECT", "")
            results.append(SysimageUBootInfo(
                sysimage=sysimage,
                project=project,
                tag=data.get("BOOTLOADER_TAG", ""),
                patch_count=self._patch_count(project),
                has_checkout=self._has_checkout(project),
                config_source=self._format_config_source(data),
            ))
        return results


def print_sysimage_table(infos: list[SysimageUBootInfo]) -> None:
    col_sysimage = max(len("sysimage"),   max((len(i.sysimage) for i in infos), default=0)) + 2
    col_project  = max(len("project"),    max((len(i.project)  for i in infos), default=0)) + 2
    col_tag      = max(len("tag"),        max((len(i.tag)      for i in infos), default=0)) + 2
    col_patches  = max(len("patches"), 3) + 2
    col_checkout = max(len("checkout"), 3) + 2

    header = (
        "sysimage".ljust(col_sysimage)
        + "project".ljust(col_project)
        + "tag".ljust(col_tag)
        + "patches".ljust(col_patches)
        + "checkout".ljust(col_checkout)
        + "config source"
    )
    print(header)
    print("-" * len(header))

    for i in infos:
        print(
            i.sysimage.ljust(col_sysimage)
            + i.project.ljust(col_project)
            + i.tag.ljust(col_tag)
            + str(i.patch_count).ljust(col_patches)
            + ("yes" if i.has_checkout else "no").ljust(col_checkout)
            + i.config_source
        )
