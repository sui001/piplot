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

# The two boards this has been used on. They differ in chip family, where
# the bootloader goes, which partition table, and how big the app slot is.
#
# wroom: the working path. FluidNC's classic build fits a 4 MB ESP32 as
#        shipped, so its own partition table is used untouched.
# s3:    PARKED 21 Sep. Needs our 4 MB table (make_partitions.py), flashes
#        and verifies, then FluidNC takes over the S3's USB at boot and dies
#        before its console or WiFi appear. Cause unknown without a TX log.
BOARDS = {
    "wroom": {"chip": "esp32", "build": "wifi", "boot": "0x1000",
              "table": None, "app0": 0x1E0000},
    "s3":    {"chip": "esp32s3", "build": "wifi_s3", "boot": "0x0000",
              "table": TABLE, "app0": 0x300000},
}


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
    b = BOARDS[a.board]
    files = {
        b["boot"]: os.path.join(rel, b["build"], "bootloader.bin"),
        "0x8000": b["table"] or os.path.join(rel, b["build"], "partitions.bin"),
        "0xe000": os.path.join(rel, "common", "boot_app0.bin"),
        "0x10000": os.path.join(rel, b["build"], "firmware.bin"),
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
    if fw > b["app0"]:
        print(f"firmware is {fw} bytes, over the {b['app0']} byte app slot")
        return 1

    base = [esptool(rel), "--chip", b["chip"], "--port", a.port]

    # Every call but the last uses --after no-reset, so the chip stays in its
    # loader. The S3's USB port belongs to whatever is running: reset it into
    # an erased chip and the ROM reboots faster than Windows can enumerate,
    # so the port simply vanishes; reset it into FluidNC and FluidNC swaps in
    # its own USB serial on a new COM number. Either way the next step is
    # talking to a port that no longer exists. The first version of this
    # script did exactly that on 21 Sep: it reset between steps, the chip
    # ended up fully erased, and nothing had been written.
    print("\n1. what flash does this chip actually have")
    out = run(base + ["--after", "no-reset", "flash-id"], a.dry_run)
    if not a.dry_run:
        m = re.search(r"Detected flash size:\s*(\S+)", out)
        size = m.group(1) if m else "unknown"
        print(f"  detected: {size}")
        if size != "4MB":
            print(f"  refusing: this table is for 4 MB chips and this one says "
                  f"{size}. On 8 MB or more, FluidNC's own install-wifi_s3.bat "
                  "is the right tool and keeps over-the-air updates.")
            return 1

    # Erase and write in ONE esptool call (--erase-all), so there is no
    # moment where the chip sits erased with a reset pending. See above.
    print("\n2. erase and write in one pass: bootloader, 4 MB table, "
          "boot_app0, firmware")
    write = base + ["--baud", "921600", "--after", "hard-reset",
                    "write-flash", "-z", "--flash-mode", "dio",
                    "--flash-freq", "80m", "--flash-size", "4MB"]
    if not a.no_erase:
        write.append("--erase-all")
    for off, path in files.items():
        write += [off, path]
    out = run(write, a.dry_run)
    if a.dry_run:
        return 0

    # esptool hashes each region after writing it. Four regions went in, so
    # four confirmations or it did not happen, whatever the exit code says.
    verified = out.count("Hash of data verified")
    print(f"  esptool verified {verified} of {len(files)} regions")
    if verified != len(files):
        print(out)
        print("NOT done: the write did not verify. Do not upload a config "
              "onto this; reflash first.")
        return 1

    if a.board == "wroom":
        # The CH340 is a separate chip, so the port survives the reboot.
        print(f"\ndone. FluidNC is booting on {a.port}. Next: "
              f"python flash_s3_4mb.py upload --port {a.port}")
        return 0

    print("\n3. waiting for FluidNC's own USB serial to appear")
    port = wait_for_new_port(timeout=25.0)
    if port is None:
        print("  no Espressif USB port appeared in 25 s after reboot. The "
              "flash verified, so FluidNC is on the chip; its USB console "
              "just has not come up. Hold BOOT and replug to get back in.")
        return 1
    print(f"  FluidNC is on {port}")
    print(f"\ndone. Next: python flash_s3_4mb.py upload --port {port}")
    return 0


def before_ports():
    from serial.tools import list_ports
    return [p.device for p in list_ports.comports() if p.vid == 0x303A]


def wait_for_new_port(timeout: float):
    """The Espressif port that exists after the reboot, or None.

    Waits a few seconds first, because the loader's port is still listed for
    a moment after the reset and would otherwise be mistaken for FluidNC's.
    """
    from serial.tools import list_ports
    time.sleep(4.0)
    end = time.time() + timeout
    while time.time() < end:
        ports = [p.device for p in list_ports.comports() if p.vid == 0x303A]
        if ports:
            time.sleep(1.5)                  # let it settle, then re-read
            again = [p.device for p in list_ports.comports()
                     if p.vid == 0x303A]
            if again:
                return again[0]
        time.sleep(0.5)
    return None


def cmd_upload(a) -> int:
    path = os.path.abspath(a.config)
    if not os.path.exists(path):
        print(f"no config at {path}")
        return 1
    data = open(path, "rb").read()
    if not a.port:
        found = before_ports() if not a.dry_run else ["COM?"]
        if len(found) != 1:
            print(f"need exactly one Espressif port to guess from, found "
                  f"{found or 'none'}; pass --port")
            return 1
        a.port = found[0]
    print(f"\nsending {os.path.basename(path)} ({len(data)} bytes) as "
          f"config.yaml to {a.port}")
    if a.dry_run:
        print("  would send: $Xmodem/Receive=config.yaml, then XMODEM, then "
              "$Bye to reboot into it")
        return 0

    import io
    import serial
    from xmodem import XMODEM

    # DTR and RTS OFF BEFORE opening. pyserial asserts DTR on open by
    # default, and on the S3's native USB that resets the chip with the boot
    # strap held low, so it lands in the ROM loader ("waiting for download")
    # instead of FluidNC. Found 21 Sep the hard way: every probe knocked
    # FluidNC off the chip it had just been verified onto.
    #
    # WINDOWS ONLY as written. On Linux pyserial applies these one at a time
    # after the kernel has raised both lines, DTR first, and the instant of
    # DTR low with RTS high resets a classic devkit (measured on polarpi,
    # 22 Sep). plotter.Polargraph.connect has the Linux-safe ordering. This
    # upload reboots the board with $Bye anyway, so a reset here costs
    # nothing, but do not copy this pattern into anything that must not reset.
    sp = serial.Serial()
    sp.port, sp.baudrate, sp.timeout = a.port, 115200, 2
    sp.dtr = False
    sp.rts = False
    sp.open()
    time.sleep(0.5)
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
    f.add_argument("--board", required=True, choices=sorted(BOARDS),
                   help="wroom is the working path; s3 is parked")
    f.add_argument("--release", required=True)
    f.add_argument("--port", required=True)
    f.add_argument("--no-erase", action="store_true")
    f.add_argument("--dry-run", action="store_true")
    u = sub.add_parser("upload")
    u.add_argument("--port", default=None,
                   help="omit to use the only Espressif USB port present")
    u.add_argument("--config", default=DEFAULT_CONFIG)
    u.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    return cmd_flash(a) if a.cmd == "flash" else cmd_upload(a)


if __name__ == "__main__":
    sys.exit(main())
