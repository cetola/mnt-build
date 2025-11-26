#!/usr/bin/env python3

import os
import sys
import shutil
import subprocess
import tarfile
from pathlib import Path

KERNEL_SRC = Path("~/mnt-build/linux").expanduser()

ARCH = "arm64"
CROSS_COMPILE = "aarch64-linux-gnu-"

DIRS_TO_COPY = [
    "arch",
    "include",
    "scripts",
    "tools/include",
    "tools/objtool", 
]

FILES_TO_COPY = [
    ".config",
    "Makefile",
    "Module.symvers",
    "System.map",
    "Kconfig", 
]

def read_kernel_release(src: Path) -> str:
    major = minor = patch = extra = None
    mf = src / "Makefile"

    if mf.exists():
        with mf.open() as f:
            for line in f:
                if line.startswith("VERSION ="):
                    major = line.split("=")[1].strip()
                elif line.startswith("PATCHLEVEL ="):
                    minor = line.split("=")[1].strip()
                elif line.startswith("SUBLEVEL ="):
                    patch = line.split("=")[1].strip()

                if major is not None and minor is not None and patch is not None:
                    break

    version = f"{major}.{minor}.{patch}"

    return version

def prepare_kernel_headers(src: Path):
    """Run make prepare and make modules_prepare to generate all needed files."""
    env = os.environ.copy()
    env["ARCH"] = ARCH
    env["CROSS_COMPILE"] = CROSS_COMPILE
    
    num_jobs = os.cpu_count() or 1
    
    print(f"=== Running 'make prepare' with ARCH={ARCH} CROSS_COMPILE={CROSS_COMPILE} -j{num_jobs} ===")
    result = subprocess.run(
        ["make", f"-j{num_jobs}", "prepare"],
        cwd=src,
        env=env,
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
        raise SystemExit(f"ERROR: 'make prepare' failed with exit code {result.returncode}")
    
    print(f"=== Running 'make modules_prepare' with ARCH={ARCH} CROSS_COMPILE={CROSS_COMPILE} -j{num_jobs} ===")
    result = subprocess.run(
        ["make", f"-j{num_jobs}", "modules_prepare"],
        cwd=src,
        env=env,
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
        raise SystemExit(f"ERROR: 'make modules_prepare' failed with exit code {result.returncode}")
    
    print("Kernel headers prepared successfully!")

def copy_selected(src: Path, dst: Path, relative: str):
    s = src / relative
    d = dst / relative
    if not s.exists():
        print(f"[WARN] Skipping missing {s}")
        return
    print(f"[COPY] {s} → {d}")
    if s.is_dir():
        shutil.copytree(s, d, symlinks=True)
    else:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)


def copy_kconfig_files(src: Path, dst: Path):
    """Copy all Kconfig* files throughout the tree."""
    print("=== Copying all Kconfig files ===")
    for kconfig in src.rglob("Kconfig*"):
        # Skip Kconfig files in directories we're already copying wholesale
        rel_path = kconfig.relative_to(src)
        
        # Skip if already in a directory we're copying
        skip = False
        for dir_to_copy in DIRS_TO_COPY:
            if str(rel_path).startswith(dir_to_copy + "/") or str(rel_path) == dir_to_copy:
                skip = True
                break
        
        if skip:
            continue
            
        dst_kconfig = dst / rel_path
        dst_kconfig.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(kconfig, dst_kconfig)
        print(f"[COPY] {kconfig} → {dst_kconfig}")


def main():
    pkgrel = 1
    if len(sys.argv) > 1:
        try:
            pkgrel = int(sys.argv[1])
        except ValueError:
            print(f"ERROR: Invalid pkgrel '{sys.argv[1]}'. Must be a number.")
            print(f"Usage: {sys.argv[0]} [pkgrel]")
            sys.exit(1)
    
    print(f"Using pkgrel: {pkgrel}")

    if not KERNEL_SRC.exists():
        raise SystemExit(f"ERROR: Kernel source path does not exist: {KERNEL_SRC}")

    print("=== Determining kernel version ===")
    version = read_kernel_release(KERNEL_SRC)
    print(f"Kernel release version: {version}")

    prepare_kernel_headers(KERNEL_SRC)

    OUTPUT_TARBALL = Path(f"headers-{version}-{pkgrel}-mnt.tar.gz")
    staging_dir = Path("kernel_headers_staging")

    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir()

    print("=== Copying required directories ===")
    for d in DIRS_TO_COPY:
        copy_selected(KERNEL_SRC, staging_dir, d)

    print("=== Copying required files ===")
    for f in FILES_TO_COPY:
        copy_selected(KERNEL_SRC, staging_dir, f)

    copy_kconfig_files(KERNEL_SRC, staging_dir)

    print(f"=== Creating tarball: {OUTPUT_TARBALL} ===")
    with tarfile.open(OUTPUT_TARBALL, "w:gz") as tar:
        tar.add(staging_dir, arcname=f"linux-{version}")

    print("\nDone.")
    print(f"Created: {OUTPUT_TARBALL}")
    print("Staging directory: kernel_headers_staging")


if __name__ == "__main__":
    main()
