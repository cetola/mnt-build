#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# Configuration
# ============================================================================
readonly IMAGE_SIZE_GB=110
readonly BOOT_SIZE_MB=1024
readonly MAX_BOOTLOADER_MB=16
readonly PARTITION_WAIT_MAX_ATTEMPTS=20
readonly PARTITION_WAIT_INTERVAL=0.2

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly WORK_DIR="$(pwd)/image-gen"
readonly DOWNLOADS_DIR="$WORK_DIR/downloads"
readonly MOUNT_DIR="$WORK_DIR/mnt"
readonly BOOT_MNT="$MOUNT_DIR/boot"
readonly ROOT_MNT="$MOUNT_DIR/root"
readonly CHROOT_INSTALLER_SOURCE="$SCRIPT_DIR/install_kernel_chroot.sh"
readonly CHROOT_INSTALLER_TARGET="$ROOT_MNT/tmp/install_kernel.sh"
readonly KERNEL_VERSION_FILE="$ROOT_MNT/tmp/linux-mnt-reform-bin.version"
readonly KERNEL_EXTLINUX_STAGED="$ROOT_MNT/tmp/linux-mnt-reform-bin.extlinux.conf.example"
readonly KERNEL_EXTLINUX_FALLBACK="$SCRIPT_DIR/linux-mnt-reform-bin/extlinux.conf.example"

readonly ARCH_URL="http://os.archlinuxarm.org/os/ArchLinuxARM-aarch64-latest.tar.gz"

readonly TIMESTAMP=$(date +%Y%m%d-%H%M%S)
IMAGE="$DOWNLOADS_DIR/arch-sys-mnt-image.img"
LOGFILE="$DOWNLOADS_DIR/image-gen-${TIMESTAMP}.log"

# shellcheck source=/dev/null
source "$SCRIPT_DIR/common.sh"

# Global state
LOOPDEV=""
BOOT_PART=""
ROOT_PART=""
SYSIMAGE=""
DTBPATH=""
KERNEL_DTB_STEM=""
IMAGE_PLATFORM=""
IMAGE_SOM=""
BOOTLOADER_SHA1=""
BOOTLOADER_PROJECT=""
BOOTLOADER_TAG=""
BOOTLOADER_OFFSET=0
FLASHBIN_OFFSET=0
SD_BOOT=false
BOOTLOADER_FILENAME=""
INSTALLED_KERNEL_VERSION=""

# ============================================================================
# Utility Functions
# ============================================================================

log() {
  common_log "$@"
}

log_error() {
  common_error "$@"
}

log_warn() {
  common_warn "$@"
}

log_section() {
  common_section "$@"
}

die() {
  log_error "$@"
  exit 1
}

usage() {
  cat <<'EOF'
Usage:
  image-gen.sh --sysimage <name>

Options:
  --sysimage <name>   Target platform sysimage. Supported values:
                      - pocket-reform-system-a311d
                      - reform-next-system-rk3588
                      - pocket-reform-system-rk3588
                      - pocket-reform-system-imx8mp
  -h, --help          Show this help.
EOF
}

parse_args() {
  local sysimage_set=false

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --sysimage)
        [[ $# -lt 2 ]] && die "--sysimage requires an argument"
        SYSIMAGE="$2"
        sysimage_set=true
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Unknown argument: $1"
        ;;
    esac
  done

  if [[ "$sysimage_set" != "true" ]]; then
    usage
    die "--sysimage is required"
  fi
}

source "$SCRIPT_DIR/sysimage-config.sh"
source "$SCRIPT_DIR/build-fsbl.sh"

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
    "make:make"
    "git:git"
    "curl:curl"
    "tar:tar"
    "chroot:coreutils"
    "cpio:cpio"
  )

  log "Checking for required tools..."
  
  for entry in "${tool_map[@]}"; do
    IFS=':' read -r cmd pkg <<< "$entry"
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing_tools+=("$pkg")
    fi
  done

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
  local boot_end_mb=$((MAX_BOOTLOADER_MB + BOOT_SIZE_MB))
  parted --script "$IMAGE" \
    mklabel msdos \
    mkpart primary ext4 "${MAX_BOOTLOADER_MB}"MiB "${boot_end_mb}"MiB \
    mkpart primary ext4 "${boot_end_mb}"MiB 100%
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
  download_if_missing "$ARCH_URL" "archlinuxarm.tar.gz"
}

