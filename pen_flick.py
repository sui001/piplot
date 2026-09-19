"""Flick a pen servo up fast, hold, drop it, hold. Repeat, on one board or several.

    python pen_flick.py --boards axidraw-1
    python pen_flick.py --boards axidraw-0,axidraw-1 --units 3000 --pause 0.5

The shape matters and took a few goes to get right, so it is worth saying why
it is this and not something cleverer.

**The pauses are the instrument.** The obvious test is to alternate up and down
faster and faster and see where it breaks. That does not work: the send rate
and the servo's own speed rise together, so when the throw collapses you
cannot tell whether the servo could not keep up or whether the next command
simply arrived before it had finished and reversed it mid-travel. Both look
identical. With a long fixed pause nothing can overlap, so a trip that fails
to arrive failed on the servo's account.

**Travel decides speed, far more than any rate setting.** `SC,4` and `SC,5`
are in units of 83.3 ns of pulse width, twelve to the microsecond, and a
standard servo maps 1000 to 2000 us across its range. So 3000 units is about
250 us, roughly 45 degrees, and it snaps. An 8000 unit swing is about 120
degrees and physically takes 200 ms or more on a hobby servo whatever you set
the slew to. Asking for that in 60 ms just saturates it, and every test above
the saturation point looks the same, which is exactly how an early version of
this managed to measure nothing at all across 25 increasing speeds.

**There is a hard ceiling underneath all of it.** An RC servo takes one pulse
every ~20 ms and only updates its target then, so a full down-and-up cannot
beat about 20 Hz however small the movement or however high the slew.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from portlock import canonical, hold

VERSION = "0.1.0"

CR = chr(13)

UNITS_PER_US = 12          # the EBB counts servo position in 83.3 ns steps


def flick(name, units, centre, pause, cycles, slew, lead, report):
    import serial

    device = canonical(name)
    if not device.startswith("/"):
        report(f"{name}: could not resolve to a device")
        return
    up, down = centre - units // 2, centre + units // 2

    with hold(device, wait=5) as got:
        if not got:
            report(f"{name}: in use by something else")
            return
        sp = serial.Serial(device, 9600, timeout=2)
        try:
            time.sleep(0.3)
            sp.write(CR.encode())          # finish any half command
            time.sleep(0.2)
            sp.reset_input_buffer()

            def send(cmd, wait=0.05):
                sp.write((cmd + CR).encode())
                time.sleep(wait)
                sp.reset_input_buffer()

            for cmd in ("SC,1,1", f"SC,4,{up}", f"SC,5,{down}",
                        f"SC,11,{slew}", f"SC,12,{slew}"):
                send(cmd, 0.2)
            send("SP,0", 0.6)              # start down, so cycle 1 is a lift

            report(f"{name}: {cycles} cycles, {units} units "
                   f"({units / UNITS_PER_US:.0f} us, about "
                   f"{units / UNITS_PER_US * 180 / 1000:.0f} degrees), "
                   f"{pause}s holds, slew {slew}")
            time.sleep(lead)

            for n in range(1, cycles + 1):
                report(f"{name}: {n:>3}/{cycles}  up")
                send("SP,1")
                time.sleep(pause)
                send("SP,0")
                time.sleep(pause)
            send("SP,1", 0.3)
            report(f"{name}: done, pen up")
        finally:
            sp.close()


def main() -> int:
    p = argparse.ArgumentParser(description="flick a pen servo up fast, hold, drop, hold")
    p.add_argument("--boards", default="axidraw-1")
    p.add_argument("--units", type=int, default=3000,
                   help="travel in EBB units, 12 per microsecond; "
                        "3000 is roughly 45 degrees")
    p.add_argument("--centre", type=int, default=15500,
                   help="middle of the swing, kept away from the servo's stops")
    p.add_argument("--pause", type=float, default=0.5,
                   help="seconds held at each end. Long enough that nothing "
                        "is ever cut short by the next command.")
    p.add_argument("--cycles", type=int, default=20)
    p.add_argument("--slew", type=int, default=65535,
                   help="EBB servo rate, 400 is stock, 65535 is flat out")
    p.add_argument("--lead", type=float, default=2.0,
                   help="seconds before it starts, to get your eyes on it")
    args = p.parse_args()

    print(f"=== piplot pen flick v{VERSION} ===")
    print("Servos get warm doing this. Minutes, not afternoons.")
    print()

    lock = threading.Lock()

    def report(line):
        with lock:
            print("  " + line)
            sys.stdout.flush()

    names = [b.strip() for b in args.boards.split(",") if b.strip()]
    threads = [threading.Thread(
        target=flick,
        args=(n, args.units, args.centre, args.pause, args.cycles,
              args.slew, args.lead, report), daemon=True) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("\nall stopped, pens up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
