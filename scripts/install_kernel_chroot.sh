#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

echo "Inside chroot - initializing pacman keyring..."
pacman-key --init
pacman-key --populate archlinuxarm

PACMAN="pacman --disable-sandbox"
JOBS="$(nproc)"

echo "Configuring parallel build settings (jobs=${JOBS})..."
export MAKEFLAGS="-j${JOBS}"
export CMAKE_BUILD_PARALLEL_LEVEL="${JOBS}"
export NINJAFLAGS="-j${JOBS}"

# Prefer parallel DKMS builds for long-running module compiles (e.g. qcacld).
mkdir -p /etc/dkms
cat > /etc/dkms/framework.conf <<EOF
parallel_jobs=${JOBS}
EOF

echo "Upgrading base system (avoid partial upgrade issues)..."
$PACMAN -Syu --noconfirm --ignore linux-aarch64,linux-aarch64-headers

echo "Installing essential packages..."
$PACMAN -S --needed --noconfirm \
  base base-devel dracut networkmanager cpio git dkms sudo

echo "Removing conflicting linux-aarch64 package if present..."
$PACMAN -R --noconfirm linux-aarch64 || true

install_pkgbuild_deps() {
  local pkgdir="$1"
  local srcinfo=""
  local deps=()
  local local_pkg_names=()
  local filtered_deps=()
  local dep=""

  # Collect dependency metadata as non-root (makepkg refuses root).
  if ! srcinfo="$(
    cd "$pkgdir"
    sudo -u nobody makepkg --printsrcinfo
  )"; then
    echo "Failed to read PKGBUILD dependency metadata in $pkgdir" >&2
    exit 1
  fi

  mapfile -t deps < <(
    printf '%s\n' "$srcinfo" \
      | awk -F' = ' '/^[[:space:]]*(depends|makedepends)[[:space:]]*=/ {print $2}' \
      | sed -E 's/[<>=].*$//' \
      | sed -E 's/:.*$//' \
      | sed '/^$/d' \
      | sort -u
  )

  # Exclude package names built by this same PKGBUILD; they won't exist in repos.
  mapfile -t local_pkg_names < <(
    printf '%s\n' "$srcinfo" \
      | awk -F' = ' '/^[[:space:]]*pkgname[[:space:]]*=/ {print $2}' \
      | sed '/^$/d' \
      | sort -u
  )

  for dep in "${deps[@]}"; do
    if printf '%s\n' "${local_pkg_names[@]}" | grep -Fxq "$dep"; then
      continue
    fi
    filtered_deps+=("$dep")
  done

  if [[ ${#filtered_deps[@]} -eq 0 ]]; then
    echo "No additional PKGBUILD dependencies detected for $pkgdir"
    return 0
  fi

  echo "Installing PKGBUILD dependencies for $pkgdir: ${filtered_deps[*]}"
  $PACMAN -S --needed --noconfirm "${filtered_deps[@]}"
}

build_and_install_pkgbuild() {
  local pkgdir="$1"
  local pkgglob="$2"

  chown -R nobody:nobody "$pkgdir"
  install_pkgbuild_deps "$pkgdir"

  echo "Building package from $pkgdir..."
  (
    cd "$pkgdir"
    sudo -u nobody env \
      MAKEFLAGS="$MAKEFLAGS" \
      CMAKE_BUILD_PARALLEL_LEVEL="$CMAKE_BUILD_PARALLEL_LEVEL" \
      NINJAFLAGS="$NINJAFLAGS" \
      makepkg --noconfirm
  )

  local built_packages=("$pkgdir"/$pkgglob)
  if [[ ${#built_packages[@]} -eq 0 ]]; then
    echo "No packages found matching '$pkgglob' in $pkgdir" >&2
    exit 1
  fi

  echo "Installing package(s) from $pkgdir..."
  $PACMAN -U --noconfirm "${built_packages[@]}"
}

build_and_install_aur_package() {
  local pkgname="$1"
  local aur_repo="https://aur.archlinux.org/${pkgname}.git"
  local aur_dir="/tmp/${pkgname}-aur"

  echo "Building and installing AUR package: ${pkgname}..."
  rm -rf "$aur_dir"
  sudo -u nobody git clone --depth 1 "$aur_repo" "$aur_dir"
  chown -R nobody:nobody "$aur_dir"

  install_pkgbuild_deps "$aur_dir"

  (
    cd "$aur_dir"
    sudo -u nobody env \
      MAKEFLAGS="$MAKEFLAGS" \
      CMAKE_BUILD_PARALLEL_LEVEL="$CMAKE_BUILD_PARALLEL_LEVEL" \
      NINJAFLAGS="$NINJAFLAGS" \
      makepkg --noconfirm
  )

  local built_packages=("$aur_dir"/${pkgname}-*.pkg.tar.*)
  if [[ ${#built_packages[@]} -eq 0 ]]; then
    echo "No packages found matching '${pkgname}-*.pkg.tar.*' in $aur_dir" >&2
    exit 1
  fi

  $PACMAN -U --noconfirm "${built_packages[@]}"
}

echo "Building and installing linux-mnt-reform kernel..."
build_and_install_pkgbuild "/tmp/linux-mnt-reform" "linux-mnt-reform-*.pkg.tar.*"

echo "Building and installing AUR package reform-tools..."
build_and_install_aur_package "reform-tools"

echo "Building and installing mnt-reform-qcacld2..."
build_and_install_pkgbuild "/tmp/mnt-reform-qcacld2" "*qcacld*.pkg.tar.*"

echo "Building and installing mnt-reform-lpc..."
build_and_install_pkgbuild "/tmp/mnt-reform-lpc" "*lpc*.pkg.tar.*"

echo "Kernel, qcacld2, lpc, and reform-tools packages installed successfully!"

echo "Cleaning pacman package cache to reduce image size..."
$PACMAN -Scc --noconfirm || true

ls -lh /boot/
