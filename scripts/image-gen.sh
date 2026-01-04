#!/usr/bin/env bash
set -euo pipefail

KVER="6.18.3"
PKGREL="1"
KERNEL_VERSION="${KVER}-mnt-pocket"
IMAGE="$(pwd)/mnt-pocket-${KVER}-aarch64.img"
IMAGE_SIZE_GB=5
BOOT_SIZE_MB=1024
WORKDIR="$(pwd)/image-gen"
DOWNLOADS="$WORKDIR/downloads"
MOUNTDIR="$WORKDIR/mnt"
BOOT_MNT="$MOUNTDIR/boot"
ROOT_MNT="$MOUNTDIR/root"

POCKET_URL="https://github.com/cetola/linux-mnt-pocket/archive/refs/tags/${KVER}-${PKGREL}-mnt-pocket.tar.gz"
ARCH_URL="http://os.archlinuxarm.org/os/ArchLinuxARM-aarch64-latest.tar.gz"

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
LOGFILE="$(pwd)/image-gen-${KVER}-${TIMESTAMP}.log"

exec > >(tee -a "$LOGFILE") 2>&1

echo "Logging to: $LOGFILE"
echo "Started at: $(date)"
echo

echo "Checking for required tools..."
MISSING_TOOLS=()

command -v dd >/dev/null 2>&1 || MISSING_TOOLS+=("coreutils")
command -v parted >/dev/null 2>&1 || MISSING_TOOLS+=("parted")
command -v losetup >/dev/null 2>&1 || MISSING_TOOLS+=("util-linux")
command -v mkfs.ext4 >/dev/null 2>&1 || MISSING_TOOLS+=("e2fsprogs")
command -v curl >/dev/null 2>&1 || MISSING_TOOLS+=("curl")
command -v tar >/dev/null 2>&1 || MISSING_TOOLS+=("tar")
command -v chroot >/dev/null 2>&1 || MISSING_TOOLS+=("arch-install-scripts")

# Check for qemu-aarch64-static (required for x86_64 -> aarch64 chroot)
if [[ ! -f /usr/bin/qemu-aarch64-static ]]; then
  MISSING_TOOLS+=("qemu-user-static qemu-user-static-binfmt")
fi

