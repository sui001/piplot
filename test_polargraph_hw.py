"""Hardware claims for the polargraph driver. Needs the real board attached.

    python test_polargraph_hw.py COM48          # Windows
    ~/venv/bin/python test_polargraph_hw.py /dev/ttyUSB0   # the Pi

Moves the motors a few millimetres around the park point. Safe with no
gondola hung; with one hung it draws a 20 mm square at the centre of the
board, so have a pen up or no pen in.

Two claims that only hardware can settle:

1. OPENING THE PORT DOES NOT RESET THE BOARD. On this machine a reset
   re-derives the zero from wherever the gondola hangs, silently. So: move
   off the park point, drop the connection WITHOUT going home, reopen, and
   require that FluidNC still reports the offset. A reset would read 0,0.
   This is OS dependent (Linux may pulse DTR during open where Windows does
   not), which is why it is run on the target, not assumed from a laptop.

2. THE DRIVER DRAWS AND COMES HOME. A 20 mm square through draw_path, then
   disconnect, which returns to the park point, and require MPos 0,0.

Exits 1 on any failure.
"""

from __future__ import annotations

import re
import sys
import time

import plotter
import polargraph

FAILS: list[str] = []


def require(ok: bool, claim: str) -> None:
    print(f"  {'pass' if ok else 'FAIL'}  {claim}")
    if not ok:
        FAILS.append(claim)


def mpos(g) -> tuple[float, float]:
    g.sp.reset_input_buffer()
    g.sp.write(b"?")
    time.sleep(0.4)
    reply = g.sp.read(g.sp.in_waiting or 1).decode(errors="replace")
    m = re.search(r"MPos:([-\d.]+),([-\d.]+)", reply)
    if not m:
        raise RuntimeError(f"no position in status reply: {reply!r}")
    return float(m.group(1)), float(m.group(2))


def settle(g, timeout=30.0) -> tuple[float, float]:
    """Position once it stops changing. Not _wait_idle, on purpose.

    FluidNC can report Idle for a moment after accepting a move, while it is
    still segmenting it, so a status poll straight after sending a G1 can
    say Idle before anything has moved. The first run of this test sampled
    "before" mid-move at (1.6, 0.9) on its way to (12, 7) for exactly that
    reason. Two identical readings half a second apart is the real test.
    """
    end = time.time() + timeout
    last = mpos(g)
    while time.time() < end:
        time.sleep(0.5)
        now = mpos(g)
        if abs(now[0] - last[0]) < 0.001 and abs(now[1] - last[1]) < 0.001:
            return now
        last = now
    raise TimeoutError(f"position never settled, last {last}")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    port = sys.argv[1]
    geom = polargraph.whiteboard()

    print(f"\n1. opening {port} does not reset the board")
    g = plotter.Polargraph(geom, serial_port=port)
    g.connect()
    require(True, "connected, identified as FluidNC, no boot banner on open")
    start = settle(g)
    g._send("G91", 0.2)
    g._send("G1 X12 Y7 F600", 0.2)
    g._send("G90", 0.2)
    time.sleep(0.5)
    before = settle(g)
    require(abs(before[0] - start[0] - 12) < 0.05 and abs(before[1] - start[1] - 7) < 0.05,
            f"the move arrived: {start} -> {before}, which is +12, +7")
    print(f"      moved off park to {before}")
    g.sp.close()                       # deliberately NOT disconnect(): no trip home
    g.sp = None
    time.sleep(1.0)

    g2 = plotter.Polargraph(geom, serial_port=port)
    try:
        g2.connect()
    except RuntimeError as exc:
        require(False, f"reopen did not reset the board ({exc})")
        return 1
    after = settle(g2)
    require(abs(after[0] - before[0]) < 0.05 and abs(after[1] - before[1]) < 0.05,
            f"after reopening, FluidNC still reports {after}, not 0,0, so no "
            f"reset happened on open")

    print("\n1b. a redundant pen lift off the park point does not move the gondola")
    # The FluidNC bug: a move with zero distance sends the cartesian target
    # to the motors as cord offsets. At (12, 7) a redundant "G0 Z5" threw the
    # gondola to (-26.942, 2.926). penup() twice in a row is exactly that
    # redundant lift, so the guard has to drop it.
    g2.penup()
    g2.penup()
    time.sleep(0.5)
    still = settle(g2)
    require(abs(still[0] - after[0]) < 0.05 and abs(still[1] - after[1]) < 0.05,
            f"two pen lifts in a row at {after} left it at {still}; the move "
            f"to nowhere was dropped instead of sent")

    print("\n2. the driver draws a square and comes home")
    hx, hy = g2.home
    square = [(hx - 10, hy - 10), (hx + 10, hy - 10), (hx + 10, hy + 10),
              (hx - 10, hy + 10), (hx - 10, hy - 10)]
    require(g2.check(square) == [], "the square passes the geometry's own check")
    finished = g2.draw_path(square)
    require(finished, "draw_path completed without being stopped")
    g2.disconnect()                    # lifts and returns to the park point
    time.sleep(0.5)

    g3 = plotter.Polargraph(geom, serial_port=port)
    g3.connect()
    home = settle(g3)
    require(abs(home[0]) < 0.05 and abs(home[1]) < 0.05,
            f"after disconnect the machine is back on its park point {home}")
    g3.sp.close()

    print()
    if FAILS:
        print(f"{len(FAILS)} claim(s) violated")
        return 1
    print("all hardware claims hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
