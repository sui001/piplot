"""Plot the same motif at a range of speeds, and time each one.

Guessing a speed from a spec sheet looks fine until it is two hours into a
real drawing. This draws one sheet you can pin to the wall: the same test
motif repeated across it, each cell at a different pen-down speed, each
labelled with its number, and a table of how long each actually took.

The motif is chosen to fail in the three ways speed makes things fail:

  square      sharp corners, so overshoot and rounding show at the corners
  rosette     smooth curves with cusps, where resonance shows as wobble
  hatch       closely spaced parallel lines, where ink starvation shows as
              lines going thin or grey, and where neighbouring lines act as a
              ruler for each other the way a swept family does

    python speed_ladder.py --port axidraw-0 --dry
    python speed_ladder.py --port axidraw-0 --speeds 25,40,55,70,85,100

Park the carriage in the home corner first. The machine has no home switches
and believes it starts at (0, 0).
"""

from __future__ import annotations

import argparse
import math
import sys
import time

from pen_box import MODELS, Claims
from portlock import hold

VERSION = "0.1.0"

# Seven segment digits, drawn in a 0..1 box. Each entry is a list of strokes.
SEG = {
    "a": [(0, 1), (1, 1)], "b": [(1, 1), (1, 0.5)], "c": [(1, 0.5), (1, 0)],
    "d": [(0, 0), (1, 0)], "e": [(0, 0.5), (0, 0)], "f": [(0, 1), (0, 0.5)],
    "g": [(0, 0.5), (1, 0.5)],
}
DIGITS = {
    "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
    "5": "afgcd", "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abfgcd",
    "%": "af",   # a lazy percent mark, two ticks, enough to read in context
}


def label(text, x, y, h):
    """Strokes for a short number, top-left at (x, y), height h.

    The segment table is written in normal maths coordinates with y upward,
    but the plotter draws with y increasing DOWN the page. Without flipping,
    the top bar lands at the bottom and the digits come out mirrored: 2 reads
    as 5, 6 reads as 9, which is worse than no label at all on a test sheet.
    """
    w, gap, out = h * 0.55, h * 0.22, []
    for ch in text:
        for s in DIGITS.get(ch, ""):
            (ax, ay), (bx, by) = SEG[s]
            out.append([(x + ax * w, y + (1 - ay) * h),
                        (x + bx * w, y + (1 - by) * h)])
        x += w + gap
    return out


def motif(cx, cy, size):
    """The test pattern, centred on (cx, cy) and fitting in a size box."""
    r = size / 2
    paths = []

    # Square: corners are where overshoot and rounding show up.
    s = r * 0.94
    paths.append([(cx - s, cy - s), (cx + s, cy - s), (cx + s, cy + s),
                  (cx - s, cy + s), (cx - s, cy - s)])

    # Rosette: smooth curve with cusps, where the gantry ringing shows.
    rose = []
    for i in range(721):
        t = i / 720 * 2 * math.pi
        rr = r * 0.72 * abs(math.cos(2.5 * t))
        rose.append((cx + rr * math.cos(t), cy + rr * math.sin(t)))
    paths.append(rose)

    # Hatch: closely spaced lines. Ink starvation shows as lines going thin,
    # and each line is a ruler for the one beside it.
    n, span = 22, r * 0.8
    for i in range(n):
        y = cy - span / 2 + i * span / (n - 1)
        row = [(cx - r * 0.45, y), (cx + r * 0.45, y)]
        paths.append(row if i % 2 == 0 else row[::-1])
    return paths


def mm_pair(text):
    a, b = text.lower().split("x")
    return (float(a), float(b))


