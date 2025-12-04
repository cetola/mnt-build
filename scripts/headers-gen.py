#!/usr/bin/env python3

import logging
import os
import sys
import shutil
import subprocess
import tarfile
import argparse
from datetime import datetime
from pathlib import Path

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

VERBOSE = False


# ANSI color codes for terminal output
class Colors:
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[1;33m'
    BLUE = '\033[0;34m'
    RESET = '\033[0m'


class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors for different log levels."""

    FORMATS = {
        logging.DEBUG: f"{Colors.BLUE}[DEBUG]{Colors.RESET} %(asctime)s - %(message)s",
        logging.INFO: f"{Colors.BLUE}[INFO]{Colors.RESET} %(asctime)s - %(message)s",
        logging.WARNING: f"{Colors.YELLOW}[WARN]{Colors.RESET} %(asctime)s - %(message)s",
        logging.ERROR: f"{Colors.RED}[ERROR]{Colors.RESET} %(asctime)s - %(message)s",
        logging.CRITICAL: f"{Colors.RED}[CRITICAL]{Colors.RESET} %(asctime)s - %(message)s",
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt='%Y-%m-%d %H:%M:%S')
        return formatter.format(record)


def setup_logging(log_file: Path) -> logging.Logger:
    """Setup logging to both console and file."""
    logger = logging.getLogger('headers_gen')
    logger.setLevel(logging.DEBUG)

    # Console handler with colors
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(ColoredFormatter())

    # File handler without colors
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter('%(levelname)s %(asctime)s - %(message)s',
                          datefmt='%Y-%m-%d %H:%M:%S')
    )

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger

def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Package kernel headers for out-of-tree module compilation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s --pkgrel 2
  %(prog)s -p 3 --kpath /path/to/linux
        """
    )
    
    parser.add_argument(
        '-p', '--pkgrel',
        type=int,
        default=1,
        metavar='N',
        help='Package release number (must be a positive integer, default: 1)'
    )
    
    parser.add_argument(
        '-k', '--kpath',
        type=str,
        default="~/mnt-build/linux",
        metavar='PATH',
        help='Path to kernel source directory (default: ~/mnt-build/linux)'
    )
    
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose output (show all copy operations)'
    )
    
    args = parser.parse_args()
    
    if args.pkgrel < 1:
        parser.error("pkgrel must be a positive integer (>= 1)")
    
    return args

def read_kernel_release(src: Path) -> str:
    major = minor = patch = None
    mf = src / "Makefile"

    if not mf.exists():
        raise SystemExit(f"ERROR: Kernel Makefile not found: {mf}")

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

    if major is None or minor is None or patch is None:
        raise SystemExit(
            f"ERROR: Could not parse kernel version from {mf}. "
            f"Found: VERSION={major}, PATCHLEVEL={minor}, SUBLEVEL={patch}"
        )

    return f"{major}.{minor}.{patch}"

def prepare_kernel_headers(src: Path, logger: logging.Logger):
    """Run make prepare and make modules_prepare to generate all needed files."""
    env = os.environ.copy()
    env["ARCH"] = ARCH
    env["CROSS_COMPILE"] = CROSS_COMPILE
    
    num_jobs = os.cpu_count() or 1
    
    logger.info(f"Running 'make prepare' with ARCH={ARCH} CROSS_COMPILE={CROSS_COMPILE} -j{num_jobs}")
    result = subprocess.run(
        ["make", f"-j{num_jobs}", "prepare"],
        cwd=src,
        env=env,
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        logger.error(f"STDOUT: {result.stdout}")
        logger.error(f"STDERR: {result.stderr}")
        raise SystemExit(f"ERROR: 'make prepare' failed with exit code {result.returncode}")
    
    logger.info(f"Running 'make modules_prepare' with ARCH={ARCH} CROSS_COMPILE={CROSS_COMPILE} -j{num_jobs}")
    result = subprocess.run(
        ["make", f"-j{num_jobs}", "modules_prepare"],
        cwd=src,
        env=env,
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        logger.error(f"STDOUT: {result.stdout}")
        logger.error(f"STDERR: {result.stderr}")
        raise SystemExit(f"ERROR: 'make modules_prepare' failed with exit code {result.returncode}")
    
    logger.info(f"{Colors.GREEN}✓{Colors.RESET} Kernel headers prepared successfully")

def copy_selected(src: Path, dst: Path, relative: str, logger: logging.Logger):
    s = src / relative
    d = dst / relative
    if not s.exists():
        if VERBOSE:
            logger.warning(f"Skipping missing {s}")
        return
    if VERBOSE:
        logger.debug(f"Copying {s} → {d}")
    if s.is_dir():
        shutil.copytree(s, d, symlinks=True)
    else:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)


def copy_kconfig_files(src: Path, dst: Path, logger: logging.Logger):
    """Copy all Kconfig* files throughout the tree."""
    if VERBOSE:
        logger.info("Copying all Kconfig files")
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
        if VERBOSE:
            logger.debug(f"Copying {kconfig} → {dst_kconfig}")


def main():
    global VERBOSE
    
    args = parse_arguments()
    
    VERBOSE = args.verbose
    pkgrel = args.pkgrel
    KERNEL_SRC = Path(args.kpath).expanduser().resolve()
    
    # Determine version first so we can include it in the log filename
    if not KERNEL_SRC.exists():
        raise SystemExit(f"ERROR: Kernel source path does not exist: {KERNEL_SRC}")
    
    version = read_kernel_release(KERNEL_SRC)
    
    # Setup logging with timestamp and version in filename
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = Path(f"headers-gen-{version}-{timestamp}.log")
    logger = setup_logging(log_file)
    
    logger.info("=" * 60)
    logger.info("Starting kernel headers generation")
    logger.info(f"Package release: {pkgrel}")
    logger.info(f"Kernel source: {KERNEL_SRC}")
    logger.info(f"Kernel version: {version}")
    logger.info(f"Log file: {log_file}")
    logger.info("=" * 60)

    prepare_kernel_headers(KERNEL_SRC, logger)

    OUTPUT_TARBALL = Path(f"headers-{version}-{pkgrel}-mnt.tar.gz")
    staging_dir = Path("kernel_headers_staging")

    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir()

    logger.info("Copying required directories")
    for d in DIRS_TO_COPY:
        copy_selected(KERNEL_SRC, staging_dir, d, logger)

    logger.info("Copying required files")
    for f in FILES_TO_COPY:
        copy_selected(KERNEL_SRC, staging_dir, f, logger)

    copy_kconfig_files(KERNEL_SRC, staging_dir, logger)

    logger.info(f"Creating tarball: {OUTPUT_TARBALL}")
    with tarfile.open(OUTPUT_TARBALL, "w:gz") as tar:
        tar.add(staging_dir, arcname=f"linux-{version}")

    logger.info("=" * 60)
    logger.info(f"{Colors.GREEN}✓ Headers generation complete!{Colors.RESET}")
    logger.info(f"Output: {OUTPUT_TARBALL}")
    logger.info(f"Log file: {log_file}")
    if VERBOSE:
        logger.info("Staging directory: kernel_headers_staging")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
