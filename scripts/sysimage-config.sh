#!/usr/bin/env bash

readonly SUPPORTED_SYSIMAGES=(
  pocket-reform-system-a311d
  reform-next-system-rk3588
  pocket-reform-system-rk3588
  pocket-reform-system-imx8mp
  pocket-reform-system-rk3588s
)

# Sysimages for which barebox is a supported bootloader option.
# Add an entry here to make --bootloader barebox available for a new machine.
# This only applies to machines loaded from reform-tools/machines/ confs (which
# can't declare BAREBOX_PROJECT themselves); local-machines/ confs set it directly.
readonly BAREBOX_SUPPORTED_SYSIMAGES=(
  reform-next-system-rk3588
  pocket-reform-system-rk3588
  pocket-reform-system-rk3588s
)

# Default barebox repo and ref used for all supported sysimages.
# mnt-reform-barebox layers MNT additions (build.sh, mnt-reform-defconfig, CI)
# as commits on top of upstream barebox releases. The MNT release tags (v2026.06.0
# etc.) track upstream and do NOT include those additions — they land on main after
# each tag. Pin to the latest main SHA until MNT cuts a tag that includes build.sh.
# Update this SHA when pulling in new barebox/MNT changes.
readonly BAREBOX_DEFAULT_PROJECT="mnt-reform-barebox"
readonly BAREBOX_DEFAULT_TAG="1cd690cc061990e2bcef2cd5b1c5aa4cfa749e09"

reset_sysimage_config() {
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
  BAREBOX_PROJECT=""
  BAREBOX_TAG=""
  BAREBOX_ARTIFACT=""
}

set_sysimage_config() {
  local dtbpath="$1"
  local sha1="$2"
  local project="$3"
  local tag="$4"
  local bootloader_offset="${5:-0}"
  local flashbin_offset="${6:-0}"
  local sd_boot="${7:-false}"

  DTBPATH="$dtbpath"
  BOOTLOADER_SHA1="$sha1"
  BOOTLOADER_PROJECT="$project"
  BOOTLOADER_TAG="$tag"
  BOOTLOADER_OFFSET="$bootloader_offset"
  FLASHBIN_OFFSET="$flashbin_offset"
  SD_BOOT="$sd_boot"
}

normalize_bootloader_fields() {
  if [[ -z "${BOOTLOADER_PROJECT:-}" && -n "${UBOOT_PROJECT:-}" ]]; then
    BOOTLOADER_PROJECT="$UBOOT_PROJECT"
  fi
  if [[ -z "${BOOTLOADER_TAG:-}" && -n "${UBOOT_TAG:-}" ]]; then
    BOOTLOADER_TAG="$UBOOT_TAG"
  fi
  if [[ -z "${BOOTLOADER_SHA1:-}" && -n "${UBOOT_SHA1:-}" ]]; then
    BOOTLOADER_SHA1="$UBOOT_SHA1"
  fi
  if [[ "${BOOTLOADER_OFFSET:-0}" == "0" && -n "${UBOOT_OFFSET:-}" ]]; then
    BOOTLOADER_OFFSET="$UBOOT_OFFSET"
  fi

  # FLASHBIN_OFFSET became explicit in newer machine configs.
  if [[ -z "${FLASHBIN_OFFSET:-}" ]]; then
    FLASHBIN_OFFSET=0
  fi
}

validate_sysimage_config() {
  [[ -n "${DTBPATH:-}" ]] || return 1
  [[ -n "${BOOTLOADER_PROJECT:-}" ]] || return 1
  [[ -n "${BOOTLOADER_TAG:-}" ]] || return 1
  [[ -n "${BOOTLOADER_SHA1:-}" ]] || return 1
}

set_bootloader_filename() {
  BOOTLOADER_FILENAME="$(basename "${DTBPATH%.dtb}")-flash.bin"
}

supported_sysimages_csv() {
  local IFS=', '
  printf '%s' "${SUPPORTED_SYSIMAGES[*]}"
}

apply_barebox_defaults() {
  [[ -n "${BAREBOX_PROJECT:-}" ]] && return 0
  local s
  for s in "${BAREBOX_SUPPORTED_SYSIMAGES[@]}"; do
    if [[ "$s" == "$SYSIMAGE" ]]; then
      BAREBOX_PROJECT="$BAREBOX_DEFAULT_PROJECT"
      BAREBOX_TAG="$BAREBOX_DEFAULT_TAG"
      case "$SYSIMAGE" in
        reform-next-system-rk3588)
          BAREBOX_ARTIFACT="barebox-mnt-reform-next-rk3588.img" ;;
        pocket-reform-system-rk3588)
          BAREBOX_ARTIFACT="barebox-mnt-pocket-reform-rk3588.img" ;;
        pocket-reform-system-rk3588s)
          BAREBOX_ARTIFACT="barebox-mnt-pocket-reform-rcm5-rk3588s.img" ;;
      esac
      return 0
    fi
  done
}

configure_sysimage() {
  local machine_conf=""

  if machine_conf="$(find_machine_conf_for_sysimage "$SYSIMAGE")"; then
    log "Loading machine config from: $machine_conf"
    if load_machine_conf "$machine_conf"; then
      set_image_identity_for_sysimage
      set_kernel_dtb_stem_for_sysimage
      set_bootloader_filename
      apply_barebox_defaults
      return
    fi
    log_warn "Machine config $machine_conf is missing required fields for bootloader handling."
  else
    log_warn "No machine config found for $SYSIMAGE in local reform-tools checkout paths."
  fi

  log_warn "Falling back to hard-coded bootloader metadata for $SYSIMAGE."
  configure_sysimage_fallback
  set_image_identity_for_sysimage
  set_kernel_dtb_stem_for_sysimage
  set_bootloader_filename
}

