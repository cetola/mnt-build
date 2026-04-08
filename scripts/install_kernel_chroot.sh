#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

echo "Inside chroot - initializing pacman keyring..."
pacman-key --init
pacman-key --populate archlinuxarm

PACMAN="pacman --disable-sandbox"
JOBS="$(nproc)"

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

echo "Upgrading base system..."
pacman_run_with_retry \
  $PACMAN -Syu --noconfirm --ignore linux-aarch64,linux-aarch64-headers

echo "Installing essential packages..."
pacman_run_with_retry \
  $PACMAN -S --needed --noconfirm \
  base base-devel dracut networkmanager cpio git dkms sudo

echo "Removing conflicting linux-aarch64 package if present..."
$PACMAN -R --noconfirm linux-aarch64 linux-aarch64-headers || true

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

build_and_install_aur_package() {
  local pkgname="$1"
  local aur_repo="https://aur.archlinux.org/${pkgname}.git"
  local aur_dir="/tmp/${pkgname}-aur"
  local srcinfo=""
  local package_names=()
  local built_packages=()
  local package_name=""
  local matches=()

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

  if ! srcinfo="$(
    cd "$aur_dir"
    sudo -u nobody makepkg --printsrcinfo
  )"; then
    echo "Failed to read built package metadata in $aur_dir" >&2
    exit 1
  fi

  mapfile -t package_names < <(
    printf '%s\n' "$srcinfo" \
      | awk -F' = ' '/^[[:space:]]*pkgname[[:space:]]*=/ {print $2}' \
      | sed '/^$/d' \
      | sort -u
  )

  if [[ ${#package_names[@]} -eq 0 ]]; then
    echo "No package names found in $aur_dir" >&2
    exit 1
  fi

  for package_name in "${package_names[@]}"; do
    matches=("$aur_dir"/${package_name}-*.pkg.tar.*)
    if [[ ${#matches[@]} -eq 0 ]]; then
      echo "Missing built package file for declared package '$package_name' in $aur_dir" >&2
      exit 1
    fi
    built_packages+=("${matches[@]}")
  done

  $PACMAN -U --noconfirm "${built_packages[@]}"
}

echo "Building and installing AUR package linux-mnt-reform-bin..."
build_and_install_aur_package "linux-mnt-reform-bin"

echo "Building and installing AUR package reform-tools..."
build_and_install_aur_package "reform-tools"

echo "Building and installing AUR package mnt-reform-qcacld2-dkms..."
build_and_install_aur_package "mnt-reform-qcacld2-dkms"

echo "Building and installing AUR package mnt-reform-lpc-dkms..."
build_and_install_aur_package "mnt-reform-lpc-dkms"

echo "Kernel, qcacld2, lpc, and reform-tools AUR packages installed successfully!"

echo "Recording installed kernel release..."
find /usr/lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
  | sort -V \
  | tail -n 1 > /tmp/linux-mnt-reform-bin.version

echo "Cleaning pacman package cache to reduce image size..."
rm -rf /var/cache/pacman/pkg/* || true
rm -rf /var/lib/pacman/sync/* || true
rm -f /var/lib/pacman/db.lck || true

ls -lh /boot/
