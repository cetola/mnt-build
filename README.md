# Kernel Build for MNT Reform

## :computer: About the MNT Reform Platforms

The MNT Reform platforms are a series of portable laptops built on the principles of open hardware and user freedom.

The team at [MNT Research](https://mntre.com/) has done an amazing job documenting their open hardware platforms, making this project possible.

## :shrug: What is MNT Build?

This repo attempts to build the Linux kernel and associated required artifacts for the MNT Reform platforms. It focuses on a "distro-agnostic" approach, so that you can use the kernel in whatever distro you choose. It also focuses on extensibility, so that you can integrate the scripts into your workflow.

As we need to test that it actually works, we use Arch Linux Arm as the the testing distro. That's also the filesytem we use to test the kernel modules, headers (DKMS), and the booting process (u-boot handoff).

## :shipit: Downloading & Booting
Releases will have both a kernel and Linux headers zip. The `image-gen.sh` script uses the kernel generated from a release to create a 120G sparse image & bmap file `mnt-reform-[ver]-aarch64.img.zst[.bmap]`. See [the docs](https://github.com/yoctoproject/bmaptool) for more info on bmap-tool.

```bash
sudo bmaptool copy path/to/mnt-reform-[ver]-aarch64.img.zst /dev/sdX
```
You'll boot into an Arch Linux ARM filesystem. Users include `root` and `alarm`. Passwords are the same as the username.

## :construction_worker: Building and Installing

You'll need some tooling:

```python
required_tools = ['git', 'make', 'tar', 'aarch64-linux-gnu-gcc', 'patch']
```

Then run:
```bash
git clone https://github.com/cetola/mnt-build.git ~/mnt-build
cd ~/mnt-build
git submodule update --init --recursive
```

The first time you build, the config you need will probably not match the config in the repo. So at a minimum you'll want to build with the olddefconfig option:

```bash
./scripts/build.py --olddefconifg
```

See --help for more options.

Once the build is complete, you'll get a kernel tarball containing the kernel, config, and firmware, plus separate module tarballs for `reform2_lpc` and `wlan` (one `.tar.gz` per module). If you want headers use the ```--with-headers``` flag. You can install all of this manually or use the PKGBUILDs in [Additional Tooling](#additional-tooling).

## :hammer: Additional Tooling

There are PKGBUILDs for both the [kernel](https://github.com/cetola/linux-mnt-reform) and [kernel headers](https://github.com/cetola/linux-mnt-reform-headers). There's also a an attempt to install [reform-tools](https://source.mnt.re/reform/reform-tools) called [mnt-reform-tools](https://github.com/cetola/mnt-reform-tools). PRs welcome there. Likewise there are PKGBUILDs for [Qualcomm's WiFi](https://github.com/cetola/mnt-reform-qcacld2) used in the i.MX8M Plus SoM and for the [Reform LPC driver](https://github.com/cetola/mnt-reform-lpc) which I believe is required on all platforms.

## :pencil2: Notes

This is very much a work in progress. Do not try to build unless you are on a release tag. Even then, YMMV.

There is a container in the scripts directory if you happen to be building for Arch and care about toolchain skew.

These scripts are an automation of a full guide that I posted on the [MNT Community Forum](https://community.mnt.re/t/guide-how-to-arch-linux-on-the-pocket-reform/3918). See there for more details. See the [Arch Linux Arm](https://archlinuxarm.org/) site to grab a filesystem and install manually.

If you use one of the provided images, know I have only tested on the Amlogic A311D MNT Pocket Reform. That being said, the kernel is patched with all patches from reform-debian-packages -> linux -> patches[ver]. As such it should boot on any MNT Reform platform. Be sure you have the correct device tree installed.

The provided images will not install a DTB (for now), so you'll need to decide which you want and edit the sd card manually in ```/boot/extlinux/extlinux.conf```. If you use the PKGBUILD it will only install a limited number of DTB files, namely the ones I can test. If you want me to add more feel free to add an issue, or better yet, a PR.
