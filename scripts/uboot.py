# SPDX-License-Identifier: MIT
"""U-Boot development workflow helpers."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import urllib.request
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

    # Build prerequisites: (kind, check_name, apt_package, description)
    # kind: 'tool' -> shutil.which; 'python' -> importlib; 'library' -> pkg-config
    _BUILD_PREREQS = [
        ("tool",    "make",                    "build-essential",    "build system"),
        ("tool",    "git",                     "git",                "version control"),
        ("tool",    "bison",                   "bison",              "parser generator"),
        ("tool",    "flex",                    "flex",               "lexer generator"),
        ("tool",    "swig",                    "swig",               "Python/C bindings for U-Boot scripts"),
        ("python",  "elftools",                "python3-pyelftools", "ELF parsing for mkimage"),
        ("library", "gnutls",                  "libgnutls28-dev",    "TLS library headers"),
        ("library", "openssl",                 "libssl-dev",         "SSL library headers"),
    ]

    def _check_build_prerequisites(self, cross_compile: str) -> list[tuple[str, str]]:
        """Return list of (apt_package, description) for missing prerequisites."""
        missing = []

        for kind, name, pkg, desc in self._BUILD_PREREQS:
            if kind == "tool":
                if not shutil.which(name):
                    missing.append((pkg, desc))
            elif kind == "python":
                if importlib.util.find_spec(name) is None:
                    missing.append((pkg, desc))
            elif kind == "library":
                try:
                    result = subprocess.run(
                        ["pkg-config", "--exists", name],
                        capture_output=True,
                    )
                    if result.returncode != 0:
                        missing.append((pkg, desc))
                except FileNotFoundError:
                    # pkg-config not installed — skip library checks
                    pass

        # Cross-compiler is derived from cross_compile prefix at runtime
        compiler = f"{cross_compile}gcc"
        if not shutil.which(compiler):
            missing.append((f"gcc-{cross_compile.rstrip('-')}", f"cross-compiler ({compiler})"))

        return missing

    def build(self, sysimage: str, cross_compile: str = "aarch64-linux-gnu-") -> int:
        """Build U-Boot for a sysimage. Prepares the checkout first if needed."""
        print(f"[uboot] Checking build prerequisites ...", flush=True)
        missing = self._check_build_prerequisites(cross_compile)
        if missing:
            print(f"[uboot] Missing required packages:", file=sys.stderr)
            for pkg, desc in missing:
                print(f"  {pkg:<30}  # {desc}", file=sys.stderr)
            print(f"[uboot] Install with:", file=sys.stderr)
            print(f"  sudo apt install {' '.join(pkg for pkg, _ in missing)}", file=sys.stderr)
            return 1

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

    @staticmethod
    def _file_sha1(path: Path) -> str:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _file_sha256(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def diff_vs_prebuilt(self, sysimage: str, cross_compile: str = "aarch64-linux-gnu-") -> int:
        """Build (if needed) then byte-compare against the MNT prebuilt artifact."""
        info = self.get_info(sysimage)
        downloads_dir = self.root / "image-gen" / "downloads"
        built_artifact = downloads_dir / info.filename

        if not built_artifact.is_file():
            print(f"[uboot] No built artifact found — building first ...")
            rc = self.build(sysimage, cross_compile)
            if rc != 0:
                return rc

        prebuilt_url = (
            f"https://source.mnt.re/reform/{info.project}/-/jobs/artifacts"
            f"/{info.tag}/raw/{info.filename}?job=build"
        )
        prebuilt_artifact = downloads_dir / f"{info.filename}.prebuilt-{info.tag}"

        print(f"[uboot] Downloading prebuilt artifact ...")
        print(f"[uboot] URL: {prebuilt_url}")
        try:
            urllib.request.urlretrieve(prebuilt_url, str(prebuilt_artifact))
        except Exception as e:
            print(f"[uboot] ERROR: could not download prebuilt: {e}", file=sys.stderr)
            return 1

        built_size    = built_artifact.stat().st_size
        prebuilt_size = prebuilt_artifact.stat().st_size
        built_sha1    = self._file_sha1(built_artifact)
        prebuilt_sha1 = self._file_sha1(prebuilt_artifact)
        built_sha256  = self._file_sha256(built_artifact)
        prebuilt_sha256 = self._file_sha256(prebuilt_artifact)

        print()
        print("=" * 50)
        print("FSBL Diff Report")
        print("=" * 50)
        print(f"Built:          {built_artifact}")
        print(f"Prebuilt:       {prebuilt_artifact}")
        print()
        print(f"Built size:     {built_size}")
        print(f"Prebuilt size:  {prebuilt_size}")
        print(f"Built SHA1:     {built_sha1}")
        print(f"Prebuilt SHA1:  {prebuilt_sha1}")
        print(f"Built SHA256:   {built_sha256}")
        print(f"Prebuilt SHA256:{prebuilt_sha256}")
        print()

        if built_sha1 == prebuilt_sha1:
            print("Binary compare: exact match")
            return 0

        print("Binary compare: differ")

        cmp = shutil.which("cmp")
        if cmp:
            result = subprocess.run(
                [cmp, "-l", str(built_artifact), str(prebuilt_artifact)],
                capture_output=True, text=True,
            )
            lines = result.stdout.splitlines()
            if lines:
                first_byte = int(lines[0].split()[0])
                print(f"First differing byte (1-based): {first_byte}")
                print()
                print(f"First 20 differing bytes (byte, built-octal, prebuilt-octal):")
                for line in lines[:20]:
                    print(f"  {line}")
        return 1

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
