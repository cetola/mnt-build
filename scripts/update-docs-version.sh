#!/bin/bash
# update-docs-version.sh NEW_VERSION
#
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 NEW_VERSION" >&2
    echo "Example: $0 7.2.2" >&2
    exit 1
fi

NEW_VERSION="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INDEX_HTML="${SCRIPT_DIR}/../docs/index.html"

if [ ! -f "$INDEX_HTML" ]; then
    echo "Error: $INDEX_HTML not found." >&2
    exit 1
fi

OLD_VERSION=$(grep -oP 'Release: <code>v\K[0-9]+\.[0-9]+\.[0-9]+' "$INDEX_HTML" || true)
if [ -z "$OLD_VERSION" ]; then
    echo "Error: couldn't find the current version on the 'Release: <code>v...' line in $INDEX_HTML." >&2
    exit 1
fi

if [ "$OLD_VERSION" = "$NEW_VERSION" ]; then
    echo "Already at $NEW_VERSION -- nothing to do."
    exit 0
fi

count_before=$(grep -o -- "$OLD_VERSION" "$INDEX_HTML" | wc -l)

old_pattern=$(printf '%s' "$OLD_VERSION" | sed 's/\./\\./g')
sed -i "s/${old_pattern}/${NEW_VERSION}/g" "$INDEX_HTML"

echo "Replaced $count_before occurrence(s) of '$OLD_VERSION' with '$NEW_VERSION' in docs/index.html"
echo
grep -n -- "$NEW_VERSION" "$INDEX_HTML"
