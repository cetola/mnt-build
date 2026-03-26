#!/usr/bin/env bash
set -euo pipefail

# Compare local defconfig against upstream config fragment and validate whether
# upstream settings can actually be applied on the currently checked out kernel.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PY="${ROOT_DIR}/scripts/config.py"
DEFAULT_KERNEL_VERSION="$(awk -F"'" '/^DEFAULT_KERNEL_VERSION[[:space:]]*=/ {print $2; exit}' "$CONFIG_PY")"
BASELINE_CONFIG="${ROOT_DIR}/configs/config-${DEFAULT_KERNEL_VERSION}-mnt-reform-arm64"
UPSTREAM_CONFIG="${ROOT_DIR}/reform-debian-packages/linux/config"
KERNEL_DIR="${ROOT_DIR}/linux"
ARCH="arm64"
IGNORE_FILE="${ROOT_DIR}/configs/check-config-sync.ignore"

usage() {
  cat <<USAGE
Usage:
  scripts/check-config-sync.sh [--baseline-config PATH] [--upstream-config PATH] [--kernel-dir PATH] [--arch ARCH] [--ignore-file PATH]

Defaults:
  --baseline-config  ${BASELINE_CONFIG}
  --upstream-config  ${UPSTREAM_CONFIG}
  --kernel-dir       ${KERNEL_DIR}
  --arch             ${ARCH}
  --ignore-file      ${IGNORE_FILE}

This script warns if upstream fragment settings differ from your resolved baseline config,
and tells you whether those settings actually stick on your current kernel tree.

Ignore file format (whitespace-separated):
  <status> <SYMBOL>
Examples:
  missing-symbol ARM_ROCKCHIP_CPUFREQ
  update-constrained ARM_IMX_CPUFREQ_DT
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --baseline-config)
      BASELINE_CONFIG="$2"; shift 2 ;;
    --upstream-config)
      UPSTREAM_CONFIG="$2"; shift 2 ;;
    --kernel-dir)
      KERNEL_DIR="$2"; shift 2 ;;
    --arch)
      ARCH="$2"; shift 2 ;;
    --ignore-file)
      IGNORE_FILE="$2"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1 ;;
  esac
done

if [[ -z "${DEFAULT_KERNEL_VERSION:-}" ]]; then
  echo "Could not parse DEFAULT_KERNEL_VERSION from: $CONFIG_PY" >&2
  exit 1
fi

for p in "$BASELINE_CONFIG" "$UPSTREAM_CONFIG"; do
  [[ -f "$p" ]] || { echo "Missing file: $p" >&2; exit 1; }
done
[[ -d "$KERNEL_DIR" ]] || { echo "Missing kernel dir: $KERNEL_DIR" >&2; exit 1; }

if [[ ! -f "$KERNEL_DIR/Makefile" ]]; then
  echo "Not a kernel tree: $KERNEL_DIR" >&2
  exit 1
fi

if [[ ! -x "$KERNEL_DIR/scripts/config" ]]; then
  echo "Kernel helper not executable: $KERNEL_DIR/scripts/config" >&2
  exit 1
fi

declare -A IGNORE_MAP=()

