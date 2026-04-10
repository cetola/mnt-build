#!/usr/bin/env bash

readonly SUPPORTED_SYSIMAGES=(
  pocket-reform-system-a311d
  reform-next-system-rk3588
  pocket-reform-system-rk3588
  pocket-reform-system-imx8mp
)

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

configure_sysimage() {
  local machine_conf=""

  if machine_conf="$(find_machine_conf_for_sysimage "$SYSIMAGE")"; then
    log "Loading machine config from: $machine_conf"
    if load_machine_conf "$machine_conf"; then
      set_image_identity_for_sysimage
      set_kernel_dtb_stem_for_sysimage
      set_bootloader_filename
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
