#!/bin/bash
set -euo pipefail

usage() {
    cat <<EOF
Usage: $(basename "$0") [-h|--help] VERSION-REVISION

Point docs/index.html at a new kernel release.

  Release line     v7.2.11              -> v7.3.1
  Release tag      7.2.11.reform1       -> 7.3.1.reform1
  Image file name  7.2.11-reform1.img   -> 7.3.1-reform1.img

Examples:
  $(basename "$0") 7.3.1-reform1
  $(basename "$0") 7.3.1-reform2
  $(basename "$0") 7.4-reform1

Options:
  -h, --help  Show this help and exit.
EOF
}

die() {
    echo "Error: $*" >&2
    exit 1
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; die "unknown option: $1" ;;
esac

if [ "$#" -ne 1 ]; then
    usage >&2
    exit 1
fi

ARG="$1"
if ! printf '%s\n' "$ARG" | grep -Eq '^[0-9]+\.[0-9]+(\.[0-9]+)?-[A-Za-z0-9_]+$'; then
    usage >&2
    die "'$ARG' is not of the form #.#[.#]-something, e.g. 7.3.1-reform1"
fi
NEW_VERSION="${ARG%%-*}"
NEW_REV="${ARG#*-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INDEX_HTML="${SCRIPT_DIR}/../docs/index.html"
[ -f "$INDEX_HTML" ] || die "$INDEX_HTML not found."

OLD_VERSION=$(sed -nE 's/.*Release: *<code>v?([0-9]+\.[0-9]+(\.[0-9]+)?)<.*/\1/p' "$INDEX_HTML" | head -n 1)
if [ -z "$OLD_VERSION" ]; then
    echo "Release lines found:" >&2
    grep -n 'Release' "$INDEX_HTML" >&2 || echo "  (none)" >&2
    die "couldn't find the current version on the 'Release: <code>v...' line in $INDEX_HTML."
fi

esc() { printf '%s' "$1" | sed 's/\./\\./g'; }
old_v=$(esc "$OLD_VERSION")

OLD_REV=$(sed -nE "s#.*releases/(tag|download)/${old_v}\.([A-Za-z0-9_]+)-aarch64.*#\2#p" "$INDEX_HTML" | head -n 1)
[ -n "$OLD_REV" ] || die "couldn't find the current revision in a 'releases/tag/${OLD_VERSION}.<revision>-aarch64' link in $INDEX_HTML."

if [ "$OLD_VERSION" = "$NEW_VERSION" ] && [ "$OLD_REV" = "$NEW_REV" ]; then
    echo "Already at ${NEW_VERSION}-${NEW_REV}. Nothing to do."
    exit 0
fi

old_r=$(esc "$OLD_REV")
count_before=$(grep -oE "(^|[^0-9.])${old_v}([^0-9.]|\.[^0-9]|$)" "$INDEX_HTML" | wc -l | tr -d ' ')

# BSD sed (macOS) has no plain -i, so write through a temp file.
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
sed -E \
    -e "s/(^|[^0-9.])${old_v}([.-])${old_r}([^A-Za-z0-9_]|$)/\1${NEW_VERSION}\2${NEW_REV}\3/g" \
    -e "s/(^|[^0-9.])${old_v}([^0-9.]|\.[^0-9]|$)/\1${NEW_VERSION}\2/g" \
    "$INDEX_HTML" > "$tmp"
cat "$tmp" > "$INDEX_HTML"

if [ "$OLD_VERSION" != "$NEW_VERSION" ] &&
   grep -nE "(^|[^0-9.])${old_v}([^0-9.]|\.[^0-9]|$)" "$INDEX_HTML" >&2; then
    echo "Warning: the lines above still contain ${OLD_VERSION}." >&2
fi

echo "Updated ${OLD_VERSION}-${OLD_REV} to ${NEW_VERSION}-${NEW_REV} in docs/index.html (${count_before} place(s))."
echo
grep -n -- "$NEW_VERSION" "$INDEX_HTML"
