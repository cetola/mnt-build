# Kernel Build for MNT Reform

## :computer: About the MNT Reform Platforms

The MNT Reform platforms are a series of portable laptops built on the principles of open hardware and user freedom.

The team at [MNT Research](https://mntre.com/) has done an amazing job documenting their open hardware platforms, making this project possible.

## :shrug: What is MNT Build?

MNT Build is a simple build system for the Linux kernel and associated required artifacts for the MNT Reform platforms. It focuses on a "distro-agnostic" approach, so that you can use the kernel in whatever distro you choose. It also focuses on extensibility, so that you can integrate the scripts into your workflow.

As we need to test that it actually works, we use Arch Linux ARM as the testing distro. That's also the filesystem we use to test the kernel modules, headers (DKMS), and the booting process (u-boot handoff).

## :shipit: Downloading & Booting
Releases will contain the following artifacts:
- MNT Reform patched Linux kernel
- Linux headers for building out of tree modules
- MNT's LPC module
- QCOM's Wi-Fi module (i.MX8M Plus)

Releases with system images for SD Card are available at [mntar.ch](https://mntar.ch/). These releases will include everything listed above as well as: System images, bmap files, and SHAs for tested platforms.

See [the docs](https://github.com/yoctoproject/bmaptool) for more info on `bmaptool`, and see [Images](#floppy_disk-images) for details on which images you can download.

```bash
sudo bmaptool copy \
  https://github.com/cetola/mnt-build/releases/download/[kernel_ver]-[arch]/arch-sys-[sysimage]-[kernel_ver].img.zst \
  /dev/sdX
```

Current supported sysimage values are:
- pocket-reform-system-a311d
- reform-next-system-rk3588
- pocket-reform-system-rk3588
- pocket-reform-system-rk3588s
- pocket-reform-system-imx8mp

You'll boot into an Arch Linux ARM filesystem. Users include `root` and `alarm`. Passwords are the same as the username.

## :construction_worker: Building and Installing

You'll need some tooling. See the [kernel requirements](https://www.kernel.org/doc/html/latest/process/changes.html).

Then run:
```bash
git clone https://github.com/cetola/mnt-build.git ~/mnt-build
cd ~/mnt-build
# Checkout a release tag
git submodule update --init --recursive
```

The first time you build, the config you need will probably not match the config in the repo. So at a minimum you'll want to build with the olddefconfig option:

```bash
./mnt-build build --olddefconfig
```

See --help for more options.

Once the build is complete, you'll get a kernel tarball containing the kernel, config, and all patches that were applied. There will be separate module tarballs for `reform2_lpc` and `wlan`. If you want headers, use the `--with-headers` flag and it will generate a headers tarball. You can install all of this manually or use the PKGBUILDs in [Additional Tooling](#hammer-additional-tooling).

## :hammer: Additional Tooling

| Upstream Package | AUR | Description |
| --- | --- | --- |
| [Linux stable kernel](https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/) | [linux-mnt-reform](https://aur.archlinux.org/packages/linux-mnt-reform-bin) | MNT Reform patched Linux kernel and headers. |
| [reform-tools](https://source.mnt.re/reform/reform-tools) | [reform-tools](https://aur.archlinux.org/packages/reform-tools) | Reform userland tools and scripts. |
| [QCOM WiFi module](https://source.mnt.re/reform/qcacld2) | [mnt-reform-qcacld2](https://aur.archlinux.org/packages/mnt-reform-qcacld2-dkms) | Out-of-tree WiFi module used on i.MX8M Plus SoMs. |
| [MNT LPC module](https://source.mnt.re/reform/reform-tools) | [mnt-reform-lpc](https://aur.archlinux.org/packages/mnt-reform-lpc-dkms) | LPC system controller driver module used across Reform platforms. |

## :floppy_disk: Images  

`image-gen.sh` creates a bootable Arch Linux ARM disk image:

- Image name: `mnt-reform-<kernel-version>-aarch64.img`
- Layout (DOS partition table):
  - BOOT ext4 partition (1 GiB)
  - ROOT ext4 partition (sparse image, 110 GiB)
- Installs:
  - ArchLinuxARM base rootfs
    - linux-mnt-reform kernel, dtb, extlinux
    - mnt-reform-qcacld2
    - mnt-reform-lpc
    - reform-tools
    - flash.bin (copy of u-boot)
- Bootloader handling:
  - Resolved from machine config (reform-tools/machines/*.conf) with fallback metadata
  - For SD-boot platforms, bootloader is also written into raw sectors at configured offsets
- Output: `mnt-reform-image-<sysimage>.tar` which contains the image, bmap file, SHA, and manifest

I will currently only release images for hardware that I can test. So today, that's the Pocket Reform with the A311D SoM. If you are willing to test other platforms / SoMs, [create an issue](https://github.com/cetola/mnt-build/issues) and I'll produce more images.

The kernel is patched with all patches from `reform-debian-packages/linux/patches[ver]`. As such, it should boot on any MNT Reform platform, provided you use the correct DTB and have a U-Boot setup that works for your system.

## :mirror::boot: U-Boot &amp; :bear::package: Barebox

U-Boot and Barebox commands are meant for helping you muck with the bootloader. Both are driven through the same `./mnt-build` interface. Simply swap the subcommand (`uboot` or `barebox`) and they behave identically: prepare a checkout, build, diff against the released artifact, menuconfig, or clean.

```bash
./mnt-build barebox --list sysimage
./mnt-build barebox --sysimage pocket-reform-system-rk3588 --dry-run
./mnt-build barebox --sysimage pocket-reform-system-rk3588 --clean
./mnt-build barebox --sysimage pocket-reform-system-rk3588 --menuconfig
./mnt-build barebox --sysimage pocket-reform-system-rk3588 # no options will build the bootloader
```

The two differences worth knowing:
- **Sysimage support**: U-Boot supports all sysimages; barebox is currently only available for the RK3588/RK3588S sysimages (`reform-next-system-rk3588`, `pocket-reform-system-rk3588`, `pocket-reform-system-rk3588s`).
- **`--reset`**: U-Boot-only. Resets the inner `u-boot/` sub-repo to the SHA `build.sh` expects (`./mnt-build uboot --sysimage <name> --reset`).

See `./mnt-build uboot --help` / `./mnt-build barebox --help` for all options and details.

## :rocket: FSBL

If you only need bootloader artifacts (without generating a full OS image), use `scripts/build-fsbl.sh`. It fetches a prebuilt bootloader or builds one from source for a target `--sysimage`, then prints the exact flash offsets (`seek`/`skip`) and `dd` command to use for SD.

Example:
```bash
./scripts/build-fsbl.sh --sysimage pocket-reform-system-a311d --mode source
sudo dd if=path/to/<flash.bin> of=/dev/sdX conv=notrunc bs=512 seek=<seek> skip=<skip>
```

See ./scripts/build-fsbl.sh --help for all options and details.

## :pencil2: Notes

There is a container in the scripts directory if you happen to be building for Arch and care about toolchain skew.
