# shellcheck shell=bash
# Arch Linux RISC-V (archriscv, https://archriscv.felixc.at/) driver for image-gen.sh.
#   distro_prepare_rootfs
#     -> configure the unpacked rootfs before the chroot
#   distro_install_kernel_files
#     -> unpack the mnt-build kernel tarball into /boot and the rootfs
#   distro_find_extlinux_example
#     -> print the path of extlinux.conf.example, or nothing
#
# There is no riscv64 kernel package, so the kernel comes from the tarball that
# "mnt-build build --arch riscv" produced (image-gen.sh --kernel-tarball). The
# chroot installer builds the initramfs and writes extlinux.conf.example and
# /tmp/linux-mnt-reform-bin.version.

DISTRO_LABEL="Arch Linux RISC-V"
DISTRO_IMAGE_PREFIX="archriscv"
DISTRO_ARCH="riscv64"
DISTRO_NEEDS_KERNEL_TARBALL=true
# Pinned: the project publishes no checksums, so this one was recorded on download.
DISTRO_ROOTFS_URL="https://archriscv.felixc.at/images/archriscv-2026-08-27.tar.zst"
DISTRO_ROOTFS_FILE="archriscv-2026-08-27.tar.zst"
DISTRO_ROOTFS_SHA256="a2045c8b62232db2f60d8e4db610dbb5d9e12856dab0ba08634ad3d7cb7ad498"
DISTRO_CHROOT_INSTALLER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/install.sh"
DISTRO_SUMMARY_NOTE="Kernel installed from the mnt-build kernel tarball"

readonly ARCHRISCV_KERNEL_ENV="/tmp/mnt-kernel.env"

distro_prepare_rootfs() {
  local mirrorlist="$ROOT_MNT/etc/pacman.d/mirrorlist"

  log "Configuring archriscv pacman mirrors..."
  mkdir -p "$(dirname "$mirrorlist")"
  cat > "$mirrorlist" <<'EOF'
Server = https://riscv.mirror.pkgbuild.com/repo/$repo
Server = https://archriscv.felixc.at/repo/$repo
EOF
}

distro_install_kernel_files() {
  local work krel dtb_count

  log "Installing kernel from $KERNEL_TARBALL..."
  work="$(mktemp -d)"
  tar -xzf "$KERNEL_TARBALL" -C "$work"

  [[ -f "$work/arch/riscv/boot/Image" ]] || die "Kernel tarball has no arch/riscv/boot/Image"
  krel="$(ls "$work/lib/modules")"
  [[ -n "$krel" && $(wc -w <<<"$krel") -eq 1 ]] || die "Expected one kernel release in the tarball, got: '$krel'"
  [[ -f "$work/$KERNEL_DTB_STEM-$krel.dtb" ]] || die "Kernel tarball has no $KERNEL_DTB_STEM-$krel.dtb"

  install -Dm644 "$work/arch/riscv/boot/Image" "$BOOT_MNT/Image-$krel"
  mkdir -p "$BOOT_MNT/dtbs"
  dtb_count=0
  for dtb in "$work"/*.dtb; do
    install -m644 "$dtb" "$BOOT_MNT/dtbs/"
    dtb_count=$((dtb_count + 1))
  done
  for cfg in "$work"/config-*; do
    if [[ -f "$cfg" ]]; then install -m644 "$cfg" "$BOOT_MNT/config-$krel"; fi
  done

  mkdir -p "$ROOT_MNT/usr/lib/modules"
  rm -rf "$ROOT_MNT/usr/lib/modules/$krel"
  cp -a "$work/lib/modules/$krel" "$ROOT_MNT/usr/lib/modules/"
  chown -R 0:0 "$ROOT_MNT/usr/lib/modules/$krel"
  rm -rf "$work"

  cat > "$ROOT_MNT$ARCHRISCV_KERNEL_ENV" <<EOF
KREL='$krel'
DTB='$KERNEL_DTB_STEM-$krel.dtb'
BOOTARGS='${BOOTARGS:-}'
EOF
  log "Installed kernel $krel ($dtb_count DTBs) into /boot and /usr/lib/modules"
}

distro_find_extlinux_example() {
  local candidate="$ROOT_MNT/tmp/linux-mnt-reform-bin.extlinux.conf.example"
  if [[ -f "$candidate" ]]; then
    printf '%s\n' "$candidate"
  fi
}
