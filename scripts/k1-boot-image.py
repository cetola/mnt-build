#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the SpacemiT K1 boot images: signed FSBL, bootinfo, and the raw SD blob.

sign-fsbl  Wrap an SPL in the signed image the BootROM expects
           (U-Boot doc/board/spacemit/smtimage.rst):

             0x000  root RSA-2048 modulus            0x100
             0x100  header0                          0x020
             0x120  key table + root key hash        0x1e0
             0x300  OEM public keys + reserved       0x800
             0xb00  signature0 over [0x100, 0xb00)   0x100
             0xc00  zero padding                     0x3e0
             0xfe0  header1                          0x020
             0x1000 SPL payload, 32-byte aligned
                    signature1 over header1+payload  0x100

           Signatures are PKCS#1 v1.5 SHA-256 from `openssl dgst`, so the
           output is reproducible for a given key. Without a root key hash in
           eFuse the K1 accepts any key pair carried in the image.

bootinfo   80-byte SD boot descriptor for disk bytes 0..79. It tells the
           BootROM where the FSBL copies are.

assemble   One blob that maps disk bytes from offset 0: bootinfo, FSBL at
           spl0 and spl1, u-boot.itb at --itb-offset. Bytes 80..511 are zero;
           write sector 0 separately so the partition table survives.
