"""Draw a rectangle inset from the paper edge, as a registration and reach test.

This is the first thing to run on a new machine, a new pen or a new sheet. It
tells you three things a status message cannot: that the geometry lands where
you think it does, that the pen height is right along all four edges, and that
the machine can actually reach the corners.

The AxiDraw has no home switches. It believes wherever the carriage is sitting
when you connect is (0,0), so park it in the home corner first or every number
below is a lie.

    python pen_box.py --model 2 --paper 420x297 --inset 20
    python pen_box.py --model 1 --paper 297x210 --inset 20 --dry
    python pen_box.py --driver grbl --travel 420x297 --port /dev/ttyUSB0

Origin is the home corner, x to the right, y down the page, mm throughout,
which is the same frame plan.py maps room coordinates into. The GRBL machine
runs y the other way and the backend flips it, so a correct sheet looks the
same off either machine. That is what --mark is for: it cuts a diagonal across
the top left corner, so the paper itself says whether the frame came out
mirrored rather than leaving it to be argued about.
"""

from __future__ import annotations

import argparse
import sys
import time

from portlock import hold

VERSION = "0.2.0"

# pyaxidraw model number -> (travel x mm, travel y mm, name).
# Straight off the AxiDraw model table. These are hard machine limits, not
# preferences, which is why a violation exits rather than warns.
MODELS = {
    1: (300.0, 218.0, "AxiDraw V2 / V3 / SE-A4"),
    2: (430.0, 297.0, "AxiDraw V3-A3 / SE-A3"),
    3: (595.0, 218.0, "AxiDraw V3 XLX"),
    4: (160.0, 101.0, "AxiDraw MiniKit"),
    5: (864.0, 594.0, "AxiDraw SE-A1"),
    6: (594.0, 432.0, "AxiDraw SE-A2"),
    7: (180.0, 140.0, "AxiDraw V3-B6"),
}


class Claims:
    """State what makes the run correct, and refuse to move if it is not.

    Same idea as the geometry build claims: the failure this guards against is
    a run that reports success while the carriage is grinding against a rail
    end. Nothing in the machine will tell you that happened.
    """

    def __init__(self) -> None:
        self.ok = True

    def require(self, label: str, cond: bool, detail: str = "") -> None:
        mark = "ok " if cond else "FAIL"
        print(f"  [{mark}] {label}" + (f"   {detail}" if detail else ""))
        if not cond:
            self.ok = False

    def note(self, label: str, detail: str = "") -> None:
        print(f"  [ -- ] {label}" + (f"   {detail}" if detail else ""))


def mm_pair(text: str):
    try:
        a, b = text.lower().split("x")
        return (float(a), float(b))
    except Exception:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT in mm, e.g. 420x297")


