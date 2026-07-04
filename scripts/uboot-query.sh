#!/usr/bin/env bash
# uboot-query.sh
#
# Sourceable config bridge between sysimage-config.sh and the Python uboot module.
#
# Usage:
#   uboot-query.sh                — print supported sysimage names, one per line
#   uboot-query.sh <sysimage>     — print KEY=value config lines for one sysimage
#
# Output keys (single sysimage mode):
#   BOOTLOADER_PROJECT, BOOTLOADER_TAG,
#   CONFIG_SOURCE_PATH  (absolute path to .conf file, or sysimage-config.sh for fallback)
#   CONFIG_SOURCE_IS_FALLBACK  (1 if hardcoded fallback was used, 0 if .conf file loaded)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Stub logging — we only want structured output on stdout.
log()      { :; }
log_warn() { :; }
die()      { echo "ERROR: $*" >&2; exit 1; }

# shellcheck source=/dev/null
source "$SCRIPT_DIR/sysimage-config.sh"

if [[ $# -eq 0 ]]; then
    for s in "${SUPPORTED_SYSIMAGES[@]}"; do
        printf '%s\n' "$s"
    done
    exit 0
fi

SYSIMAGE="$1"

# Detect config source before configure_sysimage loads it (both call
# find_machine_conf_for_sysimage; the second call is cheap — just a file scan).
CONFIG_SOURCE_PATH=""
CONFIG_SOURCE_IS_FALLBACK=0
if machine_conf="$(find_machine_conf_for_sysimage "$SYSIMAGE" 2>/dev/null)"; then
    CONFIG_SOURCE_PATH="$machine_conf"
    CONFIG_SOURCE_IS_FALLBACK=0
else
    CONFIG_SOURCE_PATH="${SCRIPT_DIR}/sysimage-config.sh"
    CONFIG_SOURCE_IS_FALLBACK=1
fi

reset_sysimage_config
configure_sysimage

printf 'BOOTLOADER_PROJECT=%s\n'        "$BOOTLOADER_PROJECT"
printf 'BOOTLOADER_TAG=%s\n'            "$BOOTLOADER_TAG"
printf 'BOOTLOADER_FILENAME=%s\n'       "$BOOTLOADER_FILENAME"
printf 'CONFIG_SOURCE_PATH=%s\n'        "$CONFIG_SOURCE_PATH"
printf 'CONFIG_SOURCE_IS_FALLBACK=%s\n' "$CONFIG_SOURCE_IS_FALLBACK"
