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

require_clean_source_tree() {
  local dirty=0

  if [[ -f "$KERNEL_DIR/.config" ]]; then
    echo "Removing in-tree .config: $KERNEL_DIR/.config"
    rm -f "$KERNEL_DIR/.config"
  fi
  if [[ -d "$KERNEL_DIR/include/config" ]]; then
    echo "Removing in-tree config headers: $KERNEL_DIR/include/config"
    rm -rf "$KERNEL_DIR/include/config"
  fi
  if [[ -d "$KERNEL_DIR/arch/$ARCH/include/generated" ]]; then
    echo "Removing in-tree generated arch headers: $KERNEL_DIR/arch/$ARCH/include/generated"
    rm -rf "$KERNEL_DIR/arch/$ARCH/include/generated"
  fi
}

run_logged() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  "$@"
}

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

echo "WARNING: Before trusting results, ensure you checked out the correct kernel."
echo
if [[ -f "$IGNORE_FILE" ]]; then
  echo "Using ignore file: $IGNORE_FILE"
else
  echo "Ignore file not found (no ignores applied): $IGNORE_FILE"
fi
echo

load_ignores "$IGNORE_FILE"
require_clean_source_tree

echo "Running mnt-build --olddefconfig --dry-run to apply patches and prepare kernel tree..."
if ! "$ROOT_DIR/mnt-build" build --dry-run; then
      echo "Error: mnt-build --olddefconfig --dry-run failed. Cannot continue." >&2
        exit 1
fi
echo

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

base_o="$tmp/base"
test_o="$tmp/test"
mkdir -p "$base_o" "$test_o"

DEFCONFIG="${ROOT_DIR}/configs/defconfig_arm64"

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

# For a given symbol, find its Kconfig file, extract all "depends on" lines
# (including \ continuations), then check each referenced symbol against the
# resolved test config. Prints any dependency not at "y".
find_constraining_deps() {
  local symbol="$1"
  local resolved_config="$2"

  # Find the Kconfig file that defines this symbol.
  local kconfig_file
  kconfig_file="$(rg -l "^[[:space:]]*(menuconfig|config)[[:space:]]+${symbol}$" \
    "$KERNEL_DIR" --glob '*Kconfig*' -m 1 2>/dev/null || true)"
  [[ -z "$kconfig_file" ]] && return 0

  # Use awk to extract all "depends on" expressions for this symbol's block,
  # handling backslash-continuation lines and stopping at the next entry.
  local dep_expr
  dep_expr="$(awk -v sym="$symbol" '
    /^[[:space:]]*(menuconfig|config)[[:space:]]+/ {
      if (in_block) { exit }
      if ($0 ~ "^[[:space:]]*(menuconfig|config)[[:space:]]+" sym "$") { in_block=1 }
      next
    }
    in_block && /^[[:space:]]*depends[[:space:]]+on[[:space:]]/ {
      line = $0
      sub(/^[[:space:]]*depends[[:space:]]+on[[:space:]]/, "", line)
      while (line ~ /\\$/) {
        sub(/\\$/, "", line)
        expr = expr " " line
        if ((getline line) <= 0) break
      }
      expr = expr " " line
    }
    END { print expr }
  ' "$kconfig_file")"

  [[ -z "${dep_expr// }" ]] && return 0

  # Extract bare symbol names — uppercase words that are not Kconfig keywords.
  local dep_symbols
  dep_symbols="$(echo "$dep_expr" | \
    grep -oE '[A-Z][A-Z0-9_]+' | \
    grep -vE '^(y|m|n|AND|OR|NOT)$' | \
    sort -u)"

  while IFS= read -r dep; do
    [[ -z "$dep" ]] && continue
    local dep_val
    dep_val="$(extract_value_from_dotconfig "$dep" "$resolved_config")"
    if [[ "$dep_val" != "y" ]]; then
      printf '      dependency %-40s = %s\n' "CONFIG_${dep}" "${dep_val}"
    fi
  done <<< "$dep_symbols"

  return 0
}

