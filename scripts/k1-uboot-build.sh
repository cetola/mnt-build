#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Build the SpacemiT K1 boot chain from an upstream U-Boot checkout:
#   OpenSBI fw_dynamic.bin + SpacemiT DDR firmware -> U-Boot SPL and u-boot.itb
#   -> signed FSBL -> one raw SD blob (bootinfo, FSBL x2, u-boot.itb).
#
# usage: k1-uboot-build.sh <u-boot checkout> <artifact file name>
#
# The artifact lands in the checkout as <artifact file name>. It maps disk
# bytes from offset 0; write it from sector 1 and copy its first 80 bytes
# into sector 0 (machine conf: BOOTLOADER_OFFSET/FLASHBIN_OFFSET=512,
# BOOTLOADER_HEAD_BYTES=80).
#
# Env: UBOOT_DEFCONFIG (required), CROSS_COMPILE (default riscv64-linux-gnu-),
#      K1_FSBL_KEY (RSA-2048 private key). Without it the key is
#      configs/k1/fsbl-signing-key.pem, generated if missing and ignored by git.
set -euo pipefail

# OpenSBI release with the generic SpacemiT K1 platform.
OPENSBI_REF="v1.9"
# spacemit-firmware commit and the DDR training blob it carries.
SPACEMIT_FW_REF="ca39878b1b74c73409a2e0444e4f099d7966021e"
DDR_FW_PATH="k1/v0.2/ddr_fw.bin"
DDR_FW_SHA256="4ca729dae39580fe6d52ff6a1ead95fd18cd6394c942cc4a323882ccf2840148"
# SPL loads u-boot.itb from this byte offset (CONFIG_SYS_MMCSD_RAW_MODE_U_BOOT_SECTOR * 512).
ITB_OFFSET=0x100000
# The blob must end before the first partition (mnt-build images start it at 16 MiB).
MAX_END=$((16 * 1024 * 1024))

die() { echo "k1-uboot-build: ERROR: $*" >&2; exit 1; }
log() { echo "k1-uboot-build: $*"; }

[[ $# -eq 2 ]] || die "usage: $0 <u-boot checkout> <artifact file name>"
SRC="$(cd "$1" && pwd)"
ARTIFACT="$SRC/$2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPS="$(dirname "$SRC")"
CROSS_COMPILE="${CROSS_COMPILE:-riscv64-linux-gnu-}"
KEY="${K1_FSBL_KEY:-$ROOT/configs/k1/fsbl-signing-key.pem}"
JOBS="$(nproc)"

[[ -n "${UBOOT_DEFCONFIG:-}" ]] || die "UBOOT_DEFCONFIG is not set (machine conf)"
[[ -f "$SRC/configs/$UBOOT_DEFCONFIG" ]] || die "$SRC/configs/$UBOOT_DEFCONFIG not found (xtra-patches not applied?)"
for tool in git make openssl python3 sha256sum "${CROSS_COMPILE}gcc"; do
  command -v "$tool" >/dev/null || die "missing tool: $tool"
done
if [[ ! -f "$KEY" ]]; then
  [[ -z "${K1_FSBL_KEY:-}" ]] || die "FSBL signing key not found: $KEY"
  log "generating a new FSBL signing key: $KEY"
  mkdir -p "$(dirname "$KEY")"
  (umask 077 && openssl genrsa -out "$KEY.tmp" 2048) && mv "$KEY.tmp" "$KEY" ||
    die "could not generate $KEY"
fi

# Clone <project> from remotes.json next to the U-Boot checkout and check out <ref>.
checkout_dep() {
  local project="$1" ref="$2" dir="$DEPS/$1" url
  url="$(python3 "$SCRIPT_DIR/bootloader-url.py" "$project")" || die "no URL for $project in remotes.json"
  if [[ ! -d "$dir/.git" ]]; then
    log "cloning $project from $url"
    git clone --quiet "$url" "$dir" || die "clone of $url failed"
  fi
  if ! git -C "$dir" rev-parse --verify --quiet "$ref^{commit}" >/dev/null; then
    log "fetching $ref for $project"
    git -C "$dir" fetch --quiet --tags origin || die "fetch of $project failed"
  fi
  git -C "$dir" -c advice.detachedHead=false checkout --quiet --detach "$ref" || die "checkout of $project $ref failed"
  log "$project at $(git -C "$dir" rev-parse --short HEAD) ($ref)"
}

checkout_dep opensbi "$OPENSBI_REF"
checkout_dep spacemit-firmware "$SPACEMIT_FW_REF"

DDR_FW="$DEPS/spacemit-firmware/$DDR_FW_PATH"
[[ -f "$DDR_FW" ]] || die "$DDR_FW not found"
echo "$DDR_FW_SHA256  $DDR_FW" | sha256sum --check --quiet - || die "$DDR_FW checksum mismatch"

log "building OpenSBI"
SBI_OUT="$DEPS/opensbi/build-mnt"
make -s -C "$DEPS/opensbi" O="$SBI_OUT" CROSS_COMPILE="$CROSS_COMPILE" PLATFORM=generic -j"$JOBS"
OPENSBI="$SBI_OUT/platform/generic/firmware/fw_dynamic.bin"
[[ -f "$OPENSBI" ]] || die "OpenSBI did not produce $OPENSBI"

log "building U-Boot ($UBOOT_DEFCONFIG)"
UB_OUT="$SRC/build-mnt"
rm -rf "$UB_OUT"
make -s -C "$SRC" O="$UB_OUT" ARCH=riscv CROSS_COMPILE="$CROSS_COMPILE" "$UBOOT_DEFCONFIG"
make -s -C "$SRC" O="$UB_OUT" ARCH=riscv CROSS_COMPILE="$CROSS_COMPILE" \
  DDR_FW_FILE="$DDR_FW" OPENSBI="$OPENSBI" -j"$JOBS"
for f in u-boot-spl-ddr.bin u-boot.itb; do
  [[ -f "$UB_OUT/$f" ]] || die "U-Boot did not produce $f"
done

log "signing FSBL and assembling $2"
python3 "$SCRIPT_DIR/k1-boot-image.py" sign-fsbl "$UB_OUT/u-boot-spl-ddr.bin" "$UB_OUT/FSBL.bin" --key "$KEY"
python3 "$SCRIPT_DIR/k1-boot-image.py" assemble "$ARTIFACT" \
  --fsbl "$UB_OUT/FSBL.bin" --itb "$UB_OUT/u-boot.itb" \
  --itb-offset "$ITB_OFFSET" --max-end "$MAX_END"

# Separate files for cards with the vendor GPT layout (fsbl/uboot partitions).
cp "$UB_OUT/FSBL.bin" "$SRC/FSBL.bin"
cp "$UB_OUT/u-boot.itb" "$SRC/u-boot.itb"
(cd "$SRC" && sha256sum "$2" FSBL.bin u-boot.itb > "$2.sha256")
log "done: $ARTIFACT"
