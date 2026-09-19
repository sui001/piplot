"""Ask a GRBL board what it is, back up its settings, and test its pen servo.

GRBL answers `ok` to a spindle command whether or not a servo is attached to the
spindle pin, so a passing test proves nothing at all. This prints a schedule
first, then runs it on the clock, so the person standing at the machine can
match what the pen did to what was sent without needing live output.

    python grbl_probe.py --port /dev/ttyUSB0                  # identify only
    python grbl_probe.py --port /dev/ttyUSB0 --servo          # and move the pen
    python grbl_probe.py --port /dev/ttyUSB0 --save m.grbl    # back the settings up
    python grbl_probe.py --port /dev/ttyUSB0 --restore m.grbl # put them back

Back the settings up before flashing anything. GRBL stores them in EEPROM and
wipes them to defaults whenever the settings version changes between builds, so
a firmware upgrade can quietly cost you the steps/mm, the axis inversions and
the knowledge that the machine is CoreXY.

Nothing here commands X or Y. The pen is the only thing that should move.
"""

from __future__ import annotations

import argparse
import sys
import time

VERSION = "0.1.0"

CR = "\r\n"


def drain(sp, wait=0.6):
    time.sleep(wait)
    return sp.read(sp.in_waiting or 1).decode(errors="replace").strip()


def ask(sp, cmd, wait=0.8):
    sp.reset_input_buffer()
    sp.write((cmd + CR).encode())
    return drain(sp, wait)


def identify(sp):
    print("identifying")
    for cmd, what in (("$I", "build"), ("$$", "settings"), ("?", "state")):
        reply = ask(sp, cmd, 1.5 if cmd == "$$" else 0.8)
        print(f"  {what:9} {reply}" if cmd != "$$" else f"  {what}:")
        if cmd == "$$":
            for line in reply.splitlines():
                print(f"    {line}")


# The spindle route, for a board whose pen wiring is not yet known. On the
# CoreXY machine this proves the negative: its build reports OPT without a V,
# so VARIABLE_SPINDLE is not compiled in, the spindle pin is a plain digital
# output, and S100, S500 and S1000 are all the identical instruction. No servo
# can be driven this way. Run it on a new board to find that out in one go.
SPINDLE_PASS = [
    ("M5", "spindle off"),
    ("M3 S100", "one tenth"),
    ("M3 S500", "half"),
    ("M3 S1000", "full"),
    ("M5", "off again"),
]


def run_pass(sp, steps, gap, label):
    print(f"\n--- {label}, {gap:.0f} s between each ---")
    for n, (cmd, what) in enumerate(steps, 1):
        print(f"  t+{(n - 1) * gap:>3.0f}s  {cmd:<10} {what}")
    print("  running now")
    sys.stdout.flush()
    replies = []
    for cmd, _ in steps:
        # Do not clear the buffer between commands. An earlier version did, and
        # it threw away every reply but the last, so an `error:` on one of these
        # would have gone unseen while the run still looked clean.
        sp.write((cmd + CR).encode())
        time.sleep(gap)
        replies.append(f"{cmd} -> {drain(sp, 0.0) or 'no reply'}")
    for r in replies:
        print(f"  {r}")


def read_settings(sp):
    """Every `$n=v` line the board reports, in the order it reports them."""
    reply = ask(sp, "$$", 2.0)
    return [ln.strip() for ln in reply.splitlines()
            if ln.strip().startswith("$") and "=" in ln]


def save_settings(sp, path, build):
    lines = read_settings(sp)
    if not lines:
        print("no settings came back, nothing saved", file=sys.stderr)
        return 1
    with open(path, "w") as fh:
        fh.write(f"; grbl settings read {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fh.write(f"; {build}\n")
        fh.write("; replay with: grbl_probe.py --restore this-file\n")
        fh.write("\n".join(lines) + "\n")
    print(f"saved {len(lines)} settings to {path}")
    return 0


def restore_settings(sp, path):
    with open(path) as fh:
        lines = [ln.strip() for ln in fh
                 if ln.strip() and not ln.strip().startswith(";")]
    print(f"restoring {len(lines)} settings from {path}")
    bad = 0
    for ln in lines:
        reply = ask(sp, ln, 0.3)
        if "ok" not in reply.lower():
            print(f"  {ln:<16} {reply or 'no reply'}")
            bad += 1
    # A setting a new build no longer has is normal on an upgrade, not a
    # failure, so say how many and let the reader judge.
    print(f"  {len(lines) - bad} accepted, {bad} refused")
    return 0


def pen_pass(sp, heights, gap):
    """Lift and lower once per height, so each lift is judged on its own.

    An ascending sweep that never returns to the bottom is unreadable: the pen
    creeps and the eye cannot tell one step from the next. Dropping back
    between each makes every height a separate event the watcher can count.
    """
    print(f"\n--- {len(heights)} lifts, {gap:.0f} s up and {gap:.0f} s down each ---")
    print("  count them and note which ones differ in height")
    sys.stdout.flush()
    ask(sp, "G90", 0.4)
    ask(sp, "G0 Z0", gap)
    for n, z in enumerate(heights, 1):
        print(f"  lift {n} of {len(heights)}   Z{z}")
        sys.stdout.flush()
        ask(sp, f"G0 Z{z}", gap)
        ask(sp, "G0 Z0", gap)


def main() -> int:
    p = argparse.ArgumentParser(description="identify a GRBL board, back it up, test its pen")
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--pen", action="store_true",
                   help="lift and lower the pen on the Z axis, where it actually lives")
    p.add_argument("--heights", default="1,2,3,5,8",
                   help="Z heights to try with --pen, comma separated")
    p.add_argument("--spindle", action="store_true",
                   help="try the M3/M5 spindle route instead, for an unknown board")
    p.add_argument("--save", metavar="FILE", help="write the board's settings to a file")
    p.add_argument("--restore", metavar="FILE", help="send a saved settings file back")
    p.add_argument("--gap", type=float, default=4.0, help="seconds between commands")
    p.add_argument("--lead", type=float, default=6.0,
                   help="seconds to get your eyes on the pen before anything moves")
    args = p.parse_args()

    import serial

    print(f"=== piplot grbl probe v{VERSION} ===")
    print(f"port {args.port} at {args.baud}")
    print("Nothing here commands X or Y. The pen is the only thing that should move.")
    print()

    sp = serial.Serial(args.port, args.baud, timeout=2)
    try:
        time.sleep(2.0)                 # the CH340 DTR reset, and GRBL's banner
        banner = drain(sp, 0.5)
        if banner:
            print("banner:", banner.replace("\r\n", " "))
        sp.write(CR.encode())
        drain(sp, 0.3)

        build = ask(sp, "$I", 0.8).splitlines()
        build = " ".join(b.strip() for b in build if b.strip().startswith("["))
        print("build:", build)

        if args.restore:
            return restore_settings(sp, args.restore)
        if args.save:
            return save_settings(sp, args.save, build)

        identify(sp)

        if not (args.pen or args.spindle):
            print("\nno --pen or --spindle, nothing moved")
            return 0

        print(f"\nPEN TEST. Watch the pen. {args.lead:.0f} s from now.")
        sys.stdout.flush()
        time.sleep(args.lead)

        if args.spindle:
            run_pass(sp, SPINDLE_PASS, args.gap, "spindle route, M3 S / M5")
            ask(sp, "M5")
        if args.pen:
            pen_pass(sp, [int(h) for h in args.heights.split(",")], args.gap)

        print("\npen test over, pen left down")
    finally:
        sp.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