# ── Defconfig health check ────────────────────────────────────────────────────
# Compares defconfig_arm64 directly against the pre-resolved full config for
# this kernel version. No Kconfig tree scanning — just a fast file diff.
#
#   stale-symbol  — symbol in your defconfig is absent from the resolved config
#                   (removed or never existed in this kernel version)
#   overridden    — symbol exists in the resolved config but at a different value
#                   than you intended (a dependency is forcing it elsewhere)
#
# Either finding causes the script to exit so the defconfig is fixed first.

# Delete the defconfig lines for every stale symbol in the health report.
# Kernel symbol names are [A-Za-z0-9_] only, so they're regex-safe.
remove_stale_symbols() {
  local defconfig="$1"
  local report="$2"

  local stale_syms=()
  local sym intended_val resolved_val status
  while IFS=$'\t' read -r sym intended_val resolved_val status; do
    [[ "$status" == "stale-symbol" ]] || continue
    stale_syms+=("$sym")
  done < "$report"

  [[ "${#stale_syms[@]}" -eq 0 ]] && return 0

  # Build one alternation matching each symbol's set or "is not set" line forms.
  local pattern="" s
  for s in "${stale_syms[@]}"; do
    pattern+="^CONFIG_${s}=|^# CONFIG_${s} is not set\$|"
  done
  pattern="${pattern%|}"

  local tmpfile
  tmpfile="$(mktemp)"
  grep -vE "$pattern" "$defconfig" > "$tmpfile"
  mv "$tmpfile" "$defconfig"

  for s in "${stale_syms[@]}"; do
    echo "  removed CONFIG_${s}"
  done
}

run_health_check() {
  local defconfig="$1"
  local resolved_config="$2"   # pre-resolved full config, e.g. config-7.0.11-mnt-reform-arm64

  echo "── Defconfig health check ──────────────────────────────────────────────"
  echo "  defconfig:       $defconfig"
  echo "  resolved config: $resolved_config"
  echo

  local health_report="$tmp/health.tsv"
  : > "$health_report"

  local defconfig_norm="$tmp/defconfig_norm.tsv"
  local resolved_norm="$tmp/resolved_norm.tsv"
  normalize_config "$defconfig" "$defconfig_norm"
  normalize_config "$resolved_config" "$resolved_norm"

  # Join on symbol name; flag anything that differs or is absent in the resolved config.
  awk -F'\t' '
    NR==FNR {
      intended[substr($1,8)] = $2
      next
    }
    {
      sym = substr($1,8)
      if (sym in intended) {
        if (intended[sym] != $2)
          printf "%s\t%s\t%s\toverridden\n", sym, intended[sym], $2
        delete intended[sym]
      }
    }
    END {
      for (sym in intended)
        printf "%s\t%s\tabsent\tstale-symbol\n", sym, intended[sym]
    }
  ' "$defconfig_norm" "$resolved_norm" >> "$health_report"

  local stale_count overridden_count
  stale_count="$(awk -F'\t' '$4=="stale-symbol"{c++} END{print c+0}' "$health_report")"
  overridden_count="$(awk -F'\t' '$4=="overridden"{c++} END{print c+0}' "$health_report")"

  if [[ "$stale_count" -eq 0 && "$overridden_count" -eq 0 ]]; then
    echo "Defconfig health check passed — no stale or overridden symbols found."
    echo
    return 0
  fi

  if [[ "$stale_count" -gt 0 ]]; then
    echo "Stale symbols (not present in the resolved config for this kernel version):"
    while IFS=$'\t' read -r sym intended_val resolved_val status; do
      [[ "$status" == "stale-symbol" ]] || continue
      printf '  - CONFIG_%s (set to: %s)\n' "$sym" "$intended_val"
    done < "$health_report"
    echo
  fi

  if [[ "$overridden_count" -gt 0 ]]; then
    echo "Overridden symbols (your intended value differs from the resolved config):"
    while IFS=$'\t' read -r sym intended_val resolved_val status; do
      [[ "$status" == "overridden" ]] || continue
      printf '  - CONFIG_%s: intended=%s, resolved=%s\n' "$sym" "$intended_val" "$resolved_val"
    done < "$health_report"
    echo
  fi

  # Offer to remove all stale symbols in one go.
  local removed_stale=0
  if [[ "$stale_count" -gt 0 ]]; then
    read -r -p "Do you want to remove all stale symbols? (Y/n): " stale_answer
    stale_answer="${stale_answer:-Y}"
    if [[ "$stale_answer" =~ ^[Yy]$ ]]; then
      echo "Removing stale symbols from $defconfig..."
      remove_stale_symbols "$defconfig" "$health_report"
      removed_stale=1
      echo
    else
      echo "Leaving stale symbols in place."
      echo
    fi
  fi

  # Block continuing if stale symbols remain (user declined removal), or if any
  # overridden symbols are present (those must be resolved by hand).
  if [[ "$stale_count" -gt 0 && "$removed_stale" -eq 0 ]]; then
    echo "Stale symbols remain — fix your defconfig before running the upstream sync check."
    exit 1
  fi

  if [[ "$overridden_count" -gt 0 ]]; then
    echo "Overridden symbols must be resolved manually before continuing:"
    echo "  correct the value, or remove the entry if the resolved default is acceptable."
    exit 1
  fi

  echo "Defconfig health check passed."
  echo
  return 0
}

