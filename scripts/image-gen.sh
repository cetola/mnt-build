#!/usr/bin/env bash
set -euo pipefail

IMAGE="$(pwd)/mnt-pocket.img"
IMAGE_SIZE_GB=12
BOOT_SIZE_MB=1024
WORKDIR="$(pwd)/work"
DOWNLOADS="$WORKDIR/downloads"
MOUNTDIR="$WORKDIR/mnt"
BOOT_MNT="$MOUNTDIR/boot"
ROOT_MNT="$MOUNTDIR/root"

KERNEL_URL="https://github.com/cetola/mnt-build/releases/download/6.17.12-1-mnt-pocket/kernel-6.17.12-1-mnt.tar.gz"
POCKET_URL="https://github.com/cetola/linux-mnt-pocket/archive/refs/tags/6.17.12-1-mnt-pocket.tar.gz"
ARCH_URL="http://os.archlinuxarm.org/os/ArchLinuxARM-aarch64-latest.tar.gz"

# Check for required tools
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
  # Only cleanup if bmaptool hasn't run yet
  umount "$ROOT_MNT/run" 2>/dev/null || true
  umount "$ROOT_MNT/dev/pts" 2>/dev/null || true
  umount "$ROOT_MNT/dev" 2>/dev/null || true
  umount "$ROOT_MNT/sys" 2>/dev/null || true
  umount "$ROOT_MNT/proc" 2>/dev/null || true
  umount "$BOOT_MNT" 2>/dev/null || true
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

LOOPDEV=$(losetup --find --show --partscan "$IMAGE")
echo "Using loop device: $LOOPDEV"

BOOT_PART="${LOOPDEV}p1"
ROOT_PART="${LOOPDEV}p2"

echo "Formatting partitions..."
mkfs.ext4 -F -L BOOT "$BOOT_PART"
mkfs.ext4 -F -L ROOT "$ROOT_PART"

mount "$BOOT_PART" "$BOOT_MNT"
mount "$ROOT_PART" "$ROOT_MNT"

cd "$DOWNLOADS"

# Download with caching
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

download_if_missing "$KERNEL_URL" "kernel.tar.gz"
download_if_missing "$POCKET_URL" "pocket.tar.gz"
download_if_missing "$ARCH_URL" "archlinuxarm.tar.gz"

echo "Extracting ArchLinuxARM root filesystem..."
tar -xpf archlinuxarm.tar.gz -C "$ROOT_MNT"

echo "Extracting kernel..."
mkdir -p "$WORKDIR/kernel"
tar -xpf kernel.tar.gz -C "$WORKDIR/kernel"

echo "Extracting linux-mnt-pocket..."
mkdir -p "$WORKDIR/pocket"
tar -xpf pocket.tar.gz -C "$WORKDIR/pocket"

echo "Populating boot partition..."
cp "$WORKDIR/kernel/config-6.17.12-mnt-reform-arm64" "$BOOT_MNT/"
cp "$WORKDIR/kernel/imx8mp-mnt-pocket-reform-6.17.12.dtb" "$BOOT_MNT/imx8mp-mnt-pocket-reform.dtb"
cp "$WORKDIR/kernel/arch/arm64/boot/Image" "$BOOT_MNT/Image-testing"

mkdir -p "$BOOT_MNT/extlinux"
cp \
  "$WORKDIR/pocket/linux-mnt-pocket-6.17.12-1-mnt-pocket/extlinux.conf.example" \
  "$BOOT_MNT/extlinux/extlinux.conf"

echo "Overlaying kernel /etc, /usr, /lib into root filesystem..."
for dir in etc usr lib; do
  if [[ -d "$WORKDIR/kernel/$dir" ]]; then
    cp -a "$WORKDIR/kernel/$dir/." "$ROOT_MNT/$dir/"
  fi
done

# Prepare for chroot
echo "Preparing chroot environment..."

# Copy qemu-aarch64-static for x86_64 -> aarch64 chroot
echo "Setting up qemu-user-static for cross-architecture chroot..."
cp /usr/bin/qemu-aarch64-static "$ROOT_MNT/usr/bin/"

