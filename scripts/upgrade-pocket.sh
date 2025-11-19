#!/bin/bash
#Run this from the target machine from a directory with the tarball in it.
export MNTVER=6.17.8
export KVER=$MNTVER-dirty
tar -xvzf kernel-$MNTVER-mnt.tar.gz
echo "Copy kernel modules: $KVER"
sudo cp -r lib/modules/$KVER /lib/modules/
sudo mkdir /lib/modules/$KVER/extra
sudo cp reform2_lpc.ko /lib/modules/$KVER/extra/
sudo cp wlan.ko /lib/modules/$KVER/extra/
sync && sync
echo "Backup old image, initramfs, and dtb"
sudo cp /boot/Image-testing /boot/Image-old
sudo cp /boot/initramfs-linux-testing /boot/initramfs-linux-old
sudo cp /boot/imx8mp-mnt-pocket-reform.dtb /boot/imx8mp-mnt-pocket-reform-old.dtb
sync && sync
echo "Building dracut for kernel: $KVER"
sudo dracut -f -v --kver "$KVER" --no-hostonly
sync && sync
echo "Install new kernel, initramfs, and dtb"
sudo cp arch/arm64/boot/Image /boot/Image-testing
sudo cp imx8mp-mnt-pocket-reform-$MNTVER.dtb /boot/imx8mp-mnt-pocket-reform.dtb
sudo mv /boot/initramfs-$KVER.img /boot/initramfs-linux-testing
sync && sync
sudo depmod $KVER
sync && sync
echo "Done. Reboot and good luck."
