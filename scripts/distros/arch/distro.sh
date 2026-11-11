# shellcheck shell=bash
# Arch Linux ARM driver for image-gen.sh.
#   distro_prepare_rootfs
#     -> configure the unpacked rootfs before the chroot
#   distro_find_extlinux_example
#     -> print the path of extlinux.conf.example, or nothing
#
# The chroot installer must leave /boot populated and write the kernel release
# to /tmp/linux-mnt-reform-bin.version in the rootfs.

DISTRO_LABEL="Arch Linux ARM"
DISTRO_IMAGE_PREFIX="arch"
DISTRO_ROOTFS_URL="http://os.archlinuxarm.org/os/ArchLinuxARM-aarch64-latest.tar.gz"
DISTRO_ROOTFS_FILE="archlinuxarm.tar.gz"
DISTRO_CHROOT_INSTALLER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh"
DISTRO_SUMMARY_NOTE="Kernel and lpc installed from AUR packages"

distro_prepare_rootfs() {
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

distro_find_extlinux_example() {
  local candidate
  for candidate in \
    "$ROOT_MNT/usr/share/doc/linux-mnt-reform-bin/extlinux.conf.example" \
    "$ROOT_MNT/tmp/linux-mnt-reform-bin.extlinux.conf.example" \
    "$SCRIPT_DIR/linux-mnt-reform-bin/extlinux.conf.example"; do
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
}
