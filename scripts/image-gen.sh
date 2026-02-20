#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# Configuration
# ============================================================================
readonly KVER="6.18.12"
readonly PKGREL="1"
readonly KERNEL_VERSION="${KVER}-mnt-reform"
readonly IMAGE_SIZE_GB=120
readonly BOOT_SIZE_MB=1024
readonly PARTITION_WAIT_MAX_ATTEMPTS=20
readonly PARTITION_WAIT_INTERVAL=0.2

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly WORK_DIR="$(pwd)/image-gen"
readonly DOWNLOADS_DIR="$WORK_DIR/downloads"
readonly MOUNT_DIR="$WORK_DIR/mnt"
readonly BOOT_MNT="$MOUNT_DIR/boot"
readonly ROOT_MNT="$MOUNT_DIR/root"
readonly IMAGE="$(pwd)/mnt-reform-${KVER}-aarch64.img"

readonly KERNEL_URL="https://github.com/cetola/linux-mnt-reform/archive/refs/tags/${KVER}-${PKGREL}-mnt-reform.tar.gz"
readonly ARCH_URL="http://os.archlinuxarm.org/os/ArchLinuxARM-aarch64-latest.tar.gz"

readonly TIMESTAMP=$(date +%Y%m%d-%H%M%S)
readonly LOGFILE="$(pwd)/image-gen-${KVER}-${TIMESTAMP}.log"

# Global state
LOOPDEV=""
BOOT_PART=""
ROOT_PART=""

# ============================================================================
# Utility Functions
# ============================================================================

log() {
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*"
}

log_error() {
  echo "ERROR: $*" >&2
}

log_section() {
  echo
  echo "=========================================="
  echo "$*"
  echo "=========================================="
  echo
}

die() {
  log_error "$@"
  exit 1
}

# ============================================================================
# Validation Functions
# ============================================================================

check_root() {
  if [[ $EUID -ne 0 ]]; then
    die "This script must be run as root."
  fi
}