load_ignores() {
  local file="$1"
  local status symbol

  [[ -f "$file" ]] || return 0

  while read -r status symbol _; do
    [[ -z "${status:-}" ]] && continue
    [[ "$status" == \#* ]] && continue
    if [[ -z "${symbol:-}" ]]; then
      echo "Invalid ignore entry in $file: '$status'" >&2
      exit 1
    fi
    symbol="${symbol#CONFIG_}"
    IGNORE_MAP["${status}|${symbol}"]=1
  done < "$file"
}

is_ignored() {
  local status="$1"
  local symbol="$2"
  [[ -n "${IGNORE_MAP["${status}|${symbol}"]:-}" || -n "${IGNORE_MAP["*|${symbol}"]:-}" ]]
}

echo "WARNING: Before trusting results, ensure you checked out the correct kernel"
echo "         and ran: ./mnt-build build --olddefconfig --dry-run"
echo
if [[ -f "$IGNORE_FILE" ]]; then
  echo "Using ignore file: $IGNORE_FILE"
else
  echo "Ignore file not found (no ignores applied): $IGNORE_FILE"
fi
echo

load_ignores "$IGNORE_FILE"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

base_o="$tmp/base"
test_o="$tmp/test"
mkdir -p "$base_o" "$test_o"

normalize_config() {
  local in_file="$1"
  local out_file="$2"
  awk '
    /^CONFIG_[A-Za-z0-9_]+=/{
      key=$0; sub(/=.*/, "", key)
      val=$0; sub(/^[^=]*=/, "", val)
      print key "\t" val
    }
    /^# CONFIG_[A-Za-z0-9_]+ is not set$/ {
      print $2 "\tn"
    }
  ' "$in_file" | sort -u > "$out_file"
}

extract_value_from_dotconfig() {
  local symbol="$1"
  local file="$2"
  local line
  line="$(grep -E "^(CONFIG_${symbol}=.*|# CONFIG_${symbol} is not set)$" "$file" | head -n1 || true)"
  if [[ -z "$line" ]]; then
    echo "absent"
  elif [[ "$line" == "# CONFIG_${symbol} is not set" ]]; then
    echo "n"
  else
    echo "${line#CONFIG_${symbol}=}"
  fi
}

symbol_exists_in_kernel() {
  local symbol="$1"
  rg -n "^[[:space:]]*(menuconfig|config)[[:space:]]+${symbol}$" "$KERNEL_DIR" --glob '*Kconfig*' -m 1 >/dev/null 2>&1
}

set_symbol_in_config() {
  local symbol="$1"
  local value="$2"
  local cfg="$3"

  case "$value" in
    y)
      "$KERNEL_DIR/scripts/config" --file "$cfg" --enable "$symbol" >/dev/null ;;
    m)
      "$KERNEL_DIR/scripts/config" --file "$cfg" --module "$symbol" >/dev/null ;;
    n)
      "$KERNEL_DIR/scripts/config" --file "$cfg" --disable "$symbol" >/dev/null ;;
    \"*\")
      local s="$value"
      s="${s#\"}"; s="${s%\"}"
      "$KERNEL_DIR/scripts/config" --file "$cfg" --set-str "$symbol" "$s" >/dev/null ;;
    *)
      "$KERNEL_DIR/scripts/config" --file "$cfg" --set-val "$symbol" "$value" >/dev/null ;;
  esac
}

upstream_norm="$tmp/upstream.tsv"
normalize_config "$UPSTREAM_CONFIG" "$upstream_norm"

cp "$BASELINE_CONFIG" "$base_o/.config"
make -s -C "$KERNEL_DIR" ARCH="$ARCH" O="$base_o" olddefconfig >/dev/null
cp "$base_o/.config" "$test_o/.config"

report="$tmp/report.tsv"
: > "$report"

while IFS=$'\t' read -r sym val; do
  sym="${sym#CONFIG_}"
  base_val="$(extract_value_from_dotconfig "$sym" "$base_o/.config")"

  if ! symbol_exists_in_kernel "$sym"; then
    printf '%s\t%s\t%s\t%s\t%s\n' "$sym" "$val" "$base_val" "absent" "missing-symbol" >> "$report"
    continue
  fi

  set_symbol_in_config "$sym" "$val" "$test_o/.config"
done < "$upstream_norm"

make -s -C "$KERNEL_DIR" ARCH="$ARCH" O="$test_o" olddefconfig >/dev/null

while IFS=$'\t' read -r sym val; do
  sym="${sym#CONFIG_}"
  # Skip if already marked missing.
  if grep -qE "^${sym}[[:space:]]" "$report"; then
    continue
  fi

  base_val="$(extract_value_from_dotconfig "$sym" "$base_o/.config")"
  final_val="$(extract_value_from_dotconfig "$sym" "$test_o/.config")"

  status="unchanged"
  if [[ "$val" != "$base_val" ]]; then
    if [[ "$val" == "$final_val" ]]; then
      status="update-applies"
    else
      status="update-constrained"
    fi
  fi

  printf '%s\t%s\t%s\t%s\t%s\n' "$sym" "$val" "$base_val" "$final_val" "$status" >> "$report"
