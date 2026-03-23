#!/usr/bin/env bash

configure_sysimage() {
  local machine_conf=""

  if machine_conf="$(find_machine_conf_for_sysimage "$SYSIMAGE")"; then
    log "Loading machine config from: $machine_conf"
    if load_machine_conf "$machine_conf"; then
      BOOTLOADER_FILENAME="$(basename "${DTBPATH%.dtb}")-flash.bin"
      return
    fi
    log_warn "Machine config $machine_conf is missing required fields for bootloader handling."
  else
    log_warn "No machine config found for $SYSIMAGE in local reform-tools checkout paths."
  fi

  log_warn "Falling back to hard-coded bootloader metadata for $SYSIMAGE."
  configure_sysimage_fallback
  BOOTLOADER_FILENAME="$(basename "${DTBPATH%.dtb}")-flash.bin"
}

configure_sysimage_fallback() {
  case "$SYSIMAGE" in
    pocket-reform-system-a311d)
      DTBPATH="amlogic/meson-g12b-bananapi-cm4-mnt-pocket-reform.dtb"
      BOOTLOADER_SHA1="c96ea54a1947ce59cc48b1cc2d7d1dce494d8ff9"
      BOOTLOADER_PROJECT="reform-a311d-uboot"
      BOOTLOADER_TAG="2026-01-28"
      BOOTLOADER_OFFSET=512
      FLASHBIN_OFFSET=512
      SD_BOOT=true
      ;;
    reform-next-system-rk3588)
      DTBPATH="rockchip/rk3588-mnt-reform-next.dtb"
      BOOTLOADER_SHA1="cb9a3caaf69cd4458341c1c8c9527a86153ea4a3"
      BOOTLOADER_PROJECT="reform-rk3588-uboot"
      BOOTLOADER_TAG="2026-01-28"
      BOOTLOADER_OFFSET=32768
      FLASHBIN_OFFSET=0
      SD_BOOT=true
      ;;
    pocket-reform-system-rk3588)
      DTBPATH="rockchip/rk3588-mnt-pocket-reform.dtb"
      BOOTLOADER_SHA1="1edcd78a47ce32927e854118bf6bf9b86d2f8fe7"
      BOOTLOADER_PROJECT="reform-rk3588-uboot"
      BOOTLOADER_TAG="2026-01-28"
      BOOTLOADER_OFFSET=32768
      FLASHBIN_OFFSET=0
      SD_BOOT=true
      ;;
    *)
      die "Unsupported --sysimage '$SYSIMAGE'. Supported: pocket-reform-system-a311d, reform-next-system-rk3588, pocket-reform-system-rk3588"
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
  DTBPATH=""
  BOOTLOADER_SHA1=""
  BOOTLOADER_PROJECT=""
  BOOTLOADER_TAG=""
  BOOTLOADER_OFFSET=0
  FLASHBIN_OFFSET=0
  SD_BOOT=false

  # shellcheck source=/dev/null
  . "$conf"

  # Backward compatibility with older machine configs.
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

  [[ -n "${DTBPATH:-}" ]] || return 1
  [[ -n "${BOOTLOADER_PROJECT:-}" ]] || return 1
  [[ -n "${BOOTLOADER_TAG:-}" ]] || return 1
  [[ -n "${BOOTLOADER_SHA1:-}" ]] || return 1

  # FLASHBIN_OFFSET became explicit in newer machine configs.
  if [[ -z "${FLASHBIN_OFFSET:-}" ]]; then
    FLASHBIN_OFFSET=0
  fi

  return 0
}
