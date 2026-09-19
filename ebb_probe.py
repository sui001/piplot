"""Ask an EiBotBoard what it is, and make its pen servo move where you can see it.

The GRBL machine has grbl_probe.py; this is the same idea for the AxiDraws. An
EBB answers OK to a pen command whether or not a servo is attached, plugged in
the right way round, or on the right header pin, so a passing test proves
nothing. This prints its schedule first and then runs it on the clock, so
whoever is standing at the machine can match what the pen did to what was sent.

    python ebb_probe.py --port axidraw-1              # identify only
    python ebb_probe.py --port axidraw-1 --servo      # and move the pen

Two tests, and the difference between them is the diagnosis:

  SP   the normal pen command, which obeys SC,1 (the configured lift
       mechanism: 0 solenoid, 1 RC servo, 2 both)
  S2   drives the servo pin directly and ignores all of that

If S2 moves the pen and SP does not, the board is set to the wrong lift
mechanism. If neither moves it, the problem is the servo, its cable, or which
header pin it is on. The pen servo belongs on RB1.
"""

from __future__ import annotations

import argparse
import sys
import time

from portlock import hold

VERSION = "0.1.0"

CR = chr(13)

# Servo positions in the EBB's own units of 83.3 ns of pulse width. The stock
# AxiDraw range is roughly 7500 to 28000; these are deliberately wide so a
# working servo swings visibly rather than twitching.
WIDE_UP = 10000
WIDE_DOWN = 24000

PEN_PIN = 1          # RB1, where the AxiDraw's pen servo lives


def main() -> int:
    p = argparse.ArgumentParser(description="identify an EiBotBoard and test its pen servo")
    p.add_argument("--port", default=None, help="board nickname or device path")
    p.add_argument("--servo", action="store_true", help="also move the pen")
    p.add_argument("--gap", type=float, default=2.5, help="seconds between commands")
    p.add_argument("--lead", type=float, default=6.0,
                   help="seconds to get your eyes on the pen before anything moves")
    p.add_argument("--swings", type=int, default=6, help="alternations per test")
    args = p.parse_args()

    import serial
    from portlock import canonical

    device = canonical(args.port) if args.port else None
    if not device or not device.startswith("/"):
        print(f"could not resolve {args.port!r} to a device", file=sys.stderr)
        return 1

    print(f"=== piplot ebb probe v{VERSION} ===")
    print(f"{args.port} -> {device}")
    print("Nothing here commands X or Y. The pen is the only thing that moves.")
    print()

    with hold(device, wait=3) as got:
        if not got:
            print(f"{args.port} is in use by something else", file=sys.stderr)
            return 1
        sp = serial.Serial(device, 9600, timeout=2)
        try:
            time.sleep(0.3)
            sp.write(CR.encode())      # finish any half command left by a replug
            time.sleep(0.2)
            sp.reset_input_buffer()

            def ask(cmd, wait=0.4):
                sp.reset_input_buffer()
                sp.write((cmd + CR).encode())
                time.sleep(wait)
                raw = sp.read(300).decode(errors="replace").strip()
                return raw.replace(CR + chr(10), " ")

            print("firmware   ", ask("V"))
            qc = ask("QC")
            try:
                volts = int(qc.split(",")[1].split()[0]) * 0.0295
                rail = f"{volts:.1f} V"
            except Exception:
                rail = "unreadable"
            print("motor rail ", f"{qc}   ({rail})")
            print("pen state  ", ask("QP"), " (1 is up, as the board believes)")

            if not args.servo:
                print("\nno --servo, nothing moved")
                return 0

            print()
            print("widening the travel so any movement at all is obvious")
            for cmd in (f"SC,1,1", f"SC,4,{WIDE_UP}", f"SC,5,{WIDE_DOWN}"):
                print(f"  {cmd:16} -> {ask(cmd)}")

            for title, build in (
                ("TEST 1: SP, the normal pen command",
                 lambda n: ("SP,0", "DOWN") if n % 2 == 0 else ("SP,1", "UP")),
                ("TEST 2: S2, driving the servo pin directly",
                 lambda n: ((f"S2,{WIDE_DOWN},{PEN_PIN},0,0", "DOWN") if n % 2 == 0
                            else (f"S2,{WIDE_UP},{PEN_PIN},0,0", "UP"))),
            ):
                print()
                print(f"{title}. {args.lead:.0f} s from now, then "
                      f"{args.gap:.1f} s apart.")
                sys.stdout.flush()
                time.sleep(args.lead)
                for n in range(args.swings):
                    cmd, what = build(n)
                    reply = ask(cmd, 0.2)
                    print(f"  {n + 1}/{args.swings}  {what:4}  {cmd:22} -> {reply}")
                    sys.stdout.flush()
                    time.sleep(max(0.0, args.gap - 0.2))

            ask("SP,1")
            print()
            print("done, pen commanded up")
        finally:
            sp.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