done < "$upstream_norm"

total="$(wc -l < "$report")"
updates="$(awk -F '\t' '$5=="update-applies" || $5=="update-constrained"{c++} END{print c+0}' "$report")"
applies="$(awk -F '\t' '$5=="update-applies"{c++} END{print c+0}' "$report")"
constrained="$(awk -F '\t' '$5=="update-constrained"{c++} END{print c+0}' "$report")"
missing="$(awk -F '\t' '$5=="missing-symbol"{c++} END{print c+0}' "$report")"
unchanged="$(awk -F '\t' '$5=="unchanged"{c++} END{print c+0}' "$report")"
ignored_updates="$(awk -F '\t' -v file="$IGNORE_FILE" '
  BEGIN {
    while ((getline line < file) > 0) {
      if (line ~ /^[[:space:]]*$/ || line ~ /^[[:space:]]*#/) continue
      split(line, a, /[[:space:]]+/)
      status=a[1]; sym=a[2]
      gsub(/^CONFIG_/, "", sym)
      ig[status "|" sym]=1
      ig["*" "|" sym]=1
    }
    close(file)
  }
  ($5=="update-applies" || $5=="update-constrained") && ((($5 "|" $1) in ig) || (("*" "|" $1) in ig)) { c++ }
  END { print c+0 }
' "$report")"
ignored_missing="$(awk -F '\t' -v file="$IGNORE_FILE" '
  BEGIN {
    while ((getline line < file) > 0) {
      if (line ~ /^[[:space:]]*$/ || line ~ /^[[:space:]]*#/) continue
      split(line, a, /[[:space:]]+/)
      status=a[1]; sym=a[2]
      gsub(/^CONFIG_/, "", sym)
      ig[status "|" sym]=1
      ig["*" "|" sym]=1
    }
    close(file)
  }
  $5=="missing-symbol" && ((($5 "|" $1) in ig) || (("*" "|" $1) in ig)) { c++ }
  END { print c+0 }
' "$report")"
eff_updates=$((updates - ignored_updates))
eff_missing=$((missing - ignored_missing))

echo "Compared upstream fragment against resolved baseline config on current kernel tree"
echo "  baseline config: $BASELINE_CONFIG"
echo "  upstream config: $UPSTREAM_CONFIG"
echo "  kernel dir:      $KERNEL_DIR"
echo "  arch:            $ARCH"
echo
echo "Summary"
echo "  symbols in upstream fragment: $total"
echo "  updates vs your resolved defconfig: $updates"
echo "    - applies cleanly: $applies"
echo "    - constrained by deps/Kconfig: $constrained"
echo "  unchanged/already-matching: $unchanged"
echo "  missing in current kernel tree: $missing"
echo "  ignored (via ignore file): $((ignored_updates + ignored_missing))"
echo "  effective updates (non-ignored): $eff_updates"
echo "  effective missing (non-ignored): $eff_missing"
echo

if [[ "$eff_updates" -eq 0 && "$eff_missing" -eq 0 ]]; then
  echo "clean defconfig (all detected deltas are ignored or already matching)"
elif [[ "$updates" -eq 0 ]]; then
  echo "No upstream config updates detected relative to your resolved baseline config."
else
  echo "Updates detected:"
  while IFS=$'\t' read -r sym up cur res status; do
    [[ "$status" == "update-applies" || "$status" == "update-constrained" ]] || continue
    if is_ignored "$status" "$sym"; then
      continue
    fi
    printf '  - CONFIG_%s: upstream=%s, current=%s, result=%s [%s]\n' "$sym" "$up" "$cur" "$res" "$status"
  done < "$report"
fi

if [[ "$eff_missing" -gt 0 ]]; then
  echo
  echo "Symbols present in upstream fragment but missing in this kernel tree:"
  while IFS=$'\t' read -r sym up cur _ status; do
    [[ "$status" == "missing-symbol" ]] || continue
    if is_ignored "$status" "$sym"; then
      continue
    fi
    printf '  - CONFIG_%s (upstream=%s, current=%s)\n' "$sym" "$up" "$cur"
  done < "$report"
fi