# ── Upstream comparison ───────────────────────────────────────────────────────

run_health_check "$DEFCONFIG" "$BASELINE_CONFIG"

echo "Resolving baseline config with olddefconfig..."
cp "$BASELINE_CONFIG" "$base_o/.config"
run_logged make -s -C "$KERNEL_DIR" ARCH="$ARCH" O="$base_o" olddefconfig >/dev/null
cp "$base_o/.config" "$test_o/.config"

upstream_norm="$tmp/upstream.tsv"
echo "Normalizing upstream config fragment..."
normalize_config "$UPSTREAM_CONFIG" "$upstream_norm"

report="$tmp/report.tsv"
: > "$report"

# Pass 1: identify missing symbols, record raw diffs, and apply all present
# symbols to the scratch config in one batch.
echo "Scanning upstream symbols and applying to scratch config..."
while IFS=$'\t' read -r sym val; do
  sym="${sym#CONFIG_}"
  base_val="$(extract_value_from_dotconfig "$sym" "$base_o/.config")"

  if ! symbol_exists_in_kernel "$sym"; then
    printf '%s\t%s\t%s\t%s\t%s\n' "$sym" "$val" "$base_val" "absent" "missing-symbol" >> "$report"
    continue
  fi

  # Record the raw diff now; resolved result filled in after olddefconfig.
  if [[ "$val" != "$base_val" ]]; then
    printf '%s\t%s\t%s\t%s\t%s\n' "$sym" "$val" "$base_val" "pending" "pending" >> "$report"
  else
    printf '%s\t%s\t%s\t%s\t%s\n' "$sym" "$val" "$base_val" "$base_val" "unchanged" >> "$report"
  fi

  set_symbol_in_config "$sym" "$val" "$test_o/.config"
done < "$upstream_norm"

# Pass 2: resolve all upstream changes together in one olddefconfig run,
# so inter-symbol dependencies are handled correctly.
echo "Resolving all upstream changes together with olddefconfig..."
run_logged make -s -C "$KERNEL_DIR" ARCH="$ARCH" O="$test_o" olddefconfig >/dev/null

# Pass 3: fill in resolved results for all pending entries.
echo "Recording resolved results..."
resolved_report="$tmp/report_resolved.tsv"
: > "$resolved_report"
while IFS=$'\t' read -r sym val base_val res status; do
  if [[ "$status" == "pending" ]]; then
    final_val="$(extract_value_from_dotconfig "$sym" "$test_o/.config")"
    if [[ "$val" == "$final_val" ]]; then
      status="update-applies"
    else
      status="update-constrained"
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' "$sym" "$val" "$base_val" "$final_val" "$status" >> "$resolved_report"
  else
    printf '%s\t%s\t%s\t%s\t%s\n' "$sym" "$val" "$base_val" "$res" "$status" >> "$resolved_report"
  fi
done < "$report"
mv "$resolved_report" "$report"

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
echo "  symbols in upstream fragment:              $total"
echo "  differ from your defconfig:                $updates"
echo "    - resolve cleanly to upstream value:     $applies"
echo "    - Kconfig constrains to different value: $constrained"
echo "  already matching:                          $unchanged"
echo "  not present in this kernel tree:           $missing"
echo "  ignored via ignore file:                   $((ignored_updates + ignored_missing))"
echo "  actionable differences (non-ignored):      $eff_updates"
echo "  actionable missing (non-ignored):          $eff_missing"
echo

