# SPDX-License-Identifier: MIT
class BuildError(Exception):
    pass


class PatchStats:
    def __init__(self):
        self.success = 0
        self.failed = 0
        self.failed_patches = []
        self.skipped = 0
        self.skipped_patches = []
        self.mnt_found = 0
        self.xtra_success = 0
        self.xtra_failed = 0
        self.xtra_failed_patches = []
        self.xtra_found = 0
        self.verruckt_no_author = []

    @property
    def total(self) -> int:
        return self.success + self.failed

    @property
    def xtra_total(self) -> int:
        return self.xtra_success + self.xtra_failed

    @property
    def has_xtra(self) -> bool:
        return self.xtra_total > 0

    @property
    def has_mnt(self) -> bool:
        return self.mnt_found > 0

    @property
    def has_any(self) -> bool:
        return self.has_mnt or self.xtra_found > 0

    @property
    def no_mnt_patches(self) -> bool:
        return self.mnt_found == 0

    def add_success(self):
        self.success += 1

    def add_failure(self, patch_name: str):
        self.failed += 1
        self.failed_patches.append(patch_name)

    def add_skipped(self, patch_name: str):
        self.skipped += 1
        self.skipped_patches.append(patch_name)

    def set_mnt_found(self, count: int):
        self.mnt_found = count

    def add_xtra_success(self):
        self.xtra_success += 1

    def add_xtra_failure(self, patch_name: str):
        self.xtra_failed += 1
        self.xtra_failed_patches.append(patch_name)

    def set_xtra_found(self, count: int):
        self.xtra_found = count

    def add_verruckt_no_author(self, patch_name: str):
        self.verruckt_no_author.append(patch_name)
