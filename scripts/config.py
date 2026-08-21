# SPDX-License-Identifier: MIT
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

DEFAULT_KERNEL_VERSION = '7.1.9'
DEFAULT_LOCALVERSION_NAME = 'reform'
DEFAULT_LOCALVERSION_REV = 2
DEFAULT_CROSS_COMPILE = "aarch64-linux-gnu-"
DEFAULT_KERNEL_ONLY = False


def defconfig_name_for_arch(arch: str) -> str:
    return f"defconfig_{arch}"


def log_arch_name_for_arch(arch: str) -> str:
    """Return the user-facing architecture label used in build log filenames."""
    arch_aliases = {
        "arm64": "aarch64",
    }
    return arch_aliases.get(arch, arch)

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

VENDOR_CONFIG_MAP = {
    "rockchip": "CONFIG_ARCH_ROCKCHIP",
    "freescale": "CONFIG_ARCH_MXC",
    "amlogic":   "CONFIG_ARCH_MESON",
}

# Upstream DTBs built by make dtbs that should be verified and packaged
# alongside the custom DTBs, without requiring any source copying.
EXTRA_DTB_PATHS = [
    "arch/arm64/boot/dts/rockchip/rk3588s-radxa-cm5-io.dtb",
]


@dataclass
class BuildConfig:
    version: str
    build_version: str
    kernel_release: str
    localversion: str
    arch: str
    build_dir: Path
    linux_dir: Path
    qcacld_dir: Path
    reform_tools_dir: Path
    patches_dir: Path
    xtra_patches_dir: Path
    mnt_overrides_dir: Path
    xtra_dtbs_dir: Path
    defconfig_file: Path
    config_file: Path
    dtb_files: list[Path]
    output_tar: Path
    output_headers_tar: Path
    output_lpc_module_tar: Path
    output_wifi_module_tar: Path
    log_file: Path
    log_build_version: str
    timestamp: str
    jobs: int
    localversion_rev: int

    @classmethod
    def create(cls, version: str, arch: str = "arm64",
               build_dir: Optional[Path] = None,
               jobs: Optional[int] = None,
               localversion_rev: Optional[int] = None):
        """Create build configuration with sensible defaults."""
        if build_dir is None:
            build_dir = Path.home() / "mnt-build"

        if jobs is None:
            jobs = os.cpu_count() or 4

        if localversion_rev is None:
            localversion_rev = DEFAULT_LOCALVERSION_REV

        linux_dir = build_dir / "linux"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        localversion = f"-{DEFAULT_LOCALVERSION_NAME}{localversion_rev}"
        kernel_release = f"{version}{localversion}"
        build_version = f"{version}.{DEFAULT_LOCALVERSION_NAME}{localversion_rev}"
        log_build_version = (
            f"{version}_{log_arch_name_for_arch(arch)}."
            f"{DEFAULT_LOCALVERSION_NAME}{localversion_rev}"
        )

        # Extract major.minor version (e.g., "6.17" from "6.17.8")
        version_parts = version.split('.')
        major_minor = f"{version_parts[0]}.{version_parts[1]}"

        # DTBs are only relevant for the current ARM64 Reform kernel flow.
        dtb_files = []
        xtra_dtbs_dir = build_dir / "xtra-dtbs"
        if arch == "arm64":
            dtb_files = [
                linux_dir / f"arch/arm64/boot/dts/{dts_config['vendor']}/{dts_config['name'].replace('.dts', '.dtb')}"
                for dts_config in DTS_CONFIGS
            ]
            if xtra_dtbs_dir.exists():
                for vendor_dir in sorted(xtra_dtbs_dir.iterdir()):
                    if vendor_dir.is_dir() and vendor_dir.name in VENDOR_CONFIG_MAP:
                        for dts_file in sorted(vendor_dir.glob("*.dts")):
                            dtb_files.append(
                                linux_dir / f"arch/arm64/boot/dts/{vendor_dir.name}/{dts_file.stem}.dtb"
                            )
            for extra_path in EXTRA_DTB_PATHS:
                dtb_files.append(linux_dir / extra_path)

        return cls(
            version=version,
            build_version=build_version,
            kernel_release=kernel_release,
            localversion=localversion,
            arch=arch,
            build_dir=build_dir,
            linux_dir=linux_dir,
            qcacld_dir=build_dir / "qcacld2",
            reform_tools_dir=build_dir / "reform-tools",
            patches_dir=build_dir / "reform-debian-packages" / "linux" / f"patches{major_minor}",
            xtra_patches_dir=build_dir / "xtra-patches" / major_minor,
            mnt_overrides_dir=build_dir / "xtra-patches" / "mnt-overrides" / major_minor,
            xtra_dtbs_dir=xtra_dtbs_dir,
            defconfig_file=build_dir / "configs" / defconfig_name_for_arch(arch),
            config_file=build_dir / "configs" / f"config-{version}-mnt-reform-{arch}",
            dtb_files=dtb_files,
            output_tar=linux_dir / f"kernel-{build_version}.tar.gz",
            output_headers_tar=linux_dir / f"headers-{build_version}.tar.gz",
            output_lpc_module_tar=linux_dir / f"reform2_lpc-{build_version}.tar.gz",
            output_wifi_module_tar=linux_dir / f"wlan-{build_version}.tar.gz",
            log_file=build_dir / f"build-{log_build_version}-{timestamp}.log",
            log_build_version=log_build_version,
            timestamp=timestamp,
            jobs=jobs,
            localversion_rev=localversion_rev
        )

    def failed_patch_log(self, suffix: str = "") -> Path:
        """Path for a patch-failure log, named/located like the main build log."""
        return self.build_dir / f"patch-failures{suffix}-{self.log_build_version}-{self.timestamp}.log"
