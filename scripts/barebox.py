# SPDX-License-Identifier: MIT
"""Barebox development workflow helpers."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SysimageBareboxInfo:
    sysimage: str
    project: str   # empty string = barebox not supported for this sysimage
    tag: str
    artifact: str  # expected image filename, e.g. barebox-mnt-pocket-reform-rk3588.img
    bootloader_offset: int
    flashbin_offset: int
    patch_count: int
    has_checkout: bool
    config_source: str


class BareboxManager:
    def __init__(self, mnt_build_root: Path):
        self.root = mnt_build_root
        self._query_script = mnt_build_root / "scripts" / "barebox-query.sh"
        self._patches_root = mnt_build_root / "xtra-patches" / "barebox"
        self._barebox_root = mnt_build_root / "barebox"

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

    def _patch_count(self) -> int:
        return len(list(self._patches_root.glob("*.patch"))) \
            if self._patches_root.is_dir() else 0

    def _has_checkout(self) -> bool:
        return self._barebox_root.is_dir()

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

    def get_info(self, sysimage: str) -> SysimageBareboxInfo:
        """Return barebox config for a single sysimage."""
        try:
            data = self._query_sysimage(sysimage)
        except subprocess.CalledProcessError as e:
            detail = e.stderr.strip() if e.stderr else str(e)
            raise ValueError(f"Failed to query config for '{sysimage}': {detail}") from e
        project = data.get("BAREBOX_PROJECT", "")
        return SysimageBareboxInfo(
            sysimage=sysimage,
            project=project,
            tag=data.get("BAREBOX_TAG", ""),
            artifact=data.get("BAREBOX_ARTIFACT", ""),
            bootloader_offset=int(data.get("BOOTLOADER_OFFSET", "0") or "0"),
            flashbin_offset=int(data.get("FLASHBIN_OFFSET", "0") or "0"),
            patch_count=self._patch_count() if project else 0,
            has_checkout=self._has_checkout() if project else False,
            config_source=self._format_config_source(data),
        )

    def list_sysimages(self) -> list[SysimageBareboxInfo]:
        results = []
        for sysimage in self._supported_sysimages():
            try:
                results.append(self.get_info(sysimage))
            except (ValueError, subprocess.CalledProcessError) as e:
                print(f"WARNING: failed to query config for {sysimage}: {e}", file=sys.stderr)
        return results

    def _resolve_ref(self, checkout_dir: Path, ref: str) -> str | None:
        """Resolve a git ref to a full SHA. Returns None if not found locally."""
        result = subprocess.run(
            ["git", "-C", str(checkout_dir), "rev-parse", "--verify",
             f"{ref}^{{commit}}"],
            capture_output=True, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def _apply_patches(self, checkout_dir: Path) -> int:
        patches = sorted(self._patches_root.glob("*.patch")) \
            if self._patches_root.is_dir() else []
        if not patches:
            print("[barebox] No patches to apply (xtra-patches/barebox/ is empty)")
        else:
            print(f"[barebox] Applying {len(patches)} patch(es) from "
                  "xtra-patches/barebox/ ...")
            subprocess.run(
                ["git", "-C", str(checkout_dir), "am",
                 *[str(p) for p in patches]],
                check=True,
            )
        print("[barebox] Checkout ready at: barebox/")
        return 0

    def prepare(self, sysimage: str) -> int:
        """Clone barebox repo, checkout tag, apply local patches. Stops before building."""
        info = self.get_info(sysimage)
        if not info.project:
            print(f"[barebox] barebox is not supported for sysimage '{sysimage}'",
                  file=sys.stderr)
            return 1

        checkout_dir = self._barebox_root
        repo_url = f"https://source.mnt.re/reform/{info.project}.git"

        print(f"[barebox] Preparing {info.project} for {sysimage}")
        print(f"[barebox] Ref:      {info.tag}")
        print(f"[barebox] Checkout: {checkout_dir}")

        if checkout_dir.is_dir():
            current = subprocess.run(
                ["git", "-C", str(checkout_dir), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            expected = self._resolve_ref(checkout_dir, info.tag)
            if expected is None:
                print(f"[barebox] Ref {info.tag!r} not found locally — fetching ...")
                subprocess.run(
                    ["git", "-C", str(checkout_dir), "fetch", "--tags", "origin"],
                    check=True,
                )
                expected = self._resolve_ref(checkout_dir, info.tag)
                if expected is None:
                    print(f"[barebox] ERROR: ref {info.tag!r} not found even after fetch",
                          file=sys.stderr)
                    return 1

            if current == expected:
                # At the base ref — patches not yet applied.
                return self._apply_patches(checkout_dir)

            # Check if current is already ahead of expected (patches applied).
            is_ahead = subprocess.run(
                ["git", "-C", str(checkout_dir), "merge-base", "--is-ancestor",
                 expected, current],
                capture_output=True,
            ).returncode == 0
            if is_ahead:
                print(f"[barebox] Checkout already patched ({current[:12]}) — skipping.")
                return 0

            print(f"[barebox] Ref changed: {current[:12]} → {expected[:12]}")
            print(f"[barebox] Updating checkout ...")
            subprocess.run(
                ["git", "-C", str(checkout_dir), "checkout", "--detach", info.tag],
                check=True,
            )
            return self._apply_patches(checkout_dir)

        print(f"[barebox] Cloning {repo_url} ...")
        subprocess.run(["git", "clone", repo_url, str(checkout_dir)], check=True)

        print(f"[barebox] Checking out {info.tag} ...")
        subprocess.run(
            ["git", "-C", str(checkout_dir), "checkout", "--detach", info.tag],
            check=True,
        )
        return self._apply_patches(checkout_dir)

    # Build prerequisites: (kind, check_name, apt_package, description)
    _BUILD_PREREQS = [
        ("tool",    "make",  "build-essential", "build system"),
        ("tool",    "git",   "git",             "version control"),
        ("tool",    "bison", "bison",            "parser generator"),
        ("tool",    "flex",  "flex",             "lexer generator"),
        ("library", "openssl", "libssl-dev",     "SSL library headers"),
    ]

    def _check_build_prerequisites(self) -> list[tuple[str, str]]:
        """Return list of (apt_package, description) for missing prerequisites."""
        import importlib.util
        missing = []
        for kind, name, pkg, desc in self._BUILD_PREREQS:
            if kind == "tool":
                if not shutil.which(name):
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
        compiler = "aarch64-linux-gnu-gcc"
        if not shutil.which(compiler):
            missing.append(("gcc-aarch64-linux-gnu", f"cross-compiler ({compiler})"))
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

    def _find_artifact(self, checkout_dir: Path, artifact_name: str) -> Path:
        """Locate the built barebox image inside the checkout."""
        images_dir = checkout_dir / "images"
        if artifact_name:
            candidate = images_dir / artifact_name
            if candidate.is_file():
                return candidate
            raise FileNotFoundError(
                f"Expected artifact '{artifact_name}' not found in {images_dir}. "
                "Has the build completed successfully?"
            )
        # No artifact name configured — fall back to newest barebox-*.img.
        if images_dir.is_dir():
            matches = sorted(images_dir.glob("barebox-*.img"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
            if matches:
                return matches[0]
        matches = sorted(checkout_dir.rglob("barebox-*.img"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        if matches:
            return matches[0]
        raise FileNotFoundError(
            f"Could not find barebox-*.img in {checkout_dir}. "
            "Has the build completed successfully?"
        )

    def build(self, sysimage: str) -> int:
        """Build barebox for a sysimage. Prepares the checkout first if needed."""
        print(f"[barebox] Checking build prerequisites ...", flush=True)
        missing = self._check_build_prerequisites()
        if missing:
            print(f"[barebox] Missing required packages:", file=sys.stderr)
            for pkg, desc in missing:
                print(f"  {pkg:<30}  # {desc}", file=sys.stderr)
            print(f"[barebox] Install with:", file=sys.stderr)
            print(f"  sudo apt install {' '.join(pkg for pkg, _ in missing)}",
                  file=sys.stderr)
            return 1

        info = self.get_info(sysimage)
        if not info.project:
            print(f"[barebox] barebox is not supported for sysimage '{sysimage}'",
                  file=sys.stderr)
            return 1

        start_time = time.monotonic()
        checkout_dir = self._barebox_root

        rc = self.prepare(sysimage)
        if rc != 0:
            return rc

        downloads_dir = self.root / "image-gen" / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = self.root / f"barebox-build-{sysimage}-{timestamp}.log"
        print(f"[barebox] Log: {log_path.relative_to(self.root)}")

        jobs = str(os.cpu_count() or 4)
        build_env = {**os.environ, "MAKEFLAGS": f"-j{jobs}"}

        print(f"[barebox] Building {info.project} ...")
        print(f"[barebox] Running build.sh (CROSS_COMPILE set internally by build.sh)")

        with open(log_path, "w") as log_fh:
            rc = self._run_logged(
                ["bash", "./build.sh"], str(checkout_dir), build_env, log_fh
            )

        if rc != 0:
            print(f"[barebox] Build failed (exit {rc}). "
                  f"Log: {log_path.relative_to(self.root)}", file=sys.stderr)
            return rc

        artifact = self._find_artifact(checkout_dir, info.artifact)
        output_path = downloads_dir / artifact.name
        shutil.copy2(artifact, output_path)

        elapsed = time.monotonic() - start_time
        self._print_build_summary(info, artifact, output_path, elapsed, log_path)
        return 0

    def _print_build_summary(
        self,
        info: SysimageBareboxInfo,
        artifact: Path,
        output_path: Path,
        elapsed: float,
        log_path: Path,
    ) -> None:
        sha1 = self._file_sha1(output_path)
        size = output_path.stat().st_size
        elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"

        patch_files = sorted(self._patches_root.glob("*.patch")) \
            if self._patches_root.is_dir() else []

        rel_artifact = output_path.relative_to(self.root) \
            if output_path.is_relative_to(self.root) else output_path
        rel_checkout = self._barebox_root.relative_to(self.root)
        rel_log = log_path.relative_to(self.root) \
            if log_path.is_relative_to(self.root) else log_path

        lines = [
            "",
            "=" * 52,
            "Barebox Build Summary",
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

        seek_blocks = info.bootloader_offset // 512 if info.bootloader_offset else 0
        skip_blocks = info.flashbin_offset // 512 if info.flashbin_offset else 0

        dd_args = f"seek={seek_blocks}"
        if skip_blocks:
            dd_args += f" skip={skip_blocks}"
        dd_args += " conv=notrunc,fsync"

        lines += [
            "",
            f"Artifact:   {rel_artifact}",
            f"  size:     {size}",
            f"  SHA1:     {sha1}",
            "",
            f"Build time: {elapsed_str}",
            f"Log:        {rel_log}",
        ]

        if seek_blocks:
            lines += [
                "",
                f"To write to SD card (seek={seek_blocks} → offset {info.bootloader_offset}):",
                f"  dd if={rel_artifact} of=/dev/<device> bs=512 {dd_args}",
                "  (replace <device> with your SD card, e.g. mmcblk1 or sda)",
                "",
                f"To test from SD before flashing eMMC, erase eMMC bootloader first:",
                f"  dd if=/dev/zero bs=512 seek={seek_blocks} of=/dev/mmcblk0 count=2",
            ]

        lines.append("=" * 52)

        for line in lines:
            print(line)

        with open(log_path, "a") as log_fh:
            for line in lines:
                log_fh.write(line + "\n")

    def diff_vs_prebuilt(self, sysimage: str) -> int:
        """Build (if needed) then byte-level diff against the MNT CI artifact."""
        info = self.get_info(sysimage)
        if not info.project:
            print(f"[barebox] barebox is not supported for sysimage '{sysimage}'",
                  file=sys.stderr)
            return 1

        downloads_dir = self.root / "image-gen" / "downloads"
        built_artifacts = list(downloads_dir.glob("barebox-*.img")) if downloads_dir.is_dir() else []

        if not built_artifacts:
            print(f"[barebox] No built artifact found — building first ...")
            rc = self.build(sysimage)
            if rc != 0:
                return rc
            built_artifacts = list(downloads_dir.glob("barebox-*.img"))

        built_artifact = max(built_artifacts, key=lambda p: p.stat().st_mtime)
        built_sha1 = self._file_sha1(built_artifact)
        built_size = built_artifact.stat().st_size

        print()
        print("=" * 50)
        print("Barebox Diff Report")
        print("=" * 50)
        print(f"Built artifact: {built_artifact}")
        print(f"  size:   {built_size}")
        print(f"  SHA1:   {built_sha1}")
        print()

        if info.config_source.startswith("local-machines"):
            print(
                "This is a custom sysimage (local-machines/) — no prebuilt artifact\n"
                "exists on source.mnt.re. Nothing to compare against."
            )
            return 1

        prebuilt_downloads = downloads_dir / "prebuilt-ref"
        prebuilt_downloads.mkdir(parents=True, exist_ok=True)
        prebuilt_artifact = prebuilt_downloads / built_artifact.name

        artifact_path_in_ci = f"images/{built_artifact.name}"
        url = (
            f"https://source.mnt.re/reform/{info.project}"
            f"/-/jobs/artifacts/{info.tag}/raw/{artifact_path_in_ci}?job=build"
        )

        if prebuilt_artifact.is_file():
            print(f"Using cached prebuilt artifact: {prebuilt_artifact}")
        else:
            print(f"Downloading prebuilt artifact ...")
            print(f"  {url}")
            result = subprocess.run(
                ["curl", "-fL", "--retry", "3", "--retry-delay", "2",
                 "-o", str(prebuilt_artifact), url],
            )
            if result.returncode != 0:
                print("Prebuilt artifact not available — CI artifacts may have expired.")
                return 1

        prebuilt_sha1 = self._file_sha1(prebuilt_artifact)
        prebuilt_size = prebuilt_artifact.stat().st_size

        print(f"Prebuilt artifact: {prebuilt_artifact}")
        print(f"  size:   {prebuilt_size}")
        print(f"  SHA1:   {prebuilt_sha1}")
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

    def menuconfig(self, sysimage: str,
                   cross_compile: str = "aarch64-linux-gnu-") -> int:
        """Run make menuconfig directly in the barebox checkout."""
        info = self.get_info(sysimage)
        if not info.project:
            print(f"[barebox] barebox is not supported for sysimage '{sysimage}'",
                  file=sys.stderr)
            return 1

        checkout_dir = self._barebox_root

        rc = self.prepare(sysimage)
        if rc != 0:
            return rc

        if not (checkout_dir / ".config").is_file():
            defconfig = checkout_dir / "mnt-reform-defconfig"
            if not defconfig.is_file():
                print(f"[barebox] mnt-reform-defconfig not found in checkout",
                      file=sys.stderr)
                return 1
            print(f"[barebox] No .config found — copying mnt-reform-defconfig ...")
            shutil.copy2(defconfig, checkout_dir / ".config")

        print(f"[barebox] Running menuconfig for {info.project} ...")
        result = subprocess.run(
            ["make", "menuconfig", "ARCH=arm64",
             f"CROSS_COMPILE={cross_compile}"],
            cwd=str(checkout_dir),
        )
        return result.returncode

    def clean(self, sysimage: str) -> int:
        """Remove the barebox checkout for a sysimage."""
        info = self.get_info(sysimage)
        if not info.project:
            print(f"[barebox] barebox is not supported for sysimage '{sysimage}'",
                  file=sys.stderr)
            return 1

        checkout_dir = self._barebox_root

        if not checkout_dir.is_dir():
            print(f"[barebox] No checkout found at barebox/ — nothing to clean.")
            return 0

        print(f"[barebox] Removing barebox/ ...")
        shutil.rmtree(checkout_dir)
        print(f"[barebox] Done.")
        return 0

    @staticmethod
    def _file_sha1(path: Path) -> str:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()


def print_barebox_table(infos: list[SysimageBareboxInfo]) -> None:
    col_sysimage = max(len("sysimage"),
                       max((len(i.sysimage) for i in infos), default=0)) + 2
    col_project  = max(len("project"),
                       max((len(i.project) for i in infos), default=0)) + 2
    col_tag      = max(len("tag"),
                       max((len(i.tag) for i in infos), default=0)) + 2
    col_patches  = max(len("patches"), 3) + 2
    col_checkout = max(len("checkout"), 3) + 2
    col_support  = max(len("barebox"), 3) + 2

    header = (
        "sysimage".ljust(col_sysimage)
        + "project".ljust(col_project)
        + "tag".ljust(col_tag)
        + "patches".ljust(col_patches)
        + "checkout".ljust(col_checkout)
        + "barebox".ljust(col_support)
        + "config source location"
    )
    print(header)
    print("-" * len(header))

    for i in infos:
        supported = "yes" if i.project else "no"
        print(
            i.sysimage.ljust(col_sysimage)
            + i.project.ljust(col_project)
            + i.tag.ljust(col_tag)
            + str(i.patch_count).ljust(col_patches)
            + ("yes" if i.has_checkout else "no").ljust(col_checkout)
            + supported.ljust(col_support)
            + i.config_source
        )