preflight_bootloader_artifact() {
  log_section "Preflight: resolving bootloader artifact"
  log "BOOTLOADER_SOURCE_MODE=${BOOTLOADER_SOURCE_MODE:-prebuilt}"
  get_fsbl_artifact

  if [[ -z "${BOOTLOADER_ARTIFACT_PATH:-}" || ! -f "${BOOTLOADER_ARTIFACT_PATH}" ]]; then
    die "Bootloader preflight failed: artifact path is missing or file does not exist."
  fi

  log "Bootloader preflight succeeded: BOOTLOADER_ARTIFACT_PATH=${BOOTLOADER_ARTIFACT_PATH}"
}

verify_bootloader_checksum() {
  if [[ "${BOOTLOADER_SOURCE_MODE:-prebuilt}" == "source" ]]; then
    log "Skipping static checksum verification for source-built bootloader artifact."
    return 0
  fi

  log "Verifying bootloader checksum..."
  local bootloader_path="${BOOTLOADER_ARTIFACT_PATH:-$DOWNLOADS_DIR/$BOOTLOADER_FILENAME}"
  local actual_sha1
  actual_sha1="$(sha1sum "$bootloader_path" | awk '{print $1}')"
  if [[ "$actual_sha1" != "$BOOTLOADER_SHA1" ]]; then
    die "Bootloader checksum mismatch for $BOOTLOADER_FILENAME (expected $BOOTLOADER_SHA1, got $actual_sha1)"
  fi
}

install_bootloader_to_image() {
  if [[ "$SD_BOOT" != "true" ]]; then
    log "Skipping bootloader raw write for $SYSIMAGE (SD_BOOT=false)."
    return
  fi

  local bootloader_path="${BOOTLOADER_ARTIFACT_PATH:-$DOWNLOADS_DIR/$BOOTLOADER_FILENAME}"
  local bootloader_size
  local first_partition_start=""
  local required_space

  bootloader_size="$(stat -c%s "$bootloader_path")"
  required_space=$((BOOTLOADER_OFFSET - FLASHBIN_OFFSET + bootloader_size))

  first_partition_start="$(parted --script --machine "$IMAGE" unit B print \
    | awk -F: '$1 ~ /^[0-9]+$/ { gsub(/B/, "", $2); print $2; exit }')"
  if [[ -z "$first_partition_start" ]]; then
    die "Unable to determine first partition start from partition table."
  fi

  if (( BOOTLOADER_OFFSET < 512 )); then
    die "Refusing to write bootloader into first 512 bytes (BOOTLOADER_OFFSET=$BOOTLOADER_OFFSET)."
  fi

  if (( required_space > first_partition_start )); then
    die "Bootloader would overlap first partition (needs ${required_space} bytes, first partition starts at ${first_partition_start})."
  fi

  if (( BOOTLOADER_OFFSET % 512 != 0 || FLASHBIN_OFFSET % 512 != 0 )); then
    die "BOOTLOADER_OFFSET and FLASHBIN_OFFSET must be multiples of 512."
  fi

  log "Writing bootloader to image (offset=$BOOTLOADER_OFFSET, skip=$FLASHBIN_OFFSET)..."
  dd if="$bootloader_path" of="$IMAGE" conv=notrunc bs=512 \
    seek="$((BOOTLOADER_OFFSET / 512))" skip="$((FLASHBIN_OFFSET / 512))"
}

install_bootloader_to_boot_partition() {
  local bootloader_path="${BOOTLOADER_ARTIFACT_PATH:-$DOWNLOADS_DIR/$BOOTLOADER_FILENAME}"
  local boot_partition_bootloader="$BOOT_MNT/flash.bin"

  log "Copying bootloader artifact into boot filesystem: $boot_partition_bootloader"
  install -m 0644 "$bootloader_path" "$boot_partition_bootloader"
}

# ============================================================================
# Filesystem Setup Functions
# ============================================================================

extract_rootfs() {
  log "Extracting ArchLinuxARM root filesystem..."
  tar -xpf "$DOWNLOADS_DIR/archlinuxarm.tar.gz" -C "$ROOT_MNT"
}

