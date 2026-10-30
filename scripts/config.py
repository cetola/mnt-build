# SPDX-License-Identifier: MIT
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple, Optional

DEFAULT_KERNEL_VERSION = '7.2.14'
DEFAULT_LOCALVERSION_NAME = 'reform'
DEFAULT_LOCALVERSION_REV = 1
DEFAULT_CROSS_COMPILE = "aarch64-linux-gnu-"
DEFAULT_KERNEL_ONLY = False
DEFAULT_KERNEL_DIR = "mnt-linux"
REMOTES_FILE = "remotes.json"
MNT_KERNEL_REMOTE = "mnt"
STABLE_KERNEL_REMOTE = "stable"
MNT_BASE_TRAILER = "MNT-Base"
REBASED_FROM_TRAILER = "Rebased-From"


def defconfig_name_for_arch(arch: str) -> str:
    return f"defconfig_{arch}"


def log_arch_name_for_arch(arch: str) -> str:
    arch_aliases = {
        "arm64": "aarch64",
    }
    return arch_aliases.get(arch, arch)


class KernelRemote(NamedTuple):
    name: str
    url: str
    push_url: Optional[str]
    # The remote the kernel checkout must have as "origin". It is the first
    # place to look for mnt-v{version} branches.
    is_origin: bool


def load_kernel_remotes(path: Path) -> list[KernelRemote]:
    """Read the kernel remotes from REMOTES_FILE.

    {"kernel": {"remotes": [
        {"name": ..., "url": ..., "push-url": ..., "is-origin": true}, ...]}}

    "push-url" and "is-origin" are optional. At most one remote may be the origin.
    """
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: not valid JSON: {e}")
    try:
        entries = data["kernel"]["remotes"]
    except (KeyError, TypeError):
        raise ValueError(f'{path}: expected {{"kernel": {{"remotes": [...]}}}}')
    if not isinstance(entries, list):
        raise ValueError(f'{path}: "kernel"."remotes" must be a list')

    types = {"name": str, "url": str, "push-url": str, "is-origin": bool}
    remotes: list[KernelRemote] = []
    for index, entry in enumerate(entries):
        where = f"{path}: kernel remote #{index + 1}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: expected an object")
        unknown = sorted(set(entry) - set(types))
        if unknown:
            raise ValueError(f"{where}: unknown key(s): {', '.join(unknown)}")
        for key in ("name", "url"):
            if key not in entry:
                raise ValueError(f'{where}: missing "{key}"')
        for key, value in entry.items():
            if not isinstance(value, types[key]):
                raise ValueError(f'{where}: "{key}" must be a {types[key].__name__}')
        if any(r.name == entry["name"] for r in remotes):
            raise ValueError(f'{where}: duplicate name "{entry["name"]}"')
        remotes.append(KernelRemote(
            entry["name"], entry["url"], entry.get("push-url"), entry.get("is-origin", False)
        ))

    origins = [r.name for r in remotes if r.is_origin]
    if len(origins) > 1:
        raise ValueError(f'{path}: more than one remote has "is-origin": true: {", ".join(origins)}')
    return remotes


def load_bootloader_url(path: Path, project: str) -> str:
    """Return the clone URL for a U-Boot or barebox project from REMOTES_FILE.

    {"bootloaders": {"<project>": {"url": ...}, ...}}
    """
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ValueError(f"{path}: not found")
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: not valid JSON: {e}")
    bootloaders = data.get("bootloaders") if isinstance(data, dict) else None
    if not isinstance(bootloaders, dict):
        raise ValueError(f'{path}: expected {{"bootloaders": {{"<project>": {{"url": ...}}}}}}')
    entry = bootloaders.get(project)
    if entry is None:
        raise ValueError(f'{path}: no "bootloaders" entry for "{project}"')
    where = f'{path}: bootloader "{project}"'
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object")
    unknown = sorted(set(entry) - {"url"})
    if unknown:
        raise ValueError(f"{where}: unknown key(s): {', '.join(unknown)}")
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError(f'{where}: "url" must be a non-empty string')
    return url


def version_key(version: str) -> Optional[tuple]:
    """Sort key for a kernel release. None for anything else, such as an -rc."""
    parts = version.split('.')
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def kernel_tag_name(version: str, localversion_rev: int) -> str:
    return f"mnt-v{version}-{DEFAULT_LOCALVERSION_NAME}{localversion_rev}"


_KERNEL_TAG_RE = re.compile(
    rf'^mnt-v(\d+\.\d+(?:\.\d+)?(?:-rc\d+)?)-{DEFAULT_LOCALVERSION_NAME}(\d+)$'
)


def parse_kernel_tag(name: str) -> Optional[tuple]:
    """(version, localversion_rev) of a kernel tag name, or None if it is not one."""
    match = _KERNEL_TAG_RE.match(name)
    return (match.group(1), int(match.group(2))) if match else None


def normalize_git_url(url: str) -> str:
    """Reduce a git URL to host/path so HTTPS and SSH forms of one repo compare equal."""
    url = url.strip()
    if '://' in url:
        url = url.split('://', 1)[1]
        url = url.split('@', 1)[-1] if '@' in url.split('/', 1)[0] else url
    elif '@' in url and ':' in url:
        # scp-like syntax: user@host:path
        host, path = url.split('@', 1)[1].split(':', 1)
        url = f"{host}/{path.lstrip('/')}"
    host, _, path = url.partition('/')
    path = path.rstrip('/')
    if path.endswith('.git'):
        path = path[:-4]
    return f"{host.lower()}/{path}"

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
    kernel: str
    build_dir: Path
    linux_dir: Path
    qcacld_dir: Path
    reform_tools_dir: Path
    xtra_patches_dir: Path
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
    tag: str

    @classmethod
    def create(cls, version: str, arch: str = "arm64",
               build_dir: Optional[Path] = None,
               jobs: Optional[int] = None,
               localversion_rev: Optional[int] = None,
               kernel: str = DEFAULT_KERNEL_DIR):
        if build_dir is None:
            build_dir = Path.home() / "mnt-build"

        if jobs is None:
            jobs = os.cpu_count() or 4

        if localversion_rev is None:
            localversion_rev = DEFAULT_LOCALVERSION_REV

        linux_dir = build_dir / kernel
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        localversion = f"-{DEFAULT_LOCALVERSION_NAME}{localversion_rev}"
        kernel_release = f"{version}{localversion}"
        build_version = f"{version}.{DEFAULT_LOCALVERSION_NAME}{localversion_rev}"
        log_build_version = (
            f"{version}_{log_arch_name_for_arch(arch)}."
            f"{DEFAULT_LOCALVERSION_NAME}{localversion_rev}"
        )

        version_parts = version.split('.')
        major_minor = f"{version_parts[0]}.{version_parts[1]}"

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
            kernel=kernel,
            build_dir=build_dir,
            linux_dir=linux_dir,
            qcacld_dir=build_dir / "qcacld2",
            reform_tools_dir=build_dir / "reform-tools",
            xtra_patches_dir=build_dir / "xtra-patches" / major_minor,
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
            localversion_rev=localversion_rev,
            tag=kernel_tag_name(version, localversion_rev),
        )

    def failed_patch_log(self, suffix: str = "") -> Path:
        return self.build_dir / f"patch-failures{suffix}-{self.log_build_version}-{self.timestamp}.log"
