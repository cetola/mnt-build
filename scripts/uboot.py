# SPDX-License-Identifier: MIT
"""U-Boot development workflow helpers."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SysimageUBootInfo:
    sysimage: str
    project: str
    tag: str
    filename: str
    sha1: str
    bootloader_offset: int
    patch_count: int
    has_checkout: bool
    config_source: str


class UBootManager:
    def __init__(self, mnt_build_root: Path):
        self.root = mnt_build_root
        self._query_script = mnt_build_root / "scripts" / "uboot-query.sh"
        self._patches_root = mnt_build_root / "xtra-patches" / "u-boot"
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
            sha1=data.get("BOOTLOADER_SHA1", ""),
            bootloader_offset=int(data.get("BOOTLOADER_OFFSET", "0") or "0"),
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

        print(f"[uboot] Cloning {repo_url} ...")
        subprocess.run(["git", "clone", repo_url, str(checkout_dir)], check=True)

        # Detached HEAD, so accidental commits are obvious.
        print(f"[uboot] Checking out {info.tag} ...")
        subprocess.run(
            ["git", "-C", str(checkout_dir), "checkout", "--detach", info.tag],
            check=True,
        )

        patches = sorted(patches_dir.glob("*.patch")) if patches_dir.is_dir() else []
        if not patches:
            print(f"[uboot] No patches to apply (xtra-patches/u-boot/{info.project}/ is empty)")
        else:
            print(f"[uboot] Applying {len(patches)} patch(es) from xtra-patches/u-boot/{info.project}/ ...")
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
        matches = sorted(checkout_dir.rglob("*-flash.bin"), key=lambda p: p.stat().st_mtime, reverse=True)
        if matches:
            return matches[0]
        raise FileNotFoundError(
            f"Could not find built artifact in {checkout_dir}. "
            "Use a custom build command if the output path differs."
        )

    # Build prerequisites: (kind, check_name, apt_package, description)
    # kind is one of:
    #   tool    -> shutil.which
    #   python  -> importlib
    #   library -> pkg-config
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
                    pass

        # Not in _BUILD_PREREQS: derived from cross_compile at runtime, not fixed.
        compiler = f"{cross_compile}gcc"
        if not shutil.which(compiler):
            missing.append((f"gcc-{cross_compile.rstrip('-')}", f"cross-compiler ({compiler})"))

        return missing

    @staticmethod
    def _run_logged(cmd: list[str], cwd: str, env: dict, log_fh) -> int:
        """Run a command, teeing stdout+stderr to the terminal and log_fh."""
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log_fh.write(line)
        proc.wait()
        return proc.returncode

    def build(self, sysimage: str, cross_compile: str = "aarch64-linux-gnu-") -> int:
        print(f"[uboot] Checking build prerequisites ...", flush=True)
        missing = self._check_build_prerequisites(cross_compile)
        if missing:
            print(f"[uboot] Missing required packages:", file=sys.stderr)
            for pkg, desc in missing:
                print(f"  {pkg:<30}  # {desc}", file=sys.stderr)
            print(f"[uboot] Install with:", file=sys.stderr)
            print(f"  sudo apt install {' '.join(pkg for pkg, _ in missing)}", file=sys.stderr)
            return 1

        start_time = time.monotonic()
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

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = self.root / f"uboot-build-{sysimage}-{timestamp}.log"
        print(f"[uboot] Log: {log_path.relative_to(self.root)}")

        jobs = str(os.cpu_count() or 4)
        build_env = {**os.environ, "MAKEFLAGS": f"-j{jobs}"}

        print(f"[uboot] Building {info.project} ...")
        print(f"[uboot] CROSS_COMPILE: {cross_compile}")

        build_script = checkout_dir / "build.sh"
        with open(log_path, "w") as log_fh:
            if build_script.is_file():
                print(f"[uboot] Running build.sh ...")
                rc = self._run_logged(["bash", "./build.sh"], str(checkout_dir), build_env, log_fh)
            else:
                print(f"[uboot] Running make ...")
                rc = self._run_logged(
                    ["make", f"-j{jobs}", f"CROSS_COMPILE={cross_compile}"],
                    str(checkout_dir), build_env, log_fh,
                )

        if rc != 0:
            print(f"[uboot] Build failed (exit {rc}). Log: {log_path.relative_to(self.root)}", file=sys.stderr)
            return rc

        artifact = self._find_artifact(checkout_dir, info.filename)
        shutil.copy2(artifact, output_path)

        elapsed = time.monotonic() - start_time
        self._print_build_summary(info, output_path, elapsed, log_path)
        return 0

    def _print_build_summary(
        self, info: SysimageUBootInfo, artifact: Path, elapsed: float, log_path: Path
    ) -> None:
        sha1 = self._file_sha1(artifact)
        size = artifact.stat().st_size
        elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"

        patches_dir = self._patches_root / info.project
        patch_files = sorted(patches_dir.glob("*.patch")) if patches_dir.is_dir() else []

        seek_blocks = info.bootloader_offset // 512 if info.bootloader_offset else 0

        rel_artifact = artifact.relative_to(self.root) if artifact.is_relative_to(self.root) else artifact
        rel_checkout = (self._uboot_root / info.project).relative_to(self.root)
        rel_log = log_path.relative_to(self.root) if log_path.is_relative_to(self.root) else log_path

        lines = [
            "",
            "=" * 52,
            "U-Boot Build Summary",
            "=" * 52,
            f"Sysimage:   {info.sysimage}",
            f"Project:    {info.project}",
            f"Tag:        {info.tag}",
            f"Checkout:   {rel_checkout}/",
            "",
        ]

        if patch_files:
            lines.append(f"Xtra patches: {len(patch_files)} applied")
            for p in patch_files:
                lines.append(f"  {p.name}")
        else:
            lines.append("Xtra patches: none")

        lines += [
            "",
            f"Artifact:   {rel_artifact}",
            f"  size:     {size}",
            f"  SHA1:     {sha1}",
            "",
            f"Build time: {elapsed_str}",
            f"Log:        {rel_log}",
            "",
            "To write to SD card:",
        ]
        if seek_blocks:
            lines.append(f"  dd if={rel_artifact} of=/dev/<device> bs=512 seek={seek_blocks} conv=notrunc,fsync")
        else:
            lines.append(f"  dd if={rel_artifact} of=/dev/<device> conv=notrunc,fsync")
        lines += [
            "  (replace <device> with your SD card, e.g. sda or mmcblk0)",
            "=" * 52,
        ]

        for line in lines:
            print(line)

        with open(log_path, "a") as log_fh:
            for line in lines:
                log_fh.write(line + "\n")

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
        """Build (if needed) then byte-level diff against the MNT prebuilt artifact.

        Downloads the prebuilt from source.mnt.re (GitLab CI artifacts) for comparison.
        GitLab artifacts expire, so the download is best-effort. A 404 is reported
        clearly rather than treated as an error.
        """
        info = self.get_info(sysimage)
        downloads_dir = self.root / "image-gen" / "downloads"
        built_artifact = downloads_dir / info.filename

        if not built_artifact.is_file():
            print(f"[uboot] No built artifact found — building first ...")
            rc = self.build(sysimage, cross_compile)
            if rc != 0:
                return rc

        built_sha1   = self._file_sha1(built_artifact)
        built_sha256 = self._file_sha256(built_artifact)
        built_size   = built_artifact.stat().st_size

        print()
        print("=" * 50)
        print("U-Boot Diff Report")
        print("=" * 50)
        print(f"Built artifact: {built_artifact}")
        print(f"  size:   {built_size}")
        print(f"  SHA1:   {built_sha1}")
        print(f"  SHA256: {built_sha256}")
        print()

        # Custom sysimages (config from local-machines/) have no prebuilt CI artifact.
        if info.config_source.startswith("local-machines"):
            print(
                "This is a custom sysimage (local-machines/) — no prebuilt artifact\n"
                "exists on source.mnt.re. Nothing to compare against."
            )
            return 1

        # curl directly, same URL pattern as build-fsbl.sh, to avoid its SHA1
        # hard-fail, which would reject any source build not matching MNT's CI binary.
        prebuilt_downloads = downloads_dir / "prebuilt-ref"
        prebuilt_downloads.mkdir(parents=True, exist_ok=True)
        prebuilt_artifact = prebuilt_downloads / info.filename

        if prebuilt_artifact.is_file():
            print(f"Using cached prebuilt artifact: {prebuilt_artifact}")
        else:
            url = (
                f"https://source.mnt.re/reform/{info.project}"
                f"/-/jobs/artifacts/{info.tag}/raw/{info.filename}?job=build"
            )
            print(f"Downloading prebuilt artifact ...")
            print(f"  {url}")
            result = subprocess.run(
                ["curl", "-fL", "--retry", "3", "--retry-delay", "2",
                 "-o", str(prebuilt_artifact), url],
            )
            if result.returncode != 0:
                print("Prebuilt artifact not available — CI artifacts may have expired.")
                return 1

        prebuilt_sha1   = self._file_sha1(prebuilt_artifact)
        prebuilt_sha256 = self._file_sha256(prebuilt_artifact)
        prebuilt_size   = prebuilt_artifact.stat().st_size

        print(f"Prebuilt artifact: {prebuilt_artifact}")
        print(f"  size:   {prebuilt_size}")
        print(f"  SHA1:   {prebuilt_sha1}")
        print(f"  SHA256: {prebuilt_sha256}")
        print()

        if built_sha1 == prebuilt_sha1:
            print("Binary compare: exact match")
            return 0

        print("Binary compare: differ")
        cmp_bin = shutil.which("cmp")
        if cmp_bin:
            result = subprocess.run(
                [cmp_bin, "-l", str(built_artifact), str(prebuilt_artifact)],
                capture_output=True, text=True,
            )
            lines = result.stdout.splitlines()
            if lines:
                first_byte = int(lines[0].split()[0])
                print(f"First differing byte (1-based): {first_byte}")
                print()
                print("First 20 differing bytes (byte  built-octal  prebuilt-octal):")
                for line in lines[:20]:
                    print(f"  {line}")
        return 1

    def _find_defconfig(self, checkout_dir: Path, filename: str) -> Path | None:
        """Find the best matching defconfig for a BOOTLOADER_FILENAME.

        The defconfig files live in the checkout root (e.g. rk3588-mnt-pocket-reform_defconfig).
        BOOTLOADER_FILENAME may use a variant prefix (e.g. rk3588s-) that doesn't
        match any defconfig exactly, so fall back to a machine-part match.
        """
        base = filename.removesuffix("-flash.bin") if filename.endswith("-flash.bin") else filename

        exact = checkout_dir / f"{base}_defconfig"
        if exact.is_file():
            return exact

        # e.g. "rk3588s-mnt-pocket-reform" -> look for "*mnt-pocket-reform_defconfig"
        parts = base.split("-", 1)
        if len(parts) == 2:
            machine_part = parts[1]
            candidates = sorted(checkout_dir.glob(f"*{machine_part}_defconfig"))
            if candidates:
                return candidates[0]

        return None

    def _parse_uboot_subdir_params(self, checkout_dir: Path) -> tuple[str, str] | None:
        """Return (url, sha) for the u-boot sub-repo from build.sh, or None."""
        build_sh = checkout_dir / "build.sh"
        if not build_sh.is_file():
            return None
        m = re.search(
            r'git_shallow_clone\s+(\S+)\s+([0-9a-f]+)\s+u-boot',
            build_sh.read_text(),
        )
        return (m.group(1), m.group(2)) if m else None

    def _init_uboot_subdir(self, checkout_dir: Path, uboot_dir: Path) -> int:
        """Shallow-clone the u-boot sub-repo using the URL and SHA from build.sh."""
        params = self._parse_uboot_subdir_params(checkout_dir)
        if params is None:
            print(f"[uboot] Could not find u-boot clone parameters in build.sh")
            return 1

        url, sha = params
        print(f"[uboot] Initializing u-boot source ({sha[:12]}) from:")
        print(f"[uboot]   {url}")
        for cmd in [
            ["git", "init", str(uboot_dir)],
            ["git", "-C", str(uboot_dir), "remote", "add", "origin", url],
            ["git", "-C", str(uboot_dir), "fetch", "--depth", "1", "origin", sha],
            ["git", "-C", str(uboot_dir), "checkout", "FETCH_HEAD"],
        ]:
            result = subprocess.run(cmd)
            if result.returncode != 0:
                return result.returncode
        return 0

    def reset(self, sysimage: str) -> int:
        """Reset the inner u-boot/ sub-repo to the SHA expected by build.sh.

        Use this after exporting patches from the development tree to get back
        to a clean state so the next build succeeds.
        """
        info = self.get_info(sysimage)
        checkout_dir = self._uboot_root / info.project
        uboot_dir = checkout_dir / "u-boot"

        if not checkout_dir.is_dir():
            print(f"[uboot] No checkout found at uboot/{info.project}/ — nothing to reset.")
            return 0

        if not uboot_dir.is_dir():
            print(f"[uboot] u-boot/ sub-repo not present — nothing to reset.")
            return 0

        params = self._parse_uboot_subdir_params(checkout_dir)
        if params is None:
            print(f"[uboot] Could not read expected SHA from build.sh")
            return 1

        _, sha = params
        print(f"[uboot] Resetting uboot/{info.project}/u-boot/ to {sha[:12]} ...")
        for cmd in [
            ["git", "-C", str(uboot_dir), "reset", "--hard", sha],
            ["git", "-C", str(uboot_dir), "clean", "-fdx"],
        ]:
            result = subprocess.run(cmd)
            if result.returncode != 0:
                return result.returncode
        print(f"[uboot] Done.")
        return 0

    def menuconfig(self, sysimage: str, cross_compile: str = "aarch64-linux-gnu-") -> int:
        """Run make menuconfig in the U-Boot source tree.

        The actual U-Boot source lives in uboot/<project>/u-boot/ (a sub-repo
        managed by build.sh). If no .config exists there, the matching defconfig
        is loaded automatically. No prior build required.
        """
        info = self.get_info(sysimage)
        checkout_dir = self._uboot_root / info.project
        uboot_dir = checkout_dir / "u-boot"

        if not checkout_dir.is_dir():
            print(f"[uboot] No checkout found — running prepare first ...")
            rc = self.prepare(sysimage)
            if rc != 0:
                return rc

        if not uboot_dir.is_dir():
            rc = self._init_uboot_subdir(checkout_dir, uboot_dir)
            if rc != 0:
                return rc

        if not (uboot_dir / ".config").is_file():
            print(f"[uboot] No .config found — loading defconfig ...")
            defconfig = self._find_defconfig(checkout_dir, info.filename)
            if defconfig is None:
                print(f"[uboot] Could not find a matching defconfig in uboot/{info.project}/")
                available = sorted(checkout_dir.glob("*_defconfig"))
                if available:
                    print("[uboot] Available defconfigs:")
                    for f in available:
                        print(f"[uboot]   {f.name}")
                return 1
            print(f"[uboot] Using defconfig: {defconfig.name}")
            shutil.copy2(defconfig, uboot_dir / "configs" / defconfig.name)
            result = subprocess.run(
                ["make", defconfig.name, f"CROSS_COMPILE={cross_compile}", "ARCH=arm64"],
                cwd=str(uboot_dir),
            )
            if result.returncode != 0:
                return result.returncode

        print(f"[uboot] Running menuconfig for {info.project} ...")
        result = subprocess.run(
            ["make", "menuconfig", f"CROSS_COMPILE={cross_compile}", "ARCH=arm64"],
            cwd=str(uboot_dir),
        )
        return result.returncode

    def clean(self, sysimage: str) -> int:
        info = self.get_info(sysimage)
        checkout_dir = self._uboot_root / info.project

        if not checkout_dir.is_dir():
            print(f"[uboot] No checkout found at uboot/{info.project}/ — nothing to clean.")
            return 0

        print(f"[uboot] Removing uboot/{info.project}/ ...")
        shutil.rmtree(checkout_dir)
        print(f"[uboot] Done.")
        return 0

    def list_sysimages(self) -> list[SysimageUBootInfo]:
        results = []
        for sysimage in self._supported_sysimages():
            try:
                results.append(self.get_info(sysimage))
            except (ValueError, subprocess.CalledProcessError) as e:
                print(f"WARNING: failed to query config for {sysimage}: {e}", file=sys.stderr)
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
