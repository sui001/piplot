"""Put FluidNC and the polargraph config onto a 4 MB ESP32-S3 SuperMini.

    python flash_s3_4mb.py flash  --release DIR --port COM46
    python flash_s3_4mb.py upload --port COM46 [--config ../fluidnc-polargraph-bench.yaml]

Add `--dry-run` to either to print what would happen and touch nothing.

`DIR` is an unpacked FluidNC release (fluidnc-v4.1.0-win64), which supplies
esptool.exe, the bootloader, boot_app0.bin and the wifi_s3 firmware. The
only thing this repo contributes is the partition table, built and claimed
by make_partitions.py. See that file for why the stock one cannot work on a
4 MB chip.

flash
-----
1. Reads the chip's flash size and REFUSES unless it is 4 MB. The whole
   point of the custom table is a 4 MB chip; on 8 MB or more, use FluidNC's
   own install-wifi_s3.bat instead, which keeps over-the-air updates.
2. Erases the whole chip, unless --no-erase. Whatever was running before
   leaves NVS settings and filesystem remnants that FluidNC would then try
   to make sense of; starting blank means the first boot formats a clean
   filesystem, which is what make_partitions.py's design relies on.
3. Writes bootloader, partition table, boot_app0 and firmware, at the same
   offsets as FluidNC's install script except for the table, and with the
   flash size stated as 4MB rather than detected.

upload
------
Sends the config as `config.yaml`, the name FluidNC loads at boot, using
FluidNC's own `$Xmodem/Receive=` command and the same `xmodem` package
FluidTerm uses. FluidTerm's Ctrl+U would do the transfer too, but it keeps
the local filename, and FluidNC would then boot with no config and say so.

Opening the port RESETS the S3 (native USB, DTR). That is fine here: there
is nothing to lose on a board with no machine attached yet.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(HERE, "partitions-s3-4mb.bin")
DEFAULT_CONFIG = os.path.join(HERE, "..", "fluidnc-polargraph-bench.yaml")
APP0_BYTES = 0x300000


def esptool(release: str) -> str:
    return os.path.join(release, "win64", "esptool.exe")


def run(cmd, dry: bool) -> str:
    print("  $ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    if dry:
        return ""
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = res.stdout + res.stderr
    if res.returncode != 0:
        print(out)
        raise SystemExit(f"that step failed (exit {res.returncode}); stopping "
                         "before anything else is written")
    return out


def cmd_flash(a) -> int:
    rel = a.release
    files = {
        "0x0000": os.path.join(rel, "wifi_s3", "bootloader.bin"),
        "0x8000": TABLE,
        "0xe000": os.path.join(rel, "common", "boot_app0.bin"),
        "0x10000": os.path.join(rel, "wifi_s3", "firmware.bin"),
    }
    missing = [p for p in [esptool(rel), *files.values()] if not os.path.exists(p)]
    if missing:
        print("missing, so nothing was touched:")
        for p in missing:
            print("  " + p)
        if TABLE in missing:
            print("run make_partitions.py first; it only writes the table "
                  "when every claim holds")
        return 1
    fw = os.path.getsize(files["0x10000"])
    if fw > APP0_BYTES:
        print(f"firmware is {fw} bytes, over the {APP0_BYTES} byte app slot")
        return 1

    base = [esptool(rel), "--chip", "esp32s3", "--port", a.port]

    print("\n1. what flash does this chip actually have")
    out = run(base + ["flash-id"], a.dry_run)
    if not a.dry_run:
        m = re.search(r"Detected flash size:\s*(\S+)", out)
        size = m.group(1) if m else "unknown"
        print(f"  detected: {size}")
        if size != "4MB":
            print(f"  refusing: this table is for 4 MB chips and this one says "
                  f"{size}. On 8 MB or more, FluidNC's own install-wifi_s3.bat "
                  "is the right tool and keeps over-the-air updates.")
            return 1

    if not a.no_erase:
        print("\n2. erase, so the first boot formats a clean filesystem")
        run(base + ["erase-flash"], a.dry_run)
    else:
        print("\n2. erase skipped (--no-erase)")

    print("\n3. write bootloader, 4 MB table, boot_app0, firmware")
    write = base + ["--baud", "921600", "--before", "default-reset",
                    "--after", "hard-reset", "write-flash", "-z",
                    "--flash-mode", "dio", "--flash-freq", "80m",
                    "--flash-size", "4MB"]
    for off, path in files.items():
        write += [off, path]
    run(write, a.dry_run)

    print("\ndone. Next: python flash_s3_4mb.py upload --port " + a.port)
    return 0


def cmd_upload(a) -> int:
    path = os.path.abspath(a.config)
    if not os.path.exists(path):
        print(f"no config at {path}")
        return 1
    data = open(path, "rb").read()
    print(f"\nsending {os.path.basename(path)} ({len(data)} bytes) as "
          f"config.yaml to {a.port}")
    if a.dry_run:
        print("  would send: $Xmodem/Receive=config.yaml, then XMODEM, then "
              "$Bye to reboot into it")
        return 0

    import io
    import serial
    from xmodem import XMODEM

    sp = serial.Serial(a.port, 115200, timeout=2)
    time.sleep(2.5)                     # native USB reset on open; let it boot
    sp.reset_input_buffer()
    sp.write(b"\n")
    time.sleep(0.3)
    sp.reset_input_buffer()

    sp.write(b"$Xmodem/Receive=config.yaml\n")
    time.sleep(1.2)                     # FluidNC waits 1 s before listening

    def getc(size, timeout=1):
        sp.timeout = timeout
        return sp.read(size) or None

    def putc(buf, timeout=1):
        return sp.write(buf)

    ok = XMODEM(getc, putc, mode="xmodem").send(io.BytesIO(data))
    time.sleep(0.5)
    tail = sp.read(sp.in_waiting or 1).decode(errors="replace")
    print(("  transfer ok" if ok else "  TRANSFER FAILED") +
          (f"\n  board said: {tail.strip()}" if tail.strip() else ""))
    if not ok:
        sp.close()
        return 1

    print("\nrebooting into the new config")
    sp.write(b"$Bye\n")
    time.sleep(4.0)
    boot = sp.read(sp.in_waiting or 1).decode(errors="replace")
    sp.close()
    for line in boot.splitlines():
        if any(k in line for k in ("Kinematic", "Configuration", "error",
                                   "Error", "VER:", "Axis", "Local filesystem")):
            print("  " + line.strip())
    if "WallPlotter" not in boot:
        print("\n  did NOT see 'Kinematic system: WallPlotter' in the boot "
              "log. Open fluidterm and look at the whole thing before moving "
              "anything.")
        return 1
    print("\n  WallPlotter kinematics loaded.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("flash")
    f.add_argument("--release", required=True)
    f.add_argument("--port", required=True)
    f.add_argument("--no-erase", action="store_true")
    f.add_argument("--dry-run", action="store_true")
    u = sub.add_parser("upload")
    u.add_argument("--port", required=True)
    u.add_argument("--config", default=DEFAULT_CONFIG)
    u.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    return cmd_flash(a) if a.cmd == "flash" else cmd_upload(a)


if __name__ == "__main__":
    sys.exit(main())
