#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

echo "Inside chroot - initializing pacman keyring..."
pacman-key --init
pacman-key --populate archlinuxarm

PACMAN="pacman --disable-sandbox"
JOBS="$(nproc)"
PACMAN_FULL_UPGRADE="${PACMAN_FULL_UPGRADE:-0}"
INSTALL_REFORM_TOOLS="${INSTALL_REFORM_TOOLS:-0}"

refresh_pacman_databases() {
  echo "Refreshing pacman package databases..."
  $PACMAN -Sy --noconfirm
}

pacman_run_with_retry() {
  local attempt=1
  local max_attempts=2
  local rc=0
  while (( attempt <= max_attempts )); do
    if "$@"; then
      return 0
    fi
    rc=$?
    if (( attempt < max_attempts )); then
      echo "pacman command failed (attempt ${attempt}/${max_attempts}); refreshing DB and retrying..."
      refresh_pacman_databases || true
      sleep 2
    fi
    ((attempt++))
  done
  return "$rc"
}

echo "Configuring parallel build settings (jobs=${JOBS})..."
export MAKEFLAGS="-j${JOBS}"
export CMAKE_BUILD_PARALLEL_LEVEL="${JOBS}"
export NINJAFLAGS="-j${JOBS}"

# Prefer parallel DKMS builds for long-running module compiles (e.g. qcacld).
mkdir -p /etc/dkms
cat > /etc/dkms/framework.conf <<EOF
parallel_jobs=${JOBS}
EOF

refresh_pacman_databases

if [[ "$PACMAN_FULL_UPGRADE" == "1" ]]; then
  echo "Upgrading base system (PACMAN_FULL_UPGRADE=1)..."
  pacman_run_with_retry \
    $PACMAN -Syu --noconfirm --ignore linux-aarch64,linux-aarch64-headers
else
  echo "Skipping full system upgrade (PACMAN_FULL_UPGRADE=0)."
fi

echo "Installing essential packages..."
pacman_run_with_retry \
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
  pacman_run_with_retry \
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

if [[ "$INSTALL_REFORM_TOOLS" == "1" ]]; then
  echo "Building and installing AUR package reform-tools (INSTALL_REFORM_TOOLS=1)..."
  build_and_install_aur_package "reform-tools"
else
  echo "Skipping reform-tools install (INSTALL_REFORM_TOOLS=0)."
fi

echo "Building and installing mnt-reform-qcacld2..."
build_and_install_pkgbuild "/tmp/mnt-reform-qcacld2" "*qcacld*.pkg.tar.*"

echo "Building and installing mnt-reform-lpc..."
build_and_install_pkgbuild "/tmp/mnt-reform-lpc" "*lpc*.pkg.tar.*"

if [[ "$INSTALL_REFORM_TOOLS" == "1" ]]; then
  echo "Kernel, qcacld2, lpc, and reform-tools packages installed successfully!"
else
  echo "Kernel, qcacld2, and lpc packages installed successfully!"
fi

echo "Cleaning pacman package cache to reduce image size..."
rm -rf /var/cache/pacman/pkg/* || true
rm -rf /var/lib/pacman/sync/* || true
rm -f /var/lib/pacman/db.lck || true

ls -lh /boot/