check_required_tools() {
  local missing_tools=()
  local tool_map=(
    "truncate:coreutils"
    "parted:parted"
    "losetup:util-linux"
    "mkfs.ext4:e2fsprogs"
    "curl:curl"
    "tar:tar"
    "chroot:arch-install-scripts"
  )

  log "Checking for required tools..."
  
  for entry in "${tool_map[@]}"; do
    IFS=':' read -r cmd pkg <<< "$entry"
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing_tools+=("$pkg")
    fi
  done

  # Check for qemu-aarch64-static (required for x86_64 -> aarch64 chroot)
  if [[ ! -f /usr/bin/qemu-aarch64-static ]]; then
    missing_tools+=("qemu-user-static qemu-user-static-binfmt")
  fi

  if [[ ${#missing_tools[@]} -gt 0 ]]; then
    log_error "Missing required tools/packages:"
    for tool in "${missing_tools[@]}"; do
      echo "  - $tool"
    done
    echo
    echo "Install them with:"
    echo "  sudo pacman -S ${missing_tools[*]}"
    exit 1
  fi

  log "All required tools found."
}

# ============================================================================
# Cleanup Functions
# ============================================================================

cleanup_mounts() {
  set +e
  log "Cleaning up mounts..."

  umount "$ROOT_MNT/run" 2>/dev/null || true
  umount -R "$ROOT_MNT/dev" 2>/dev/null || umount -l "$ROOT_MNT/dev" 2>/dev/null || true
  umount "$ROOT_MNT/sys" 2>/dev/null || true
  umount "$ROOT_MNT/proc" 2>/dev/null || true
  umount "$ROOT_MNT/boot" 2>/dev/null || true
  umount "$ROOT_MNT" 2>/dev/null || true
  umount "$BOOT_MNT" 2>/dev/null || true  # Add this - clean up boot mount point too

  sync
  sleep 1
  if [[ -n "$LOOPDEV" ]]; then
    losetup -d "$LOOPDEV" 2>/dev/null || true
  fi
}

cleanup() {
  set +e
  # Note: unshare -m creates a private mount namespace, so mounts inside
  # the chroot don't need cleanup - they're automatically cleaned up when
  # the namespace exits. We only clean up mounts we created before chroot.
  cleanup_mounts
}
trap cleanup EXIT

# ============================================================================
# Image Creation Functions
# ============================================================================

create_disk_image() {
  log "Creating sparse disk image..."
  truncate -s "${IMAGE_SIZE_GB}G" "$IMAGE"
}

partition_image() {
  log "Partitioning image..."
  parted --script "$IMAGE" \
    mklabel msdos \
    mkpart primary ext4 1MiB "$((BOOT_SIZE_MB + 1))"MiB \
    mkpart primary ext4 "$((BOOT_SIZE_MB + 1))"MiB 100%
}

setup_loop_device() {
  log "Setting up loop device..."
  
  # Attach AFTER partitioning; ask kernel to scan partitions immediately
  LOOPDEV="$(losetup --find --show --partscan "${IMAGE}")"
  log "Using loop device: ${LOOPDEV}"

  # Let udev create /dev/loopXp1 nodes
  udevadm settle || true
  sleep 1

  # Optional: nudge the kernel to re-read partition table (best effort)
  partprobe "${LOOPDEV}" || true
  udevadm settle || true
  sleep 1

  # Wait until partition nodes exist (handles slow runners)
  local attempt=0
  while [[ $attempt -lt $PARTITION_WAIT_MAX_ATTEMPTS ]]; do
    if [[ -b "${LOOPDEV}p1" && -b "${LOOPDEV}p2" ]]; then
      break
    fi
    sleep $PARTITION_WAIT_INTERVAL
    udevadm settle || true
    ((attempt++))
  done

  # Hard fail with useful diagnostics if still missing
  if [[ ! -b "${LOOPDEV}p1" || ! -b "${LOOPDEV}p2" ]]; then
    log_error "Partition nodes not created for ${LOOPDEV}"
    ls -l "${LOOPDEV}"* || true
    ls -l /dev/loop* || true
    cat /proc/partitions | grep -E 'loop|mapper' || true
    exit 1
  fi

  BOOT_PART="${LOOPDEV}p1"
  ROOT_PART="${LOOPDEV}p2"
  
  log "Partitions ready: boot=$BOOT_PART, root=$ROOT_PART"
}

format_partitions() {
  log "Formatting partitions..."
  mkfs.ext4 -F -L BOOT "$BOOT_PART"
  mkfs.ext4 -F -L ROOT "$ROOT_PART"
}

mount_partitions() {
  log "Mounting partitions..."
  mount "$BOOT_PART" "$BOOT_MNT"
  mount "$ROOT_PART" "$ROOT_MNT"
}

# ============================================================================
# Download Functions
# ============================================================================

download_if_missing() {
  local url="$1"
  local output="$2"
  
  if [[ -f "$output" ]]; then
    log "Using cached $output"
  else
    log "Downloading $output..."
    curl -L -o "$output" "$url"
  fi
}

download_dependencies() {
  log "Downloading dependencies..."
  cd "$DOWNLOADS_DIR"
  download_if_missing "$KERNEL_URL" "kernel.tar.gz"
  download_if_missing "$ARCH_URL" "archlinuxarm.tar.gz"
}

# ============================================================================
# Filesystem Setup Functions
# ============================================================================

extract_rootfs() {
  log "Extracting ArchLinuxARM root filesystem..."
  tar -xpf "$DOWNLOADS_DIR/archlinuxarm.tar.gz" -C "$ROOT_MNT"
}

extract_kernel() {
  log "Extracting linux-mnt-reform..."
  mkdir -p "$WORK_DIR/kernel"
  tar --no-same-owner -xpf "$DOWNLOADS_DIR/kernel.tar.gz" -C "$WORK_DIR/kernel"
}

create_fstab() {
  log "Creating /etc/fstab..."
  cat > "$ROOT_MNT/etc/fstab" << 'EOF'
# <source> <mountpoint> <fstype> <options> <dump> <pass>
LABEL=ROOT / ext4 defaults 0 1
LABEL=BOOT /boot ext4 defaults 0 2
EOF
}

setup_chroot_environment() {
  log "Preparing chroot environment..."
  
  log "Setting up qemu-user-static for cross-architecture chroot..."
  cp /usr/bin/qemu-aarch64-static "$ROOT_MNT/usr/bin/"
  
  log "Mounting /boot inside root filesystem..."
  mkdir -p "$ROOT_MNT/boot"
  mount "$BOOT_PART" "$ROOT_MNT/boot"
  
  setup_bootloader_config
  mount_virtual_filesystems
  configure_dns
  copy_pkgbuild
}

setup_bootloader_config() {
  log "Setting up bootloader configuration..."
  mkdir -p "$ROOT_MNT/boot/extlinux"
  cp \
    "$WORK_DIR/kernel/linux-mnt-reform-${KVER}-${PKGREL}-mnt-reform/extlinux.conf.example" \
    "$ROOT_MNT/boot/extlinux/extlinux.conf"
  
  # Fix extlinux.conf to use LABEL=ROOT instead of /dev/nvme0n1p1
  log "Fixing extlinux.conf to use LABEL=ROOT..."
  sed -i 's|root=/dev/nvme0n1p1|root=LABEL=ROOT|g' "$ROOT_MNT/boot/extlinux/extlinux.conf"
}

mount_virtual_filesystems() {
  log "Mounting virtual filesystems..."
  mount -t proc /proc "$ROOT_MNT/proc"
  mount -t sysfs /sys "$ROOT_MNT/sys"
  mount --rbind /dev "$ROOT_MNT/dev"
  mount --make-rslave "$ROOT_MNT/dev"
  mount -o bind /run "$ROOT_MNT/run"
}

configure_dns() {
  log "Configuring DNS for chroot..."
  cat > "$ROOT_MNT/etc/resolv.conf" << EOF
nameserver 8.8.8.8
nameserver 8.8.4.4
nameserver 1.1.1.1
EOF
}

copy_pkgbuild() {
  log "Copying PKGBUILD into chroot..."
  cp -r "$WORK_DIR/kernel/linux-mnt-reform-${KVER}-${PKGREL}-mnt-reform" "$ROOT_MNT/tmp/linux-mnt-reform"
}

create_chroot_script() {
  log "Creating chroot installation script..."
  cat > "$ROOT_MNT/tmp/install_kernel.sh" << 'CHROOT_SCRIPT'
#!/bin/bash
set -euo pipefail

echo "Inside chroot - initializing pacman keyring..."
pacman-key --init
pacman-key --populate archlinuxarm

PACMAN="pacman --disable-sandbox"

echo "Updating package database..."
$PACMAN -Sy --noconfirm

echo "Installing essential packages..."
$PACMAN -S --needed --noconfirm base base-devel dracut networkmanager

echo "Removing conflicting linux-aarch64 package if present..."
$PACMAN -R --noconfirm linux-aarch64 || true

echo "Building and installing linux-mnt-reform kernel..."
cd /tmp/linux-mnt-reform

# Run makepkg as nobody user (makepkg refuses to run as root)
chown -R nobody:nobody /tmp/linux-mnt-reform
sudo -u nobody makepkg --noconfirm

echo "Installing kernel package..."
$PACMAN -U --noconfirm linux-mnt-reform-*.pkg.tar.xz

echo "Kernel installed successfully!"
ls -lh /boot/
CHROOT_SCRIPT
  
  chmod +x "$ROOT_MNT/tmp/install_kernel.sh"
}

run_chroot_installation() {
  log_section "Entering chroot to install kernel..."
  unshare -m chroot "$ROOT_MNT" /tmp/install_kernel.sh
}

cleanup_chroot_environment() {
  log "Cleaning up chroot environment..."
  rm -rf "$ROOT_MNT/tmp/linux-mnt-reform"
  rm -f "$ROOT_MNT/tmp/install_kernel.sh"
  rm -f "$ROOT_MNT/usr/bin/qemu-aarch64-static"
}

# ============================================================================
# Post-Processing Functions
# ============================================================================

get_target_ownership() {
  if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then
    TARGET_UID="$SUDO_UID"
    TARGET_GID="$SUDO_GID"
    log "Detected sudo user: UID=$TARGET_UID, GID=$TARGET_GID"
  else
    TARGET_UID=1000
    TARGET_GID=1000
    log "Could not detect sudo user, using UID=$TARGET_UID, GID=$TARGET_GID"
  fi
}

fix_file_ownership() {
  log "Fixing ownership and permissions of output files..."
  get_target_ownership
  
  chown "$TARGET_UID:$TARGET_GID" "$IMAGE"
  chown -R "$TARGET_UID:$TARGET_GID" "$WORK_DIR"
  chown "$TARGET_UID:$TARGET_GID" "$LOGFILE"
  
  chmod 644 "$IMAGE"
  chmod 644 "$LOGFILE"
}

generate_bmap() {
  log "Generating bmap file for sparse image writing..."
  if command -v bmaptool >/dev/null 2>&1; then
    bmaptool create -o "${IMAGE}.bmap" "${IMAGE}"
    get_target_ownership
    chown "$TARGET_UID:$TARGET_GID" "${IMAGE}.bmap"
    chmod 644 "${IMAGE}.bmap"
    log "Bmap file created: ${IMAGE}.bmap"
  else
    log "Warning: bmaptool not found. Install 'bmap-tools' for faster SD card writing."
  fi
}

print_summary() {
  log_section "Disk image successfully created"
  echo "  $IMAGE"
  if command -v bmaptool >/dev/null 2>&1; then
    echo "  $IMAGE.bmap"
  fi
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
}

# ============================================================================
# Main Execution
# ============================================================================

main() {
  # Setup logging
  exec > >(tee -a "$LOGFILE") 2>&1
  
  log "Logging to: $LOGFILE"
  log "Started at: $(date)"
  echo
  
  # Validation
  check_root
  check_required_tools
  echo
  
  # Create working directories
  mkdir -p "$DOWNLOADS_DIR" "$BOOT_MNT" "$ROOT_MNT"
  
  # Image creation
  create_disk_image
  partition_image
  setup_loop_device
  format_partitions
  mount_partitions
  
  # Download and extract
  download_dependencies
  extract_rootfs
  extract_kernel
  
  # Filesystem configuration
  create_fstab
  setup_chroot_environment
  
  # Kernel installation
  create_chroot_script
  run_chroot_installation
  cleanup_chroot_environment
  
  # Finalization
  cleanup_mounts
  sync
  
  fix_file_ownership
  generate_bmap
  print_summary
  
  log "Completed at: $(date)"
}

main "$@"
