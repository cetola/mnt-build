# Kernel Build for MNT Pocket Reform

## :computer: About the MNT Pocket Reform

The MNT Pocket Reform is a compact, portable laptop built on the principles of open hardware and user freedom.

At launch, the Pocket Reform shiped with the NXP i.MX8 M Plus SoC, a quad-core ARM Cortex-A53 processor paired with a Cortex-M7 real-time core.

The team at [MNT Research](https://mntre.com/) has done an amazing job documenting their open hardware platform, making this project possible. 🙌

## :shipit: Downloading & Booting
Releases will have both a kernel and Linux headers zip. The `image-gen.sh` script uses the kernel generated from a release to create a 120G sparse image & bmap file `mnt-pocket-[ver]-aarch64.img.zst[.bmap]`. See [the docs](https://github.com/yoctoproject/bmaptool) for more info on bmap-tool.

```bash
sudo bmaptool copy path/to/mnt-pocket-[ver]-aarch64.img.zst /dev/sdX
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

Once the build is complete, you'll end up with a tarball containing the kernel, config, and some firmware. You can install these manually or use the PKGBUILD in [Additional Tooling](#additional-tooling).

If you want headers for building out of tree modules:
```bash
./scripts/header-gen.py
```

Again, you can install manually, or use the [Additional Tooling](#additional-tooling).

## :hammer: Additional Tooling

There are PKGBUILDs for both the [kernel](https://github.com/cetola/linux-mnt-pocket) and [kernel headers](https://github.com/cetola/linux-mnt-pocket-headers).

## :pencil2: Notes

This is very much a work in progress. Do not try to build unless you are on a release tag. Even then, YMMV.

There is a container in the scripts directory if you happen to be building for Arch and care about toolchain skew.

These scripts are an automation of a full guide that I posted on the [MNT Community Forum](https://community.mnt.re/t/guide-how-to-arch-linux-on-the-pocket-reform/3918). See there for more details. See the [Arch Linux Arm](https://archlinuxarm.org/) site to grab a filesystem and install manually.

If you use one of the provided images, know I have only tested on the i.MX8 M Plus MNT Pocket Reform. That being said, the kernel is patched with all patches from reform-debian-packages -> linux -> patches[ver]. As such it should boot on any platform.

## :rocket: Next Steps

The kernel tarball and accompanying PKGBUILD will install the reform2_lpc and wlan (qcacld) out-of-tree kernel modules for the i.MX8M Plus. That is a rather small but necessary part of [reform-tools](https://source.mnt.re/reform/reform-tools). Porting the rest of these tools to Arch will make the experience much easier. Perhaps not the point of Arch, but hey, one has to have goals. :grinning:
