#!/bin/bash
# SPDX-License-Identifier: MIT
export MNTVER=6.17.7
cd ~/mnt-build/linux
git fetch --tags
git reset --hard HEAD
git clean -fd
git checkout -b pocket-reform-$MNTVER tags/v$MNTVER
mnt-patch.sh
cp /home/stephano/mnt-build/configs/config-$MNTVER-mnt-reform-arm64 .config
make olddefconfig ARCH=arm64
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -j$(nproc) Image modules dtbs
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- modules_install INSTALL_MOD_PATH=./modules -j$(nproc)
cd ~/mnt-build/reform-tools/lpc
git pull 
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- -C ../../linux/ M=$(pwd) -j$(nproc)
cd ~/mnt-build/qcacld2
git pull 
./build
cd ~/mnt-build/linux/
tar czf kernel-$MNTVER-mnt.tar.gz   arch/arm64/boot/Image   ../configs/imx8mp-mnt-pocket-reform-$MNTVER.dtb ../reform-tools/lpc/reform2_lpc.ko ../qcacld2/wlan.ko   -C modules lib/modules
echo "kernel-$MNTVER-mnt.tar.gz ready."
