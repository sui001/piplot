"""A 4 MB partition table for FluidNC on the ESP32-S3 SuperMini.

Run it: `python make_partitions.py [path/to/stock/wifi_s3/partitions.bin]`.
Writes `partitions-s3-4mb.bin`, `.csv` and `claims.json` beside this file,
and exits 1 if any claim fails, in which case the .bin is NOT written.

Why this exists
---------------
FluidNC's S3 builds ship one partition table, `app3M_spiffs1M_8MB.csv`, which
assumes 8 MB of flash: two 3 MB app slots for over-the-air updates, then the
filesystem at 0x610000. The SuperMini is an ESP32-S3FH4R2 with **4 MB**. The
first app slot fits, so the firmware flashes and even boots, but the
filesystem sits 6 MB into a 4 MB chip, and the filesystem is where
`config.yaml` lives. No filesystem, no config, no machine.

The fix is to keep everything the bootloader and firmware rely on exactly
where it was, drop the second app slot, and move the filesystem down:

    nvs       0x009000  0x005000   unchanged
    otadata   0x00e000  0x002000   unchanged, boot_app0.bin still selects app0
    app0      0x010000  0x300000   unchanged, firmware is 2.05 MB of it
    app1      gone                 only over-the-air updates ever used it
    spiffs    0x310000  0x0f0000   was 0x610000; 960 KB, ends exactly at 4 MB

Why moving the filesystem is safe
---------------------------------
FluidNC never hard-codes the filesystem's address. `esp32/localfs.cpp` mounts
LittleFS **by partition label**, trying "littlefs" then "spiffs", with
`format_if_mount_failed` set, and its own comment reads "Mount LittleFS,
create if necessary". So the label and subtype must match stock exactly, and
the offset is free to change. On first boot the partition is blank, the
mount fails, FluidNC formats it and carries on.

Lost by doing this: over-the-air firmware updates. Reflash over USB instead.

The encoder is proven, not trusted
----------------------------------
The strongest claim here re-encodes the STOCK table with the same code that
builds the new one, and requires it to match FluidNC's shipped
`partitions.bin` byte for byte, MD5 entry and padding included. If the
encoder were wrong in any field, that claim fails before the new table is
ever written.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FLASH = 0x400000                      # 4 MB, the ESP32-S3FH4R2
TABLE_LEN = 0xC00                     # the partition table sector ESP-IDF reads
FIRMWARE_BYTES = 2145312              # FluidNC v4.1.0 wifi_s3/firmware.bin

APP, DATA = 0x00, 0x01
SUB = {"ota_0": 0x10, "ota_1": 0x11, "ota": 0x00, "nvs": 0x02, "spiffs": 0x82}

# (label, type, subtype, offset, size)
STOCK_8MB = [
    ("nvs",     DATA, "nvs",    0x009000, 0x005000),
    ("otadata", DATA, "ota",    0x00e000, 0x002000),
    ("app0",    APP,  "ota_0",  0x010000, 0x300000),
    ("app1",    APP,  "ota_1",  0x310000, 0x300000),
    ("spiffs",  DATA, "spiffs", 0x610000, 0x1F0000),
]
S3_4MB = [
    ("nvs",     DATA, "nvs",    0x009000, 0x005000),
    ("otadata", DATA, "ota",    0x00e000, 0x002000),
    ("app0",    APP,  "ota_0",  0x010000, 0x300000),
    ("spiffs",  DATA, "spiffs", 0x310000, 0x0F0000),
]


def encode(table) -> bytes:
    """ESP-IDF's binary partition table: 32 bytes an entry, MD5, 0xFF pad."""
    out = b""
    for label, ptype, sub, off, size in table:
        out += struct.pack("<2sBBII16sI", b"\xaa\x50", ptype, SUB[sub], off,
                           size, label.encode().ljust(16, b"\0"), 0)
    out += b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(out).digest()
    return out.ljust(TABLE_LEN, b"\xff")


def decode(blob: bytes):
    """The inverse, so the written bytes can be claimed against, not the list."""
    rows, i = [], 0
    while i + 32 <= len(blob):
        e = blob[i:i + 32]
        if e[:2] == b"\xaa\x50":
            ptype, sub, off, size = e[2], e[3], *struct.unpack("<II", e[4:12])
            rows.append((e[12:28].rstrip(b"\0").decode(), ptype, sub, off, size))
        elif e[:2] == b"\xeb\xeb":
            return rows, e[16:32] == hashlib.md5(blob[:i]).digest()
        else:
            break
        i += 32
    return rows, False


