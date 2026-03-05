# SPDX-License-Identifier: MIT
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

DEFAULT_KERNEL_VERSION = '6.18.16'
DEFAULT_PKGREL = 1
DEFAULT_CROSS_COMPILE = "aarch64-linux-gnu-"

DTS_CONFIGS = [
    {
        "name": "imx8mp-mnt-pocket-reform.dts",
        "vendor": "freescale",
        "config": "CONFIG_ARCH_MXC"
    },
    {
        "name": "meson-g12b-bananapi-cm4-mnt-pocket-reform.dts",
        "vendor": "amlogic",
        "config": "CONFIG_ARCH_MESON"
    },
    {
        "name": "rk3588-mnt-pocket-reform.dts",
        "vendor": "rockchip",
        "config": "CONFIG_ARCH_ROCKCHIP"
    },
    {
        "name": "rk3588-mnt-reform-next.dts",
        "vendor": "rockchip",
        "config": "CONFIG_ARCH_ROCKCHIP"
    }
]


@dataclass
class BuildConfig:
    version: str
    build_dir: Path
    linux_dir: Path
    patches_dir: Path
    config_file: Path
    dtb_files: list[Path]
    output_tar: Path
    output_headers_tar: Path
    output_lpc_module_tar: Path
    output_wifi_module_tar: Path
    log_file: Path
    jobs: int
    pkgrel: int

    @classmethod
    def create(cls, version: str, build_dir: Optional[Path] = None,
               jobs: Optional[int] = None, pkgrel: Optional[int] = None):
        """Create build configuration with sensible defaults."""
        if build_dir is None:
            build_dir = Path.home() / "mnt-build"

        if jobs is None:
            jobs = os.cpu_count() or 4

        if pkgrel is None:
            pkgrel = DEFAULT_PKGREL

        linux_dir = build_dir / "linux"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        # Extract major.minor version (e.g., "6.17" from "6.17.8")
        version_parts = version.split('.')
        major_minor = f"{version_parts[0]}.{version_parts[1]}"

        # Generate DTB file paths from DTS_CONFIGS
        dtb_files = [
            linux_dir / f"arch/arm64/boot/dts/{dts_config['vendor']}/{dts_config['name'].replace('.dts', '.dtb')}"
            for dts_config in DTS_CONFIGS
        ]

        return cls(
            version=version,
            build_dir=build_dir,
            linux_dir=linux_dir,
            patches_dir=build_dir / "reform-debian-packages" / "linux" / f"patches{major_minor}",
            config_file=build_dir / "configs" / f"config-{version}-mnt-reform-arm64",
            dtb_files=dtb_files,
            output_tar=linux_dir / f"kernel-{version}-{pkgrel}-mnt.tar.gz",
            output_headers_tar=linux_dir / f"headers-{version}-{pkgrel}-mnt.tar.gz",
            output_lpc_module_tar=linux_dir / f"reform2_lpc-{version}-{pkgrel}-mnt.tar.gz",
            output_wifi_module_tar=linux_dir / f"wlan-{version}-{pkgrel}-mnt.tar.gz",
            log_file=build_dir / f"build-{version}-{timestamp}.log",
            jobs=jobs,
            pkgrel=pkgrel
        )