def plot_grbl(args, paths, travel) -> int:
    """Draw the box on the GRBL machine, via the Grbl backend in plotter.py.

    The preview lap matters more here than on an AxiDraw. That machine at least
    knows its own travel; this one has $20=0 and $130/$131 left at 5000, so it
    believes it is five metres wide and the only thing standing between a bad
    number and a rail end is the claim the backend makes before it moves.
    """
    from plotter import Grbl

    device = args.port or "/dev/ttyUSB0"
    g = Grbl(port=device, travel=travel, feed=args.speed * 40)

    bad = g.check([q for p in paths for q in p])
    if bad:
        print("REFUSING TO MOVE: " + "; ".join(bad), file=sys.stderr)
        return 1

    lock = hold(device)
    if not lock.__enter__():
        print(f"{device} is in use by something else", file=sys.stderr)
        return 1

    print(f"connecting to {device}")
    print("the carriage's current position becomes 0,0, so it must be parked "
          "at the bottom left corner")
    try:
        g.connect()

        if not args.no_preview:
            print("pen-up lap, watch it before anything is committed to paper")
            g.penup()
            for x, y in paths[0]:
                g.goto(x, y)
            g._wait_idle(timeout=120)
            time.sleep(1.0)

        for n, path in enumerate(paths, 1):
            print(f"drawing path {n} of {len(paths)}")
            g.draw_path(path)

        print("returning home")
    finally:
        g.disconnect()
        lock.__exit__(None, None, None)

    print("done, pen up, carriage home")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="draw an inset rectangle on the paper")
    p.add_argument("--model", type=int, default=2, choices=sorted(MODELS),
                   help="pyaxidraw model number, 1=A4 machine, 2=A3 machine")
    p.add_argument("--paper", type=mm_pair, default=(420.0, 297.0))
    p.add_argument("--inset", type=float, default=20.0, help="mm in from the edge")
    p.add_argument("--speed", type=int, default=25, help="pen-down speed percent")
    p.add_argument("--pen-up", type=int, default=60)
    p.add_argument("--pen-down", type=int, default=0)
    p.add_argument("--no-preview", action="store_true",
                   help="skip the pen-up lap and go straight to drawing")
    p.add_argument("--port", default=None,
                   help="board nickname or device path; needed with two machines")
    p.add_argument("--driver", choices=("axidraw", "grbl"), default="axidraw",
                   help="which backend; grbl is the homemade CoreXY machine")
    p.add_argument("--travel", type=mm_pair, default=None,
                   help="grbl only: the machine envelope, since GRBL does not "
                        "know its own ($130/$131 are left at 5000)")
    p.add_argument("--mark", action="store_true", default=True,
                   help="cut a diagonal across the top left corner, so the "
                        "sheet shows whether the frame came out mirrored")
    p.add_argument("--no-mark", dest="mark", action="store_false")
    p.add_argument("--dry", action="store_true",
                   help="check the claims and print the path, move nothing")
    args = p.parse_args()

    print(f"=== piplot pen box v{VERSION} ===")
    print("Draws an inset rectangle to check registration, pen height and reach")
    print("https://github.com/sui001/piplot")
    print()

    if args.driver == "grbl":
        # GRBL has no model table to look the envelope up in, and its own
        # $130/$131 are left at 5000, so the number has to be supplied.
        tx, ty = args.travel or (420.0, 297.0)
        name = "GRBL CoreXY (suidraw-0)"
    else:
        tx, ty, name = MODELS[args.model]
    pw, ph = args.paper
    i = args.inset
    x0, y0, x1, y1 = i, i, pw - i, ph - i

    print(f"machine  {args.driver}, {name}, travel {tx:.0f} x {ty:.0f} mm")
    print(f"paper    {pw:.0f} x {ph:.0f} mm, inset {i:.0f} mm")
    print(f"box      ({x0:.0f}, {y0:.0f}) to ({x1:.0f}, {y1:.0f}) "
          f"= {x1 - x0:.0f} x {y1 - y0:.0f} mm")
    print()

    c = Claims()
    c.require("inset leaves a box at all", x1 > x0 and y1 > y0,
              f"{x1 - x0:.0f} x {y1 - y0:.0f} mm")
    c.require("box starts inside travel", x0 >= 0 and y0 >= 0)
    c.require("box width within travel", x1 <= tx,
              f"needs {x1:.0f} mm, machine has {tx:.0f} mm")
    c.require("box height within travel", y1 <= ty,
              f"needs {y1:.0f} mm, machine has {ty:.0f} mm")
    if pw > tx or ph > ty:
        c.note("paper is larger than the travel",
               "the box fits, the full sheet does not")
    print()

    if not c.ok:
        print("REFUSING TO MOVE. The box does not fit this machine.", file=sys.stderr)
        print("Check --model is right before you argue with --paper.", file=sys.stderr)
        return 1

    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]

    # A plain rectangle is symmetrical, so it looks identical whether or not
    # the frame came out mirrored. The diagonal is what makes the sheet
    # readable: it chops the TOP LEFT corner, and top left is the origin corner
    # in this frame, so if the triangle lands anywhere else the mapping is
    # wrong. The GRBL machine runs y the other way up, which makes this the
    # cheapest possible check on the backend's flip.
    m = min(40.0, (x1 - x0) / 3, (y1 - y0) / 3)
    mark = [(x0, y0 + m), (x0 + m, y0)]
    paths = [corners, mark] if args.mark else [corners]

    if args.dry:
        print("dry run, paths would be:")
        for pn, path in enumerate(paths, 1):
            print(f"  path {pn}")
            for n, (x, y) in enumerate(path):
                print(f"    {n}  {x:7.1f}, {y:7.1f}")
        print("\nnothing moved")
        return 0

    if args.driver == "grbl":
        return plot_grbl(args, paths, (tx, ty))

    from pyaxidraw import axidraw

    ad = axidraw.AxiDraw()
    ad.interactive()
    o = ad.options
    o.units = 2
    o.model = args.model
    o.speed_pendown = args.speed
    o.speed_penup = 75
    o.pen_pos_down = args.pen_down
    o.pen_pos_up = args.pen_up
    if args.port:
        # 0, not 1: port_config 1 means "first AxiDraw found", not this one.
        o.port = args.port
        o.port_config = 0

    device = getattr(args, "port", None) or ""
    lock = hold(device)
    if not lock.__enter__():
        print(f"{device} is in use by something else", file=sys.stderr)
        return 1

    if not ad.connect():
        lock.__exit__(None, None, None)
        print("no AxiDraw found", file=sys.stderr)
        return 1
    print("connected")

    try:
        ad.penup()

        if not args.no_preview:
            print("pen-up lap, watch it before anything is committed to paper")
            for x, y in corners:
                ad.moveto(x, y)
            time.sleep(1.0)

        print("drawing")
        ad.moveto(x0, y0)
        ad.pendown()
        for x, y in corners[1:]:
            ad.lineto(x, y)
        ad.penup()

        print("returning home")
        ad.moveto(0, 0)
    finally:
        ad.disconnect()
        lock.__exit__(None, None, None)

    print("done, pen up, carriage home")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
