#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Print the clone URL of a U-Boot or barebox project from remotes.json."""
import sys
from pathlib import Path

from config import REMOTES_FILE, load_bootloader_url

if len(sys.argv) != 2:
    sys.exit(f"usage: {sys.argv[0]} <project>")
try:
    print(load_bootloader_url(Path(__file__).resolve().parent.parent / REMOTES_FILE, sys.argv[1]))
except ValueError as e:
    sys.exit(f"ERROR: {e}")
