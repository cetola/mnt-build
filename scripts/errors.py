# SPDX-License-Identifier: MIT
class BuildError(Exception):
    """Custom exception for build failures."""
    pass


class PatchStats:
    """Track patch application statistics."""

    def __init__(self):
        self.success = 0
        self.failed = 0
        self.failed_patches = []

    @property
    def total(self) -> int:
        return self.success + self.failed

    def add_success(self):
        self.success += 1

    def add_failure(self, patch_name: str):
        self.failed += 1
        self.failed_patches.append(patch_name)
