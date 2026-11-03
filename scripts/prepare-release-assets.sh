#!/usr/bin/env bash
# Prepare downloaded image-gen artifacts for upload to a GitHub release.
#
# Usage: prepare-release-assets.sh <artifacts-dir> <out-dir> [label]
#
# <artifacts-dir> holds one subdirectory per image-gen artifact. Logs are
# dropped, the optional label is appended to image and bmap names (with the
# SHA256 regenerated), and the per-image MANIFEST.txt files are merged into
# one. The files to upload end up flat in <out-dir>, one path per line on
# stdout.
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }

[[ $# -ge 2 && $# -le 3 ]] || die "usage: $0 <artifacts-dir> <out-dir> [label]"
SRC="$1"
OUT="$2"
LABEL="${3:-}"

[[ -d "$SRC" ]] || die "$SRC is not a directory"
[[ "$LABEL" =~ ^[A-Za-z0-9._-]*$ ]] || die "label may only contain letters, digits, '.', '_' and '-'"
mkdir -p "$OUT"
[[ -z "$(ls -A "$OUT")" ]] || die "$OUT is not empty"

shopt -s nullglob
artifacts=("$SRC"/*/)
[[ ${#artifacts[@]} -gt 0 ]] || die "no artifact directories in $SRC"

manifests=()
for dir in "${artifacts[@]}"; do
  dir="${dir%/}"
  find "$dir" -type f -name '*.log' -print -delete | sed 's/^/Dropped log: /' >&2

  if [[ -n "$LABEL" ]]; then
    for img in "$dir"/*.img.zst; do
      base="${img%.img.zst}"
      mv "$img" "${base}-${LABEL}.img.zst"
      [[ -f "${base}.img.bmap" ]] && mv "${base}.img.bmap" "${base}-${LABEL}.img.bmap"
      rm -f "${img}.sha256"
      (cd "$dir" && sha256sum "$(basename "${base}-${LABEL}.img.zst")" > "$(basename "${base}-${LABEL}.img.zst").sha256")
    done
  fi

  if [[ -f "$dir/MANIFEST.txt" ]]; then
    manifests+=("$dir/MANIFEST.txt")
  fi

  for f in "$dir"/*; do
    [[ -f "$f" ]] || continue
    name="$(basename "$f")"
    [[ "$name" == MANIFEST.txt ]] && continue
    [[ -e "$OUT/$name" ]] && die "two artifacts both contain $name"
    mv "$f" "$OUT/$name"
  done
done

if [[ ${#manifests[@]} -eq 1 ]]; then
  mv "${manifests[0]}" "$OUT/MANIFEST.txt"
elif [[ ${#manifests[@]} -gt 1 ]]; then
  for m in "${manifests[@]}"; do
    echo "===== $(basename "$(dirname "$m")") ====="
    cat "$m"
    echo
  done > "$OUT/MANIFEST.txt"
fi

files=("$OUT"/*)
[[ ${#files[@]} -gt 0 ]] || die "nothing to publish"
printf '%s\n' "${files[@]}"