class Claims:
    def __init__(self):
        self.rows = []

    def require(self, ok: bool, claim: str):
        self.rows.append({"ok": bool(ok), "claim": claim})
        print(f"  {'pass' if ok else 'FAIL'}  {claim}")

    @property
    def failed(self):
        return [r for r in self.rows if not r["ok"]]


def main() -> int:
    stock_path = sys.argv[1] if len(sys.argv) > 1 else None
    c = Claims()

    print("\nthe encoder reproduces FluidNC's own table exactly")
    if stock_path and os.path.exists(stock_path):
        stock = open(stock_path, "rb").read()
        c.require(encode(STOCK_8MB) == stock,
                  "re-encoding the stock 8 MB table gives FluidNC v4.1.0's "
                  "shipped partitions.bin byte for byte, MD5 and padding "
                  "included, so every field of the encoder is right")
    else:
        c.require(False, "the stock partitions.bin was supplied to check the "
                         "encoder against (pass its path as the argument)")

    blob = encode(S3_4MB)
    rows, md5_ok = decode(blob)
    print("\nthe new table, read back from its own bytes")
    c.require(md5_ok, "its MD5 entry verifies, or the bootloader rejects it")
    c.require(len(blob) == TABLE_LEN,
              f"it is exactly 0x{TABLE_LEN:X} bytes, the sector ESP-IDF reads")
    c.require([r[0] for r in rows] == [r[0] for r in S3_4MB],
              "it decodes to the four partitions that were encoded")

    print("\nit fits a 4 MB chip")
    for label, _, _, off, size in rows:
        c.require(off + size <= FLASH,
                  f"{label} ends at 0x{off + size:06X}, inside 0x{FLASH:06X}")
    spans = sorted((off, off + size, label) for label, _, _, off, size in rows)
    c.require(all(a[1] <= b[0] for a, b in zip(spans, spans[1:])),
              "no two partitions overlap")
    c.require(spans[0][0] >= 0x9000,
              "nothing sits on the bootloader (0x0) or the table (0x8000)")
    app = next(r for r in rows if r[0] == "app0")
    c.require(app[3] % 0x10000 == 0,
              "app0 is 64 KB aligned, which ESP-IDF requires of app partitions")

    print("\nthe firmware and FluidNC will both still find what they expect")
    c.require(app[4] >= FIRMWARE_BYTES,
              f"app0 holds the {FIRMWARE_BYTES / 2**20:.2f} MB firmware with "
              f"{(app[4] - FIRMWARE_BYTES) / 2**20:.2f} MB to spare")
    stock_same = {r[0]: r for r in decode(encode(STOCK_8MB))[0]}
    for label in ("nvs", "otadata", "app0"):
        mine = next(r for r in rows if r[0] == label)
        c.require(mine == stock_same[label],
                  f"{label} is identical to stock, so the bootloader, "
                  "boot_app0.bin and the firmware are none the wiser")
    fs = next(r for r in rows if r[0] == "spiffs")
    c.require(fs[1] == DATA and fs[2] == SUB["spiffs"],
              "the filesystem keeps label 'spiffs' and subtype 0x82, which is "
              "what localfs.cpp mounts by name, with format on failure")
    c.require(fs[3] + fs[4] == FLASH,
              f"the filesystem uses every byte left: "
              f"{fs[4] // 1024} KB, ending exactly at 4 MB")

    ok = not c.failed
    with open(os.path.join(HERE, "claims.json"), "w", encoding="utf-8") as fh:
        json.dump({"ok": ok, "claims": c.rows}, fh, indent=1)
    print()
    if not ok:
        print(f"{len(c.failed)} claim(s) violated. partitions-s3-4mb.bin NOT "
              "written.")
        return 1

    with open(os.path.join(HERE, "partitions-s3-4mb.bin"), "wb") as fh:
        fh.write(blob)
    with open(os.path.join(HERE, "partitions-s3-4mb.csv"), "w",
              encoding="utf-8") as fh:
        fh.write("# Name,   Type, SubType, Offset,  Size, Flags\n")
        fh.write("# Generated by make_partitions.py. Edit that, not this.\n")
        for label, ptype, sub, off, size in S3_4MB:
            fh.write(f"{label + ',':9s} {'app' if ptype == APP else 'data'}, "
                     f"{sub}, 0x{off:06x}, 0x{size:06x},\n")
    print("all claims hold. wrote partitions-s3-4mb.bin and .csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