set_image_identity_for_sysimage() {
  case "$SYSIMAGE" in
    pocket-reform-system-a311d)
      IMAGE_PLATFORM="pocket"
      IMAGE_SOM="a311d"
      ;;
    reform-next-system-rk3588)
      IMAGE_PLATFORM="reform-next"
      IMAGE_SOM="rk3588"
      ;;
    pocket-reform-system-rk3588s)
      IMAGE_PLATFORM="pocket"
      IMAGE_SOM="rk3588s"
      ;;
    pocket-reform-system-rk3588)
      IMAGE_PLATFORM="pocket"
      IMAGE_SOM="rk3588"
      ;;
    pocket-reform-system-imx8mp)
      IMAGE_PLATFORM="pocket"
      IMAGE_SOM="imx8mp"
      ;;
    *)
      IMAGE_PLATFORM=""
      IMAGE_SOM=""
      ;;
  esac
}

set_kernel_dtb_stem_for_sysimage() {
  case "$SYSIMAGE" in
    pocket-reform-system-a311d)
      KERNEL_DTB_STEM="meson-g12b-bananapi-cm4-mnt-pocket-reform"
      ;;
    reform-next-system-rk3588)
      KERNEL_DTB_STEM="rk3588-mnt-reform-next"
      ;;
    pocket-reform-system-rk3588s)
      KERNEL_DTB_STEM="rk3588s-mnt-pocket-reform"
      ;;
    pocket-reform-system-rk3588)
      KERNEL_DTB_STEM="rk3588-mnt-pocket-reform"
      ;;
    pocket-reform-system-imx8mp)
      KERNEL_DTB_STEM="imx8mp-mnt-pocket-reform"
      ;;
    *)
      KERNEL_DTB_STEM=""
      ;;
  esac
}

configure_sysimage_fallback() {
  case "$SYSIMAGE" in
    pocket-reform-system-a311d)
      set_sysimage_config \
        "amlogic/meson-g12b-bananapi-cm4-mnt-pocket-reform.dtb" \
        "c96ea54a1947ce59cc48b1cc2d7d1dce494d8ff9" \
        "reform-a311d-uboot" \
        "2026-01-28" \
        512 \
        512 \
        true
      ;;
    reform-next-system-rk3588)
      set_sysimage_config \
        "rockchip/rk3588-mnt-reform-next.dtb" \
        "cb9a3caaf69cd4458341c1c8c9527a86153ea4a3" \
        "reform-rk3588-uboot" \
        "2026-01-28" \
        32768 \
        0 \
        true
      BAREBOX_PROJECT="$BAREBOX_DEFAULT_PROJECT"
      BAREBOX_TAG="$BAREBOX_DEFAULT_TAG"
      BAREBOX_ARTIFACT="barebox-mnt-reform-next-rk3588.img"
      ;;
    pocket-reform-system-rk3588)
      set_sysimage_config \
        "rockchip/rk3588-mnt-pocket-reform.dtb" \
        "1edcd78a47ce32927e854118bf6bf9b86d2f8fe7" \
        "reform-rk3588-uboot" \
        "2026-01-28" \
        32768 \
        0 \
        true
      BAREBOX_PROJECT="$BAREBOX_DEFAULT_PROJECT"
      BAREBOX_TAG="$BAREBOX_DEFAULT_TAG"
      BAREBOX_ARTIFACT="barebox-mnt-pocket-reform-rk3588.img"
      ;;
    pocket-reform-system-rk3588s)
      set_sysimage_config \
        "rockchip/rk3588s-mnt-pocket-reform.dtb" \
        "1edcd78a47ce32927e854118bf6bf9b86d2f8fe7" \
        "reform-rk3588-uboot" \
        "2026-01-28" \
        32768 \
        0 \
        true
      BAREBOX_PROJECT="$BAREBOX_DEFAULT_PROJECT"
      BAREBOX_TAG="$BAREBOX_DEFAULT_TAG"
      BAREBOX_ARTIFACT="barebox-mnt-pocket-reform-rcm5-rk3588s.img"
      ;;
    pocket-reform-system-imx8mp)
      set_sysimage_config \
        "freescale/imx8mp-mnt-pocket-reform.dtb" \
        "7c9b492961e71f2e46c41c9f0f8332c232b85763" \
        "reform-imx8mp-uboot" \
        "2026-01-28" \
        0 \
        0 \
        false
      ;;
    *)
      die "Unsupported --sysimage '$SYSIMAGE'. Supported: $(supported_sysimages_csv)"
      ;;
  esac
}

find_machine_conf_for_sysimage() {
  local wanted_sysimage="$1"
  local candidate_dirs=(
    "$SCRIPT_DIR/../local-machines"
    "$SCRIPT_DIR/../reform-tools/machines"
    "$SCRIPT_DIR/../../reform-tools/machines"
  )
  local conf=""
  local sysimage_from_conf=""

  for dir in "${candidate_dirs[@]}"; do
    [[ -d "$dir" ]] || continue
    while IFS= read -r -d '' conf; do
      sysimage_from_conf="$(awk -F'"' '/^SYSIMAGE="/ { print $2; exit }' "$conf")"
      if [[ "$sysimage_from_conf" == "$wanted_sysimage" ]]; then
        printf "%s\n" "$conf"
        return 0
      fi
    done < <(find "$dir" -maxdepth 1 -type f -name "*.conf" -print0)
  done

  return 1
}

load_machine_conf() {
  local conf="$1"

  # Reset in case this function is called more than once.
  reset_sysimage_config

  # shellcheck source=/dev/null
  . "$conf"

  normalize_bootloader_fields
  validate_sysimage_config

  return $?
}