if [[ ${#MISSING_TOOLS[@]} -gt 0 ]]; then
  echo "ERROR: Missing required tools/packages:"
  for tool in "${MISSING_TOOLS[@]}"; do
    echo "  - $tool"
  done
  echo
  echo "Install them with:"
  echo "  sudo pacman -S ${MISSING_TOOLS[*]}"
  exit 1
fi

echo "All required tools found."
echo

if [[ $EUID -ne 0 ]]; then
  echo "This script must be run as root."
  exit 1
fi

cleanup() {
  set +e
  # Note: unshare -m creates a private mount namespace, so mounts inside
  # the chroot don't need cleanup - they're automatically cleaned up when
  # the namespace exits. We only clean up mounts we created before chroot.
  umount "$ROOT_MNT/boot" 2>/dev/null || true
  umount "$ROOT_MNT" 2>/dev/null || true
  losetup -D 2>/dev/null || true
}
trap cleanup EXIT

mkdir -p "$DOWNLOADS" "$BOOT_MNT" "$ROOT_MNT"

echo "Creating disk image..."
dd if=/dev/zero of="$IMAGE" bs=1M count=$((IMAGE_SIZE_GB * 1024)) status=progress

echo "Partitioning image..."
parted --script "$IMAGE" \
  mklabel msdos \
  mkpart primary ext4 1MiB "$((BOOT_SIZE_MB + 1))"MiB \
  mkpart primary ext4 "$((BOOT_SIZE_MB + 1))"MiB 100%

# Attach AFTER partitioning; ask kernel to scan partitions immediately
LOOPDEV="$(losetup --find --show --partscan "${IMAGE}")"
echo "Using loop device: ${LOOPDEV}"

# Let udev create /dev/loopXp1 nodes
udevadm settle || true
sleep 1

# Optional: nudge the kernel to re-read partition table (best effort)
partprobe "${LOOPDEV}" || true
udevadm settle || true
sleep 1

# Wait until partition nodes exist (handles slow runners)
for i in {1..20}; do
  if [[ -b "${LOOPDEV}p1" && -b "${LOOPDEV}p2" ]]; then
    break
  fi
  sleep 0.2
  udevadm settle || true
done

# Hard fail with useful diagnostics if still missing
if [[ ! -b "${LOOPDEV}p1" || ! -b "${LOOPDEV}p2" ]]; then
  echo "ERROR: partition nodes not created for ${LOOPDEV}"
  ls -l "${LOOPDEV}"* || true
  ls -l /dev/loop* || true
  cat /proc/partitions | grep -E 'loop|mapper' || true
  exit 1
fi

echo "Using loop device: $LOOPDEV"

BOOT_PART="${LOOPDEV}p1"
ROOT_PART="${LOOPDEV}p2"

echo "Formatting partitions..."
mkfs.ext4 -F -L BOOT "$BOOT_PART"
mkfs.ext4 -F -L ROOT "$ROOT_PART"

mount "$BOOT_PART" "$BOOT_MNT"
mount "$ROOT_PART" "$ROOT_MNT"

cd "$DOWNLOADS"

download_if_missing() {
  local url="$1"
  local output="$2"
  
  if [[ -f "$output" ]]; then
    echo "Using cached $output"
  else
    echo "Downloading $output..."
    curl -L -o "$output" "$url"
  fi
}

download_if_missing "$POCKET_URL" "pocket.tar.gz"
download_if_missing "$ARCH_URL" "archlinuxarm.tar.gz"

echo "Extracting ArchLinuxARM root filesystem..."
tar -xpf archlinuxarm.tar.gz -C "$ROOT_MNT"

echo "Extracting linux-mnt-pocket..."
mkdir -p "$WORKDIR/pocket"
tar --no-same-owner -xpf pocket.tar.gz -C "$WORKDIR/pocket"

echo "Creating /etc/fstab..."
cat > "$ROOT_MNT/etc/fstab" << 'EOF'
# <source> <mountpoint> <fstype> <options> <dump> <pass>
LABEL=ROOT / ext4 defaults 0 1
LABEL=BOOT /boot ext4 defaults 0 2
EOF

echo "Preparing chroot environment..."

echo "Setting up qemu-user-static for cross-architecture chroot..."
cp /usr/bin/qemu-aarch64-static "$ROOT_MNT/usr/bin/"

echo "Mounting /boot inside root filesystem..."
mkdir -p "$ROOT_MNT/boot"
mount "$BOOT_PART" "$ROOT_MNT/boot"

echo "Setting up bootloader configuration..."
mkdir -p "$ROOT_MNT/boot/extlinux"
cp \
  "$WORKDIR/pocket/linux-mnt-pocket-${KVER}-${PKGREL}-mnt-pocket/extlinux.conf.example" \
  "$ROOT_MNT/boot/extlinux/extlinux.conf"

# Fix extlinux.conf to use LABEL=ROOT instead of /dev/nvme0n1p1
echo "Fixing extlinux.conf to use LABEL=ROOT..."
sed -i 's|root=/dev/nvme0n1p1|root=LABEL=ROOT|g' "$ROOT_MNT/boot/extlinux/extlinux.conf"

echo "Mounting virtual filesystems..."
mount -t proc /proc "$ROOT_MNT/proc"
mount -t sysfs /sys "$ROOT_MNT/sys"
mount --rbind /dev "$ROOT_MNT/dev"
mount --make-rslave "$ROOT_MNT/dev"
mount -o bind /run "$ROOT_MNT/run"

echo "Configuring DNS for chroot..."
cat > "$ROOT_MNT/etc/resolv.conf" << EOF
nameserver 8.8.8.8
nameserver 8.8.4.4
nameserver 1.1.1.1
EOF

echo "Copying PKGBUILD into chroot..."
cp -r "$WORKDIR/pocket/linux-mnt-pocket-${KVER}-${PKGREL}-mnt-pocket" "$ROOT_MNT/tmp/linux-mnt-pocket"

cat > "$ROOT_MNT/tmp/install_kernel.sh" << 'CHROOT_SCRIPT'
#!/bin/bash
set -euo pipefail

echo "Inside chroot - initializing pacman keyring..."
pacman-key --init
pacman-key --populate archlinuxarm

echo "Updating package database..."
pacman -Sy --noconfirm

echo "Installing essential packages..."
pacman -S --needed --noconfirm base base-devel dracut networkmanager

echo "Removing conflicting linux-aarch64 package if present..."
pacman -R --noconfirm linux-aarch64 || true

echo "Building and installing linux-mnt-pocket kernel..."
cd /tmp/linux-mnt-pocket

# Run makepkg as nobody user (makepkg refuses to run as root)
chown -R nobody:nobody /tmp/linux-mnt-pocket
sudo -u nobody makepkg --noconfirm

echo "Installing kernel package..."
pacman -U --noconfirm linux-mnt-pocket-*.pkg.tar.xz

echo "Kernel installed successfully!"
ls -lh /boot/
CHROOT_SCRIPT

chmod +x "$ROOT_MNT/tmp/install_kernel.sh"

echo
echo "=========================================="
echo "Entering chroot to install kernel..."
echo "=========================================="
echo

unshare -m chroot "$ROOT_MNT" /tmp/install_kernel.sh

echo "Cleaning up chroot environment..."
rm -rf "$ROOT_MNT/tmp/linux-mnt-pocket"
rm "$ROOT_MNT/tmp/install_kernel.sh"
rm "$ROOT_MNT/usr/bin/qemu-aarch64-static"

echo "Unmounting filesystems..."
umount "$ROOT_MNT/run" 2>/dev/null || true

umount -R "$ROOT_MNT/dev" 2>/dev/null || umount -l "$ROOT_MNT/dev" 2>/dev/null || true

umount "$ROOT_MNT/sys" 2>/dev/null || true
umount "$ROOT_MNT/proc" 2>/dev/null || true
umount "$ROOT_MNT/boot" 2>/dev/null || true
umount "$ROOT_MNT" 2>/dev/null || true
losetup -D

sync

echo "Generating bmap file for sparse image writing..."
if command -v bmaptool >/dev/null 2>&1; then
  bmaptool create -o "${IMAGE}.bmap" "${IMAGE}"
  echo "Bmap file created: ${IMAGE}.bmap"
else
  echo "Warning: bmaptool not found. Install 'bmap-tools' for faster SD card writing."
fi

echo
echo "=========================================="
echo "Disk image successfully created:"
echo "  $IMAGE"
echo "  $IMAGE.bmap (if bmaptool available)"
echo "=========================================="
echo
echo "Log file saved to: $LOGFILE"
echo
echo "Contents:"
echo "  - Boot partition with kernel, DTB, and initramfs"
echo "  - Root filesystem with Arch Linux ARM and kernel modules"
echo "  - Kernel installed via pacman from PKGBUILD"
echo
echo "To write to SD card:"
if command -v bmaptool >/dev/null 2>&1; then
  echo "  (Fast) sudo bmaptool copy $IMAGE /dev/sdX"
  echo "  (Slow) sudo dd if=$IMAGE of=/dev/sdX bs=4M status=progress conv=fsync"
else
  echo "  sudo dd if=$IMAGE of=/dev/sdX bs=4M status=progress conv=fsync"
  echo
  echo "  For faster writing, install bmap-tools:"
  echo "  sudo pacman -S bmap-tools"
fi
echo
echo "Completed at: $(date)"
