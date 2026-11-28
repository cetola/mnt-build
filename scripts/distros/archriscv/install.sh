#!/usr/bin/env bash
# Runs inside the archriscv rootfs (qemu-riscv64 chroot on non-riscv hosts).
set -euo pipefail

# shellcheck source=/dev/null
. /tmp/mnt-kernel.env
: "${KREL:?}" "${DTB:?}"

PACMAN=(pacman --disable-sandbox)
EXTLINUX_EXAMPLE="/tmp/linux-mnt-reform-bin.extlinux.conf.example"

pacman_retry() {
  local attempt
  for attempt in 1 2; do
    if "$@"; then
      return 0
    fi
    echo "pacman failed (attempt $attempt/2); refreshing databases..."
    rm -rf /var/lib/pacman/sync/*
    "${PACMAN[@]}" -Syy --noconfirm || true
    sleep 2
  done
  return 1
}

echo "Initializing pacman keyring..."
pacman-key --init
pacman-key --populate archlinux

"${PACMAN[@]}" -Syy --noconfirm
echo "Upgrading base system..."
pacman_retry "${PACMAN[@]}" -Syu --noconfirm
echo "Installing essential packages..."
pacman_retry "${PACMAN[@]}" -S --needed --noconfirm base dracut networkmanager sudo openssh

[[ -f "/usr/lib/modules/$KREL/modules.order" ]] || { echo "kernel modules for $KREL missing" >&2; exit 1; }
echo "Running depmod for $KREL..."
depmod -a "$KREL"

echo "Building initramfs for $KREL..."
dracut --force --no-hostonly --kver "$KREL" "/boot/initramfs-$KREL.img"
[[ -s "/boot/initramfs-$KREL.img" ]] || { echo "dracut produced no initramfs" >&2; exit 1; }

echo "Setting root password (root)..."
echo 'root:root' | chpasswd
echo '%wheel ALL=(ALL:ALL) ALL' > /etc/sudoers.d/10-wheel
chmod 0440 /etc/sudoers.d/10-wheel

echo "Configuring network services (NetworkManager only)..."
systemctl disable systemd-networkd.service systemd-networkd.socket \
  systemd-networkd-wait-online.service systemd-resolved.service 2>/dev/null || true
systemctl enable NetworkManager.service sshd.service
rm -f /etc/resolv.conf
ln -s /run/NetworkManager/resolv.conf /etc/resolv.conf

echo "Writing extlinux example..."
cat > "$EXTLINUX_EXAMPLE" <<EOF
DEFAULT mnt
TIMEOUT 30
PROMPT 1
MENU TITLE MNT Reform

LABEL mnt
  MENU LABEL mnt-linux $KREL
  KERNEL /Image-$KREL
  INITRD /initramfs-$KREL.img
  FDT /dtbs/$DTB
  APPEND root=LABEL=ROOT rootwait $BOOTARGS
EOF

echo "$KREL" > /tmp/linux-mnt-reform-bin.version

echo "Cleaning pacman package cache..."
rm -rf /var/cache/pacman/pkg/* /var/lib/pacman/sync/*
rm -f /var/lib/pacman/db.lck /tmp/mnt-kernel.env
ls -lh /boot/
