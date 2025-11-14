#!/bin/bash
cd ~/mnt-build/linux || exit 1  # your kernel tree
PATCH_LIST=$(find ~/mnt-build/reform-debian-packages/linux/patches6.17 -name "*.patch" | sort)
SUCCESS=0
FAIL=0

# Remove old failed.log if it exists
rm -f failed.log

for patch in $PATCH_LIST; do
    echo "Applying patch: $patch"
    if patch -p1 --dry-run < "$patch" > /dev/null 2>&1; then
        patch -p1 < "$patch"
        echo "✓ Applied: $patch"
        ((SUCCESS++))
    else
        echo "✗ Failed: $patch"
        echo "========================================" >> failed.log
        echo "Failed patch: $patch" >> failed.log
        echo "----------------------------------------" >> failed.log
        patch -p1 --dry-run < "$patch" >> failed.log 2>&1
        echo >> failed.log
        ((FAIL++))
    fi
done
echo
echo "Patch application complete!"
echo "Succeeded: $SUCCESS"
echo "Failed:    $FAIL"
echo "Total:     $((SUCCESS + FAIL))"

if [ $FAIL -gt 0 ]; then
        echo
            echo "Failed patches logged to: $(pwd)/failed.log"
fi