# Mount necessary filesystems for chroot
echo "Mounting virtual filesystems..."
mount -t proc /proc "$ROOT_MNT/proc"
mount -t sysfs /sys "$ROOT_MNT/sys"
mount -o bind /dev "$ROOT_MNT/dev"
mount -t devpts devpts "$ROOT_MNT/dev/pts"
mount -o bind /run "$ROOT_MNT/run"

# Set up networking - create a simple resolv.conf
echo "Configuring DNS for chroot..."
cat > "$ROOT_MNT/etc/resolv.conf" << EOF
nameserver 8.8.8.8
nameserver 8.8.4.4
nameserver 1.1.1.1
EOF

# Create a script to run inside the chroot
cat > "$ROOT_MNT/tmp/generate_initramfs.sh" << 'CHROOT_SCRIPT'
#!/bin/bash
set -euo pipefail

echo "Inside chroot - initializing pacman keyring..."
pacman-key --init
pacman-key --populate archlinuxarm

echo "Updating package database..."
pacman -Sy --noconfirm

echo "Installing dracut..."
pacman -S --noconfirm dracut

echo "Detecting kernel version..."
KERNEL_VERSION=$(ls /lib/modules/ | head -n 1)
if [[ -z "$KERNEL_VERSION" ]]; then
  echo "ERROR: No kernel modules found in /lib/modules/"
  exit 1
fi
echo "Found kernel version: $KERNEL_VERSION"

echo "Generating initramfs with dracut..."
dracut --force --no-hostonly \
  "/boot/initramfs-linux-testing" \
  "$KERNEL_VERSION"

echo "Initramfs generated successfully: /boot/initramfs-linux-testing"

# Verify it was created
if [[ -f "/boot/initramfs-linux-testing" ]]; then
  ls -lh "/boot/initramfs-linux-testing"
else
  echo "ERROR: Initramfs file not found after generation!"
  exit 1
fi
CHROOT_SCRIPT

chmod +x "$ROOT_MNT/tmp/generate_initramfs.sh"

echo
echo "=========================================="
echo "Entering chroot to generate initramfs..."
echo "=========================================="
echo

# Run the script in chroot
chroot "$ROOT_MNT" /tmp/generate_initramfs.sh

# Move initramfs from root filesystem /boot to boot partition
echo "Moving initramfs to boot partition..."
if [[ -f "$ROOT_MNT/boot/initramfs-linux-testing" ]]; then
  mv "$ROOT_MNT/boot/initramfs-linux-testing" "$BOOT_MNT/"
  echo "Initramfs moved to boot partition successfully"
else
  echo "ERROR: Initramfs not found at $ROOT_MNT/boot/initramfs-linux-testing"
  exit 1
fi

# Clean up
echo "Cleaning up chroot environment..."
rm "$ROOT_MNT/tmp/generate_initramfs.sh"
rm "$ROOT_MNT/usr/bin/qemu-aarch64-static"

# Unmount everything before generating bmap
echo "Unmounting filesystems..."
umount "$ROOT_MNT/run" 2>/dev/null || true
umount "$ROOT_MNT/dev/pts" 2>/dev/null || true

# Unmount /dev with retry and lazy umount if needed
for i in {1..3}; do
  if umount "$ROOT_MNT/dev" 2>/dev/null; then
    break
  fi
  echo "Retrying /dev unmount (attempt $i)..."
  sleep 1
done
# If still mounted, use lazy unmount
if mountpoint -q "$ROOT_MNT/dev" 2>/dev/null; then
  echo "Using lazy unmount for /dev..."
  umount -l "$ROOT_MNT/dev"
fi

umount "$ROOT_MNT/sys" 2>/dev/null || true
umount "$ROOT_MNT/proc" 2>/dev/null || true
umount "$BOOT_MNT" 2>/dev/null || true
umount "$ROOT_MNT" 2>/dev/null || true
losetup -D

sync

# Generate bmap file for faster writing (after everything is unmounted)
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
echo "Contents:"
echo "  - Boot partition with kernel, DTB, and initramfs"
echo "  - Root filesystem with Arch Linux ARM and kernel modules"
echo "  - Initramfs: /boot/initramfs-linux-testing"
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