if [[ "$eff_updates" -eq 0 && "$eff_missing" -eq 0 ]]; then
  echo "Your defconfig is in sync with upstream (all differences are ignored or already matching)."
elif [[ "$updates" -eq 0 ]]; then
  echo "No differences detected between upstream and your resolved defconfig."
else
  echo "Settings that differ from upstream:"
  while IFS=$'\t' read -r sym up cur res status; do
    [[ "$status" == "update-applies" || "$status" == "update-constrained" ]] || continue
    if is_ignored "$status" "$sym"; then
      continue
    fi
    if [[ "$status" == "update-applies" ]]; then
      printf '  - CONFIG_%s: upstream=%s, yours=%s → resolves cleanly to: %s\n' "$sym" "$up" "$cur" "$res"
    else
      printf '  - CONFIG_%s: upstream=%s, yours=%s → Kconfig constrains to: %s\n' "$sym" "$up" "$cur" "$res"
      find_constraining_deps "$sym" "$test_o/.config"
    fi
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

# Collect ignored symbols that are still differing from upstream, so the user
# can review them before deciding whether to update.
ignored_differing=()
while IFS=$'\t' read -r sym up cur res status; do
  [[ "$status" == "update-applies" || "$status" == "update-constrained" ]] || continue
  if is_ignored "$status" "$sym"; then
    ignored_differing+=("$sym	$up	$cur	$res	$status")
  fi
done < "$report"

if [[ "${#ignored_differing[@]}" -gt 0 ]]; then
  echo
  echo "The following symbols are on your ignore list but still differ from upstream."
  echo "You may want to confirm these are still intentional:"
  for entry in "${ignored_differing[@]}"; do
    IFS=$'\t' read -r sym up cur res status <<< "$entry"
    if [[ "$status" == "update-applies" ]]; then
      printf '  - CONFIG_%s: upstream=%s, yours=%s → would resolve to: %s  [ignored]\n' "$sym" "$up" "$cur" "$res"
    else
      printf '  - CONFIG_%s: upstream=%s, yours=%s → Kconfig constrains to: %s  [ignored]\n' "$sym" "$up" "$cur" "$res"
    fi
  done
fi

if [[ "$eff_updates" -eq 0 ]]; then
  exit 0
fi

if [[ ! -f "$DEFCONFIG" ]]; then
  echo
  echo "Note: defconfig not found at $DEFCONFIG — skipping update prompt."
  exit 0
fi

echo
read -r -p "Do you want to update your defconfig with the settings that differ from upstream? (Y/n): " answer
answer="${answer:-Y}"
if [[ ! "$answer" =~ ^[Yy]$ ]]; then
  echo "Skipping defconfig update."
  exit 0
fi

# If there are ignored-but-differing symbols, ask whether to include them too.
include_ignored=0
if [[ "${#ignored_differing[@]}" -gt 0 ]]; then
  echo
  read -r -p "Some differing symbols are on your ignore list. Update those too? (y/N): " ig_answer
  ig_answer="${ig_answer:-N}"
  if [[ "$ig_answer" =~ ^[Yy]$ ]]; then
    include_ignored=1
  fi
fi

# Apply selected upstream values to the already-resolved scratch config,
# then use savedefconfig to produce a clean minimal defconfig.
echo
echo "Applying selected upstream values..."
while IFS=$'\t' read -r sym up cur res status; do
  [[ "$status" == "update-applies" || "$status" == "update-constrained" ]] || continue
  if is_ignored "$status" "$sym" && [[ "$include_ignored" -eq 0 ]]; then
    continue
  fi
  set_symbol_in_config "$sym" "$up" "$test_o/.config"
  echo "  set CONFIG_${sym}=${up}"
done < "$report"

echo
echo "Generating minimal defconfig via savedefconfig..."
run_logged make -s -C "$KERNEL_DIR" ARCH="$ARCH" O="$test_o" savedefconfig

cp "$test_o/defconfig" "$DEFCONFIG"
echo
echo "Written to: $DEFCONFIG"
echo "You may want to review and commit this file."