configure_pacman_mirrors() {
  local mirrorlist="$ROOT_MNT/etc/pacman.d/mirrorlist"

  log "Configuring ArchLinuxARM pacman mirrors..."
  mkdir -p "$(dirname "$mirrorlist")"

  cat > "$mirrorlist" <<'EOF'
# Prefer concrete mirrors so pacman can fail over if one backend is stale.
Server = http://nj.us.mirror.archlinuxarm.org/$arch/$repo
Server = http://ca.us.mirror.archlinuxarm.org/$arch/$repo
Server = http://fl.us.mirror.archlinuxarm.org/$arch/$repo
Server = http://eu.mirror.archlinuxarm.org/$arch/$repo
Server = http://de.mirror.archlinuxarm.org/$arch/$repo
Server = http://de3.mirror.archlinuxarm.org/$arch/$repo
Server = http://de4.mirror.archlinuxarm.org/$arch/$repo
Server = http://dk.mirror.archlinuxarm.org/$arch/$repo
Server = http://mirror.archlinuxarm.org/$arch/$repo
EOF
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
  
  log "Mounting /boot inside root filesystem..."
  mkdir -p "$ROOT_MNT/boot"
  mount "$BOOT_PART" "$ROOT_MNT/boot"
  
  mkdir -p "$ROOT_MNT/boot/extlinux"
  mount_virtual_filesystems
  configure_dns
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

configure_extlinux() {
  log "Installing extlinux.conf from kernel package example..."
  local extlinux_example="$ROOT_MNT/usr/share/doc/linux-mnt-reform-bin/extlinux.conf.example"
  local extlinux_conf="$BOOT_MNT/extlinux/extlinux.conf"

  if [[ ! -f "$extlinux_example" ]]; then
    extlinux_example="$KERNEL_EXTLINUX_STAGED"
  fi

  if [[ ! -f "$extlinux_example" ]]; then
    extlinux_example="$KERNEL_EXTLINUX_FALLBACK"
  fi

  if [[ ! -f "$extlinux_example" ]]; then
    die "Unable to find extlinux.conf.example in installed package, staged chroot copy, or workspace fallback"
  fi

  mkdir -p "$BOOT_MNT/extlinux"
  cp "$extlinux_example" "$extlinux_conf"

  log "Fixing extlinux.conf to use LABEL=ROOT..."
  sed -i 's|root=/dev/nvme0n1p1|root=LABEL=ROOT|g' "$extlinux_conf"
}

create_chroot_script() {
  log "Creating chroot installation script..."
  install -m 0755 "$CHROOT_INSTALLER_SOURCE" "$CHROOT_INSTALLER_TARGET"
}

run_chroot_installation() {
  log_section "Entering chroot to install kernel..."
  unshare -m chroot "$ROOT_MNT" /tmp/install_kernel.sh
}

resolve_installed_kernel_version() {
  log "Resolving installed kernel release from installed module metadata..."

  if [[ ! -f "$KERNEL_VERSION_FILE" ]]; then
    die "Installed kernel version file not found: $KERNEL_VERSION_FILE"
  fi

  INSTALLED_KERNEL_VERSION="$(<"$KERNEL_VERSION_FILE")"

  if [[ -z "$INSTALLED_KERNEL_VERSION" ]]; then
    die "Installed kernel version is empty"
  fi

  log "Resolved installed kernel version: $INSTALLED_KERNEL_VERSION"
}

configure_boot_dtb_symlink() {
  log "Configuring boot DTB symlink for $SYSIMAGE..."
  local dtb_dir="$BOOT_MNT/dtbs"
  local expected_dtb=""
  local target_link=""

  if [[ -z "${KERNEL_DTB_STEM:-}" ]]; then
    log_warn "No kernel DTB mapping defined for $SYSIMAGE. Skipping DTB symlink setup."
    return 0
  fi

  expected_dtb="${KERNEL_DTB_STEM}-${INSTALLED_KERNEL_VERSION}.dtb"
  if [[ ! -f "$dtb_dir/$expected_dtb" ]]; then
    log_warn "Expected DTB not found: $dtb_dir/$expected_dtb"
    log_warn "Skipping DTB symlink setup; image generation will continue."
    log_warn "User may need to manually copy/link the correct DTB."
    ls -lah "$dtb_dir" || true
    return 0
  fi

  target_link="$BOOT_MNT/mnt-reform-${INSTALLED_KERNEL_VERSION}.dtb"
  ln -sfn "dtbs/${expected_dtb}" "$target_link"
  ln -sfn "mnt-reform-${INSTALLED_KERNEL_VERSION}.dtb" "$BOOT_MNT/mnt-reform.dtb"
  log "DTB symlinks updated:"
  log "  mnt-reform-${INSTALLED_KERNEL_VERSION}.dtb -> dtbs/${expected_dtb}"
  log "  mnt-reform.dtb -> mnt-reform-${INSTALLED_KERNEL_VERSION}.dtb"
}

cleanup_chroot_environment() {
  log "Cleaning up chroot environment..."
  rm -f "$CHROOT_INSTALLER_TARGET"
  rm -f "$KERNEL_VERSION_FILE"
}

# Reclaim free-space compressibility before image compression.
reclaim_filesystem_free_space() {
  log "Reclaiming free space for better image compression..."
  local zero_fill_max_mb="${ZERO_FILL_MAX_MB:-2048}"

  # Validate env input and keep a safe default.
  if ! [[ "$zero_fill_max_mb" =~ ^[0-9]+$ ]]; then
    log_warn "Invalid ZERO_FILL_MAX_MB='$zero_fill_max_mb', using 2048."
    zero_fill_max_mb=2048
  fi

  # Best effort pre-trim: discard any currently free extents.
  fstrim -v "$ROOT_MNT" || true
  fstrim -v "$BOOT_MNT" || true

  # Write a bounded amount of zeros so we do not exhaust runner backing storage.
  if (( zero_fill_max_mb > 0 )); then
    log "Zero-filling up to ${zero_fill_max_mb} MiB on root filesystem..."
    dd if=/dev/zero of="$ROOT_MNT/.zero-fill" bs=1M count="$zero_fill_max_mb" status=none || true
    sync || true
    rm -f "$ROOT_MNT/.zero-fill" || true
  else
    log "ZERO_FILL_MAX_MB=0; skipping root zero-fill."
  fi

  # Keep boot fill very small.
  log "Zero-filling up to 128 MiB on boot filesystem..."
  dd if=/dev/zero of="$BOOT_MNT/.zero-fill" bs=1M count=128 status=none || true
  sync || true
  rm -f "$BOOT_MNT/.zero-fill" || true

  # Final trim after zero-fill.
  fstrim -v "$ROOT_MNT" || true
  fstrim -v "$BOOT_MNT" || true
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

rename_outputs_for_kernel_version() {
  local image_prefix="arch-sys-mnt-image"
  local final_image=""
  local final_logfile="$DOWNLOADS_DIR/image-gen-${INSTALLED_KERNEL_VERSION}-${TIMESTAMP}.log"

  if [[ -n "${IMAGE_PLATFORM:-}" && -n "${IMAGE_SOM:-}" ]]; then
    image_prefix="arch-sys-mnt-${IMAGE_PLATFORM}-${IMAGE_SOM}"
  fi

  final_image="$DOWNLOADS_DIR/${image_prefix}-${INSTALLED_KERNEL_VERSION}.img"

  if [[ "$IMAGE" != "$final_image" ]]; then
    mv "$IMAGE" "$final_image"
    IMAGE="$final_image"
  fi

  if [[ "$LOGFILE" != "$final_logfile" ]]; then
    mv "$LOGFILE" "$final_logfile"
    LOGFILE="$final_logfile"
  fi
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
  echo "Target sysimage: $SYSIMAGE"
  if [[ -n "${IMAGE_PLATFORM:-}" && -n "${IMAGE_SOM:-}" ]]; then
    echo "Platform: $IMAGE_PLATFORM"
    echo "SoM: $IMAGE_SOM"
  fi
  echo
  echo "Contents:"
  echo "  - Boot partition with kernel, DTB, and initramfs"
  echo "  - Root filesystem with Arch Linux ARM and kernel modules"
  echo "  - Kernel, qcacld2, and lpc installed from AUR packages"
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
  parse_args "$@"
  configure_sysimage

  # Create working directories before opening the logfile in downloads/.
  mkdir -p "$DOWNLOADS_DIR" "$BOOT_MNT" "$ROOT_MNT"

  # Setup logging
  exec > >(tee -a "$LOGFILE") 2>&1
  
  log "Logging to: $LOGFILE"
  log "Started at: $(date)"
  log "Selected sysimage: $SYSIMAGE"
  echo
  
  # Validation
  check_root
  check_required_tools
  echo
  
  # Fail early if bootloader source/build configuration is invalid
  preflight_bootloader_artifact
  
  # Image creation
  create_disk_image
  partition_image
  setup_loop_device
  format_partitions
  mount_partitions
  
  # Download and extract
  download_dependencies
  verify_bootloader_checksum
  install_bootloader_to_image
  install_bootloader_to_boot_partition
  extract_rootfs
  configure_pacman_mirrors
  
  # Filesystem configuration
  create_fstab
  setup_chroot_environment
  
  # Kernel installation
  create_chroot_script
  run_chroot_installation
  resolve_installed_kernel_version
  configure_boot_dtb_symlink
  configure_extlinux
  cleanup_chroot_environment
  
  # Finalization
  reclaim_filesystem_free_space
  cleanup_mounts
  sync
  
  rename_outputs_for_kernel_version
  fix_file_ownership
  generate_bmap
  print_summary
  
  log "Completed at: $(date)"
}

main "$@"