"""
import argparse
import binascii
import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

RSA_BYTES = 256
HEADER_PAD = b"\xa5" * 8
KEY_NAMES = ("spl", "uboot", "kernel", "rootfs")
BOOTINFO_MAGIC = 0xB00714F0
BOOTINFO_SIZE = 80
SPL0_OFFSET = 0x20000
SPL1_OFFSET = 0x80000
SPL_SIZE_LIMIT = 0x36000
ITB_OFFSET = 0x100000


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def int0(s: str) -> int:
    return int(s, 0)


def openssl(*args: str) -> bytes:
    result = subprocess.run(["openssl", *args], capture_output=True)
    if result.returncode != 0:
        die(f"openssl {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def modulus(key: Path) -> bytes:
    pub = "PUBLIC KEY" in key.read_text().splitlines()[0]
    text = openssl("rsa", *(["-pubin"] if pub else []), "-in", str(key), "-noout", "-modulus").decode()
    match = re.match(r"Modulus=([0-9A-F]+)\s*$", text)
    if not match:
        die(f"cannot read RSA modulus from {key}")
    n = bytes.fromhex(match.group(1)).lstrip(b"\x00")
    if len(n) != RSA_BYTES:
        die(f"{key} is not an RSA-2048 key ({len(n) * 8} bit modulus)")
    return n


def sign(key: Path, data: bytes) -> bytes:
    with tempfile.NamedTemporaryFile() as f:
        f.write(data)
        f.flush()
        sig = openssl("dgst", "-sha256", "-sign", str(key), f.name)
    if len(sig) != RSA_BYTES:
        die(f"unexpected signature length {len(sig)}")
    return sig


def image_header(image_size: int, version: int) -> bytes:
    return (b"AIHD" + bytes([version, 0]) + b"\0\0"
            + image_size.to_bytes(8, "little") + (0).to_bytes(8, "little") + HEADER_PAD)


def build_fsbl(spl: bytes, root_key: Path, spl_key: Path, oem_pub: list, version: int) -> bytes:
    root_n = modulus(root_key)

    table = (0).to_bytes(4, "little") + len(KEY_NAMES).to_bytes(4, "little")
    for key_id, name in enumerate(KEY_NAMES):
        table += name.encode().ljust(16, b"\0") + key_id.to_bytes(4, "little")
    keydata = table + bytes(320) + hashlib.sha256(root_n).digest() + bytes(40)
    assert len(keydata) == 0x1e0

    oem = modulus(spl_key) + b"".join(modulus(k) for k in oem_pub) + bytes(1024)
    assert len(oem) == 0x800

    signed0 = image_header(0x1000, version) + keydata + oem
    prefix = root_n + signed0 + sign(root_key, signed0) + bytes(0x3e0)
    assert len(prefix) == 0xfe0

    payload = spl + bytes(-len(spl) % 32)
    signed1 = image_header(len(payload), version) + payload
    return prefix + signed1 + sign(spl_key, signed1)


def build_bootinfo(spl0: int, spl1: int, limit: int) -> bytes:
    le = lambda v, n: v.to_bytes(n, "little")
    hdr = (le(BOOTINFO_MAGIC, 4) + le(0x00010001, 4) + b"SDC\0"
           + le(0, 1) + le(0, 1) + le(0, 2)          # mid, reserved, did
           + le(512, 4) + le(0x10000, 4) + le(0x10000000, 4)  # page, block, total size
           + le(0, 1) + bytes(3)                     # multi_plane, reserved
           + le(spl0, 4) + le(spl1, 4) + le(limit, 4)
           + le(0, 4) + le(0, 4) + bytes(12))        # partition table offsets, reserved
    assert len(hdr) == 64
    blob = hdr + le(binascii.crc32(hdr), 4) + bytes(12)
    assert len(blob) == BOOTINFO_SIZE
    return blob


def cmd_sign_fsbl(args) -> int:
    spl = args.spl.read_bytes()
    if not spl:
        die(f"{args.spl} is empty")
    if len(spl) > args.max_size:
        die(f"{args.spl} is {len(spl)} bytes, larger than {args.max_size:#x}")
    image = build_fsbl(spl, args.key, args.spl_key or args.key,
                       args.oem_pub or [args.key] * 3, args.version)
    args.output.write_bytes(image)
    print(f"{args.output}: {len(image)} bytes, payload {len(spl)} bytes")
    return 0


def cmd_bootinfo(args) -> int:
    args.output.write_bytes(build_bootinfo(args.spl0, args.spl1, args.limit))
    print(f"{args.output}: bootinfo spl0={args.spl0:#x} spl1={args.spl1:#x} limit={args.limit:#x}")
    return 0


def cmd_assemble(args) -> int:
    bootinfo = build_bootinfo(args.spl0, args.spl1, SPL_SIZE_LIMIT)
    fsbl = args.fsbl.read_bytes()
    itb = args.itb.read_bytes()
    if fsbl[0x100:0x104] != b"AIHD" or fsbl[0xfe0:0xfe4] != b"AIHD":
        die(f"{args.fsbl} is not a signed FSBL image")
    if len(fsbl) - 0x1000 - RSA_BYTES > SPL_SIZE_LIMIT:
        die(f"{args.fsbl} payload exceeds the BootROM limit {SPL_SIZE_LIMIT:#x}")

    regions = [(0, bootinfo, "bootinfo"), (args.spl0, fsbl, "fsbl0")]
    if args.spl1:
        regions.append((args.spl1, fsbl, "fsbl1"))
    regions.append((args.itb_offset, itb, "u-boot.itb"))
    regions.sort()
    for (a, da, na), (b, _, nb) in zip(regions, regions[1:]):
        if a + len(da) > b:
            die(f"{na} at {a:#x} ({len(da)} bytes) overlaps {nb} at {b:#x}")

    end = max(off + len(data) for off, data, _ in regions)
    blob = bytearray(end + (-end % 512))
    for off, data, _ in regions:
        blob[off:off + len(data)] = data
    if args.max_end and len(blob) > args.max_end:
        die(f"blob ends at {len(blob):#x}, beyond --max-end {args.max_end:#x}")
    args.output.write_bytes(blob)
    for off, data, name in regions:
        print(f"  {off:#09x} {name} ({len(data)} bytes)")
    print(f"{args.output}: {len(blob)} bytes")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sign-fsbl", help="sign an SPL payload")
    p.add_argument("spl", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--key", type=Path, required=True,
                   help="RSA-2048 private key; used for every slot unless overridden")
    p.add_argument("--spl-key", type=Path, help="private key for signature1 (default: --key)")
    p.add_argument("--oem-pub", type=Path, nargs=3, metavar=("UBOOT", "KERNEL", "ROOTFS"),
                   help="keys for the uboot/kernel/rootfs slots (default: --key)")
    p.add_argument("--version", type=int, default=1, help="anti-rollback version (default: 1)")
    p.add_argument("--max-size", type=int0, default=SPL_SIZE_LIMIT,
                   help=f"refuse larger payloads (default: {SPL_SIZE_LIMIT:#x})")
    p.set_defaults(func=cmd_sign_fsbl)

    p = sub.add_parser("bootinfo", help="write the 80-byte SD bootinfo")
    p.add_argument("output", type=Path)
    p.add_argument("--spl0", type=int0, default=SPL0_OFFSET)
    p.add_argument("--spl1", type=int0, default=SPL1_OFFSET)
    p.add_argument("--limit", type=int0, default=SPL_SIZE_LIMIT)
    p.set_defaults(func=cmd_bootinfo)

    p = sub.add_parser("assemble", help="build the raw SD boot blob")
    p.add_argument("output", type=Path)
    p.add_argument("--fsbl", type=Path, required=True)
    p.add_argument("--itb", type=Path, required=True)
    p.add_argument("--spl0", type=int0, default=SPL0_OFFSET)
    p.add_argument("--spl1", type=int0, default=SPL1_OFFSET, help="0 to omit the backup FSBL")
    p.add_argument("--itb-offset", type=int0, default=ITB_OFFSET)
    p.add_argument("--max-end", type=int0, default=0, help="fail if the blob ends beyond this byte")
    p.set_defaults(func=cmd_assemble)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
