# MNT Build

## 💻 About the MNT Reform Platforms

The MNT Reform platforms are a series of portable laptops built on the principles of open hardware and user freedom.

The team at [MNT Research](https://mntre.com/) has done an amazing job documenting their open hardware platforms, making this project possible.

## 🤷 What is MNT Build?

MNT Build turns tagged MNT Reform sources into bootable systems.

The MNT patch stack lives in [mnt-linux](https://git.sr.ht/~stephano/mnt-linux) as tags, one per stable release (`mnt-vX.Y.Z-reformN`). This repo pins a tag and builds it. It also builds the bootloaders (U-Boot or barebox), packages the out-of-tree modules, and assembles full disk images.

It does three jobs.

- **Build and package.** Kernel, DTBs, `reform2_lpc`, bootloaders and disk images for each supported board.
- **Carry the patch stack forward.** `dev-kernel` rebases the stack onto a new stable release, tags it, and checks that MNT's own branches have not moved ahead.
- **Stage bring-up work.** `xtra-patches/` and `xtra-dtbs/` hold changes not yet committed to `mnt-linux`. They are applied per build and removed afterwards.

It stays "distro-agnostic". The tarballs work on any distro. Arch Linux ARM is the test distro. It is the filesystem we use to check modules, headers (DKMS) and the U-Boot handoff.

Kernel changes belong in `mnt-linux`, not here.

## 🤹 Downloading & Booting
Releases contain the following artifacts:
- Linux kernel built from the MNT patch stack (`mnt-linux` tag), with its config
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

## 👷 Building and Installing

You'll need some tooling. See the [kernel requirements](https://www.kernel.org/doc/html/latest/process/changes.html).

Then run:
```bash
git clone https://github.com/cetola/mnt-build.git ~/mnt-build
cd ~/mnt-build
```

The first build clones [mnt-linux](https://git.sr.ht/~stephano/mnt-linux) into `mnt-linux/` and checks out the tag for the configured kernel version, e.g. `mnt-v7.2.10-reform1`. It also clones [reform-tools](https://source.mnt.re/reform/reform-tools) into `reform-tools/` at the release pinned by `REFORM_TOOLS_TAG` in `scripts/config.py`, and logs the commit it used.

To fetch both without building, run `./mnt-build setup`. It takes `--kversion` and `--localversion-rev` like `build`.

The first time you build, the config you need will probably not match the config in the repo. So at a minimum you'll want to build with the olddefconfig option:

```bash
./mnt-build build --olddefconfig
```

See --help for more options.

Once the build is complete, you'll get a kernel tarball containing the kernel, its config, and any `xtra-patches` that were applied. There will be a separate module tarball for `reform2_lpc`. If you want headers, use the `--with-headers` flag and it will generate a headers tarball. You can install all of this manually or use the PKGBUILDs in [Additional Tooling](#hammer-additional-tooling).

## 🔁 Moving to a New Kernel Release

The patch stack is maintained in `mnt-linux`. To carry it to a new stable release:

```bash
./mnt-build dev-kernel fetch
./mnt-build dev-kernel rebase --kversion X.Y.Z
./mnt-build build --skip-git-ops     # build and test the rebased branch
./mnt-build dev-kernel tag --kversion X.Y.Z
```

Then push the tag yourself and bump `DEFAULT_KERNEL_VERSION` in `scripts/config.py`. Tags are never moved. A changed stack on a released version gets the next `-reformN`.

## 🔨 Additional Tooling

| Upstream Package | AUR | Description |
| --- | --- | --- |
| [mnt-linux](https://git.sr.ht/~stephano/mnt-linux) | [linux-mnt-reform](https://aur.archlinux.org/packages/linux-mnt-reform-bin) | MNT Reform patched Linux kernel and headers. |
| [reform-tools](https://source.mnt.re/reform/reform-tools) | [reform-tools](https://aur.archlinux.org/packages/reform-tools) | Reform userland tools and scripts. |
| [MNT LPC module](https://source.mnt.re/reform/reform-tools) | [mnt-reform-lpc](https://aur.archlinux.org/packages/mnt-reform-lpc-dkms) | LPC system controller driver module used across Reform platforms. |

## 💾 Images  

`image-gen.sh` creates a bootable Arch Linux ARM disk image:

- Image name: `mnt-reform-<kernel-version>-aarch64.img`
- Layout (DOS partition table):
  - BOOT ext4 partition (1 GiB)
  - ROOT ext4 partition (sparse image, 110 GiB)
- Installs:
  - ArchLinuxARM base rootfs
    - linux-mnt-reform kernel, dtb, extlinux
    - mnt-reform-lpc
    - reform-tools
    - flash.bin (copy of u-boot)
- Bootloader handling:
  - Resolved from machine config (reform-tools/machines/*.conf) with fallback metadata
  - For SD-boot platforms, bootloader is also written into raw sectors at configured offsets
- Output: `mnt-reform-image-<sysimage>.tar` which contains the image, bmap file, SHA, and manifest

The kernel comes from a `mnt-vX.Y.Z-reformN` tag of [mnt-linux](https://git.sr.ht/~stephano/mnt-linux). The stack targets every MNT Reform platform. Whether a given board boots still depends on the correct DTB and a U-Boot setup that works for it.

I only release images for hardware that I can test. If you are willing to test other platforms / SoMs, [create an issue](https://github.com/cetola/mnt-build/issues) and I'll produce more images.

## 🪞👢 U-Boot &amp; 🐻📦 Barebox

U-Boot and Barebox commands are meant for helping you muck with the bootloader. Both are driven through the same `./mnt-build` interface. Simply swap the subcommand (`uboot` or `barebox`) and they behave identically: prepare a checkout, build, diff against the released artifact, menuconfig, or clean.

```bash
./mnt-build barebox --list sysimage
./mnt-build barebox --sysimage pocket-reform-system-rk3588 --dry-run
./mnt-build barebox --sysimage pocket-reform-system-rk3588 --clean
./mnt-build barebox --sysimage pocket-reform-system-rk3588 --menuconfig
./mnt-build barebox --sysimage pocket-reform-system-rk3588 # no options will build the bootloader
./mnt-build barebox add-remotes   # add the remotes listed under "mnt-reform-barebox" in remotes.json (nothing is fetched)
./mnt-build barebox fetch         # fetch every remote, and the tags from origin
```

The barebox checkout has two remotes. `origin` is MNT's `mnt-reform-barebox`, which the build checks out. `upstream` is Pengutronix's barebox, where the work goes before MNT pulls it into origin. Both commands need the checkout that `--dry-run` creates.

The two differences worth knowing:
- **Sysimage support**: U-Boot supports all sysimages; barebox is currently only available for the RK3588/RK3588S sysimages (`reform-next-system-rk3588`, `pocket-reform-system-rk3588`, `pocket-reform-system-rk3588s`).
- **`--reset`**: U-Boot-only. Resets the inner `u-boot/` sub-repo to the SHA `build.sh` expects (`./mnt-build uboot --sysimage <name> --reset`).

See `./mnt-build uboot --help` / `./mnt-build barebox --help` for all options and details.

## 🚀 FSBL

If you only need bootloader artifacts (without generating a full OS image), use `scripts/build-fsbl.sh`. It fetches a prebuilt bootloader or builds one from source for a target `--sysimage`, then prints the exact flash offsets (`seek`/`skip`) and `dd` command to use for SD.

Example:
```bash
./scripts/build-fsbl.sh --sysimage pocket-reform-system-a311d --mode source
sudo dd if=path/to/<flash.bin> of=/dev/sdX conv=notrunc bs=512 seek=<seek> skip=<skip>
```

See ./scripts/build-fsbl.sh --help for all options and details.

## ✏️ Notes

There is a container in the scripts directory if you happen to be building for Arch and care about toolchain skew.
