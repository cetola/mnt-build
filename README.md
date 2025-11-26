<img src="https://github.com/user-attachments/assets/89621d81-83b3-4d57-bd69-2859dc4e0123" alt="mnt_pocket_arch" width="600">

# Kernel Build for MNT Pocket Reform

## Motivation

I am writing software for the MNT Pocket Reform in support of some custom hardware I'm building. I wanted to learn about how the bootloader, firmware, initramfs, and OS worked on the laptop. Getting Arch Linux running on it seemed like a reasonable way to do this, at the time. 🤷

## About the MNT Pocket Reform

The MNT Pocket Reform is a compact, portable laptop built on the principles of open hardware and user freedom.

At launch, the Pocket Reform shiped with the NXP i.MX8M Plus SoC, a quad-core ARM Cortex-A53 processor paired with a Cortex-M7 real-time core. This is the only SoC supported now, but I plan to support the rk3588 module as well.

The team at MNT Research has done an amazing job documenting their open hardware platform, making this project possible. 🙌

## Getting Started

You'll need some tooling:

```python
required_tools = ['git', 'make', 'tar', 'aarch64-linux-gnu-gcc', 'patch']
```

Then run:
```bash
git clone https://github.com/cetola/mnt-build.git
cd mnt-build
git submodule update --init --recursive
```

The first time you build, your config will not be correct. I'm still working on a portable way of doing this in the script. For now, run this:

```bash
cd linux
cp ../configs/defconfig .config
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- olddefconfig
cp .config ../configs/config-6.17.9-mnt-reform-arm64 ##Or whatever release you are on
cd ..
./scripts/build.py
```

You'll end up with a tarball of the kernel for the tag you selected. You can install it manually or use the PKGBUILD in [Additional Tooling](#additional-tooling).

If you want headers for building out of tree modules:
```bash
./scripts/header-gen.py
```

Again, you can install manually, or use the [Additional Tooling](#additional-tooling).

## Additional Tooling

If you want to install this "the Arch way" you can use [this PKGBUILD project](https://github.com/cetola/linux-mnt-pocket).

Likewise for the kernel headers you can use [this PKGBUILD](https://github.com/cetola/linux-mnt-pocket-headers).

## Notes

This is very much a work in progress. Do not try to use this unless you are on a release tag. Even then, YMMV.

These scripts are an automation of a full guide that I posted on the [MNT Community Forum](https://community.mnt.re/t/guide-how-to-arch-linux-on-the-pocket-reform/3918). See there for more details. See the [Arch Linux Arm](https://archlinuxarm.org/) site to grab a filesystem.