def main() -> int:
    p = argparse.ArgumentParser(description="plot one motif at several speeds and time each")
    p.add_argument("--speeds", default="25,40,55,70,85,100",
                   help="comma separated pen-down speed percentages")
    p.add_argument("--accel", type=int, default=75,
                   help="fixed acceleration, when laddering speed")
    p.add_argument("--accels", default=None,
                   help="ladder ACCELERATION instead of speed, comma separated")
    p.add_argument("--speed", type=int, default=55,
                   help="fixed pen-down speed, when laddering acceleration")
    p.add_argument("--model", type=int, default=2, choices=sorted(MODELS))
    p.add_argument("--paper", type=mm_pair, default=(420.0, 297.0))
    p.add_argument("--margin", type=float, default=18.0)
    p.add_argument("--cols", type=int, default=3)
    p.add_argument("--port", default=None, help="board nickname or device path")
    p.add_argument("--pen-down", type=int, default=0)
    p.add_argument("--pen-up", type=int, default=60)
    p.add_argument("--dry", action="store_true", help="check and report, move nothing")
    args = p.parse_args()

    # Acceleration is usually the control that changes the clock, since short
    # segments never reach the commanded speed, so the same rig ladders either.
    if args.accels:
        vals = [int(v) for v in args.accels.split(",") if v.strip()]
        cells_spec = [(v, args.speed, v) for v in vals]
        varying = "accel"
    else:
        vals = [int(v) for v in args.speeds.split(",") if v.strip()]
        cells_spec = [(v, v, args.accel) for v in vals]
        varying = "speed"
    speeds = vals

    print(f"=== piplot speed ladder v{VERSION} ===")
    print("Plots one motif at several speeds and times each, so the choice is measured")
    print("https://github.com/sui001/piplot")
    print()

    tx, ty, name = MODELS[args.model]
    pw, ph = args.paper
    cols = min(args.cols, len(speeds))
    rows = math.ceil(len(speeds) / cols)
    cw = (pw - 2 * args.margin) / cols
    ch = (ph - 2 * args.margin) / rows
    lab_h = min(7.0, ch * 0.12)
    size = min(cw, ch - lab_h * 2.2) * 0.86

    print(f"machine  model {args.model}, {name}, travel {tx:.0f} x {ty:.0f} mm")
    print(f"sheet    {pw:.0f} x {ph:.0f} mm, {cols} x {rows} cells of "
          f"{cw:.0f} x {ch:.0f} mm, motif {size:.0f} mm")
    if varying == "accel":
        print(f"laddering ACCELERATION: {', '.join(str(v) for v in vals)}"
              f"   at fixed speed {args.speed}%")
    else:
        print(f"laddering SPEED: {', '.join(str(v) + '%' for v in vals)}"
              f"   at fixed accel {args.accel}")
    print()

    cells = []
    for i, (shown, sp, ac) in enumerate(cells_spec):
        cx = args.margin + (i % cols) * cw + cw / 2
        cy = args.margin + (i // cols) * ch + ch / 2 + lab_h * 0.6
        paths = motif(cx, cy, size)
        paths += label(f"{shown}", cx - size * 0.2,
                       cy - size / 2 - lab_h * 1.5, lab_h)
        cells.append((shown, sp, ac, paths))

    c = Claims()
    allpts = [pt for *_, ps in cells for path in ps for pt in path]
    xs = [q[0] for q in allpts]
    ys = [q[1] for q in allpts]
    c.require("cells fit the travel envelope in x", min(xs) >= 0 and max(xs) <= tx,
              f"x spans {min(xs):.0f} to {max(xs):.0f} mm, machine has 0 to {tx:.0f}")
    c.require("cells fit the travel envelope in y", min(ys) >= 0 and max(ys) <= ty,
              f"y spans {min(ys):.0f} to {max(ys):.0f} mm, machine has 0 to {ty:.0f}")
    c.require("every value is a usable percentage",
              all(1 <= v <= 100 for v in vals))
    c.require("the motif is big enough to read", size >= 20.0, f"{size:.0f} mm")
    print()

    if not c.ok:
        print("REFUSING TO MOVE.", file=sys.stderr)
        return 1

    total = sum(len(path) for *_, ps in cells for path in ps)
    print(f"{len(cells)} cells, {total} points total")
    if args.dry:
        for shown, sp, ac, ps in cells:
            n = sum(len(x) for x in ps)
            print(f"  {shown:3}  speed {sp:3}% accel {ac:3}  "
                  f"{len(ps):3} paths  {n:5} points")
        print("\ndry run, nothing moved")
        return 0

    from pyaxidraw import axidraw

    ad = axidraw.AxiDraw()
    ad.interactive()
    o = ad.options
    o.units = 2
    o.model = args.model
    o.pen_pos_down = args.pen_down
    o.pen_pos_up = args.pen_up
    o.speed_penup = 75
    if args.port:
        o.port = args.port
        o.port_config = 0      # 1 would mean "first AxiDraw found", not this one
    device = args.port or ""
    lock = hold(device)
    if not lock.__enter__():
        print(f"{device} is in use. The piplot server or another tool has it.",
              file=sys.stderr)
        return 1

    if not ad.connect():
        lock.__exit__(None, None, None)
        print(f"could not connect to {args.port or 'any AxiDraw'}", file=sys.stderr)
        return 1

    results = []
    try:
        for shown, sp, ac, paths in cells:
            o.speed_pendown = sp
            o.accel = ac
            ad.update()          # options only take effect after update
            mm = sum(math.dist(path[i - 1], path[i])
                     for path in paths for i in range(1, len(path)))
            print(f"  {shown:3}  (speed {sp}%, accel {ac})  {mm:.0f} mm ...",
                  end="", flush=True)
            t0 = time.time()
            for path in paths:
                ad.draw_path([[float(x), float(y)] for x, y in path])
            dt = time.time() - t0
            print(f" {dt:5.1f}s   {mm / dt:5.1f} mm/s actual")
            results.append((shown, mm, dt))
        ad.penup()
        ad.moveto(0, 0)
    finally:
        ad.disconnect()
        lock.__exit__(None, None, None)

    print()
    print(f"  {varying:6}  line mm   seconds   actual mm/s   vs first")
    print("  " + "-" * 50)
    base = results[0][2] if results else 1
    for shown, mm, dt in results:
        print(f"  {shown:5}   {mm:8.0f}  {dt:8.1f}  {mm / dt:11.1f}   "
              f"{base / dt:5.2f}x")
    print()
    print("Now look at the sheet, not the table. The fastest cell that still")
    print("has clean corners, no wobble on the rosette and solid hatch lines")
    print("is your speed for that pen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
