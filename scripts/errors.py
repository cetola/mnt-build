# SPDX-License-Identifier: MIT
class BuildError(Exception):
    pass


class PatchStats:
    def __init__(self):
        self.success = 0
        self.failed = 0
        self.failed_patches = []
        self.found = 0

    @property
    def total(self) -> int:
        return self.success + self.failed

    def add_success(self):
        self.success += 1

    def add_failure(self, patch_name: str):
        self.failed += 1
        self.failed_patches.append(patch_name)

    def set_found(self, count: int):
        self.found = count
