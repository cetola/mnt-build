# SPDX-License-Identifier: MIT
"""U-Boot development workflow helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SysimageUBootInfo:
    sysimage: str
    project: str
    tag: str
    filename: str
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
        folder = str(Path(rel).parent)
        return folder + (" (fallback)" if is_fallback else "")

    def get_info(self, sysimage: str) -> SysimageUBootInfo:
        """Return U-Boot config for a single sysimage."""
        try:
            data = self._query_sysimage(sysimage)
        except subprocess.CalledProcessError as e:
            detail = e.stderr.strip() if e.stderr else str(e)
            raise ValueError(f"Failed to query config for '{sysimage}': {detail}") from e
        project = data.get("BOOTLOADER_PROJECT", "")
        if not project:
            raise ValueError(f"No BOOTLOADER_PROJECT found for sysimage '{sysimage}'")
        return SysimageUBootInfo(
            sysimage=sysimage,
            project=project,
            tag=data.get("BOOTLOADER_TAG", ""),
            filename=data.get("BOOTLOADER_FILENAME", ""),
            patch_count=self._patch_count(project),
            has_checkout=self._has_checkout(project),
            config_source=self._format_config_source(data),
        )

    def prepare(self, sysimage: str) -> int:
        """Clone/fetch U-Boot repo, checkout tag, apply local patches. Stops before building."""
        info = self.get_info(sysimage)
        checkout_dir = self._uboot_root / info.project
        repo_url = f"https://source.mnt.re/reform/{info.project}.git"
        patches_dir = self._patches_root / info.project

        print(f"[uboot] Preparing {info.project} for {sysimage}")
        print(f"[uboot] Tag:      {info.tag}")
        print(f"[uboot] Checkout: {checkout_dir}")

        if checkout_dir.is_dir():
            print(f"[uboot] Checkout already exists — skipping clone.")
            print(f"[uboot] To start fresh: mnt-build uboot --sysimage {sysimage} --clean")
            return 0

        # Clone
        print(f"[uboot] Cloning {repo_url} ...")
        subprocess.run(["git", "clone", repo_url, str(checkout_dir)], check=True)

        # Checkout tag (detached HEAD so accidental commits are obvious)
        print(f"[uboot] Checking out {info.tag} ...")
        subprocess.run(
            ["git", "-C", str(checkout_dir), "checkout", "--detach", info.tag],
            check=True,
        )

        # Apply patches
        patches = sorted(patches_dir.glob("*.patch")) if patches_dir.is_dir() else []
        if not patches:
            print(f"[uboot] No patches to apply (xtra-uboot-patches/{info.project}/ is empty)")
        else:
            print(f"[uboot] Applying {len(patches)} patch(es) from xtra-uboot-patches/{info.project}/ ...")
            subprocess.run(
                ["git", "-C", str(checkout_dir), "am", *[str(p) for p in patches]],
                check=True,
            )

        print(f"[uboot] Checkout ready at: uboot/{info.project}/")
        return 0

    def _find_artifact(self, checkout_dir: Path, filename: str) -> Path:
        """Locate the built flash artifact, mirroring fsbl_resolve_source_artifact."""
        candidates = [filename, "flash.bin"] if filename else ["flash.bin"]
        for name in candidates:
            p = checkout_dir / name
            if p.is_file():
                return p
        # Glob fallback: newest *-flash.bin anywhere in the tree
        matches = sorted(checkout_dir.rglob("*-flash.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
        if matches:
            return matches[0]
        raise FileNotFoundError(
            f"Could not find built artifact in {checkout_dir}. "
            "Use a custom build command if the output path differs."
        )

    def build(self, sysimage: str, cross_compile: str = "aarch64-linux-gnu-") -> int:
        """Build U-Boot for a sysimage. Prepares the checkout first if needed."""
        info = self.get_info(sysimage)
        checkout_dir = self._uboot_root / info.project

        if not checkout_dir.is_dir():
            print(f"[uboot] No checkout found — running prepare first ...")
            rc = self.prepare(sysimage)
            if rc != 0:
                return rc

        downloads_dir = self.root / "image-gen" / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        output_path = downloads_dir / info.filename

        jobs = str(os.cpu_count() or 4)

        print(f"[uboot] Building {info.project} ...")
        print(f"[uboot] CROSS_COMPILE: {cross_compile}")

        build_script = checkout_dir / "build.sh"
        if build_script.is_file():
            print(f"[uboot] Running build.sh ...")
            subprocess.run(
                ["bash", "./build.sh"],
                cwd=str(checkout_dir),
                env={**os.environ, "MAKEFLAGS": f"-j{jobs}"},
                check=True,
            )
        else:
            print(f"[uboot] Running make ...")
            subprocess.run(
                ["make", f"-j{jobs}", f"CROSS_COMPILE={cross_compile}"],
                cwd=str(checkout_dir),
                check=True,
            )

        artifact = self._find_artifact(checkout_dir, info.filename)
        print(f"[uboot] Found artifact: {artifact}")
        shutil.copy2(str(artifact), str(output_path))
        print(f"[uboot] Installed to: {output_path}")
        return 0

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
                filename=data.get("BOOTLOADER_FILENAME", ""),
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
        + "config source location"
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
