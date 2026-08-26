#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

echo "Inside chroot - initializing pacman keyring..."
pacman-key --init
pacman-key --populate archlinuxarm

PACMAN="pacman --disable-sandbox"
JOBS="$(nproc)"
KERNEL_EXTLINUX_EXAMPLE_TMP="/tmp/linux-mnt-reform-bin.extlinux.conf.example"

refresh_pacman_databases() {
  echo "Refreshing pacman package databases..."
  rm -rf /var/lib/pacman/sync/* || true
  $PACMAN -Syy --noconfirm
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
remove_installed_packages() {
  local installed_packages=()
  local package_name=""

  for package_name in "$@"; do
    if pacman -Qq "$package_name" >/dev/null 2>&1; then
      installed_packages+=("$package_name")
    fi
  done

  if [[ ${#installed_packages[@]} -eq 0 ]]; then
    echo "No matching installed packages to remove."
    return 0
  fi

  echo "Removing installed packages: ${installed_packages[*]}"
  $PACMAN -R --noconfirm "${installed_packages[@]}"
}

remove_installed_packages linux-aarch64 linux-aarch64-headers

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

  # Exclude package names built by this same PKGBUILD. They won't exist in repos.
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

resolve_package_path() {
  local package_dir="$1"
  local package_path="$2"

  if [[ "$package_path" = /* ]]; then
    printf '%s\n' "$package_path"
  else
    printf '%s/%s\n' "$package_dir" "$package_path"
  fi
}

build_and_install_aur_package() {
  local pkgname="$1"
  local aur_repo="https://aur.archlinux.org/${pkgname}.git"
  local aur_dir="/tmp/${pkgname}-aur"
  local packagelist=""
  local built_packages=()
  local package_path=""

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

  if ! packagelist="$(
    cd "$aur_dir"
    sudo -u nobody makepkg --packagelist
  )"; then
    echo "Failed to read built package list in $aur_dir" >&2
    exit 1
  fi

  mapfile -t built_packages < <(
    printf '%s\n' "$packagelist" | sed '/^$/d'
  )

  if [[ ${#built_packages[@]} -eq 0 ]]; then
    echo "No built package archives reported by makepkg in $aur_dir" >&2
    exit 1
  fi

  for package_path in "${!built_packages[@]}"; do
    built_packages[$package_path]="$(resolve_package_path "$aur_dir" "${built_packages[$package_path]}")"
    if [[ ! -f "${built_packages[$package_path]}" ]]; then
      echo "Built package archive not found: ${built_packages[$package_path]}" >&2
      exit 1
    fi
  done

  echo "Installing built package archives: ${built_packages[*]}"
  $PACMAN -U --noconfirm "${built_packages[@]}"
}

echo "Building and installing AUR package linux-mnt-reform-bin..."
build_and_install_aur_package "linux-mnt-reform-bin"

if [[ -f "/tmp/linux-mnt-reform-bin-aur/extlinux.conf.example" ]]; then
  install -Dm644 \
    "/tmp/linux-mnt-reform-bin-aur/extlinux.conf.example" \
    "$KERNEL_EXTLINUX_EXAMPLE_TMP"
fi

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
