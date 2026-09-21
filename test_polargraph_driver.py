"""Claims about the Polargraph driver, checked without a board attached.

Run it: `python test_polargraph_driver.py`. Exits 1 on any violation.

Everything here is the part of the driver that does not touch hardware: the
frame mapping, the refusals, the G-code it emits. That is deliberate. The
frame mapping is the piece most likely to be silently wrong, because a y flip
about the wrong axis produces a drawing that is plausible, mirrored, and
reported as a success by every other part of the system.

The claim that matters most is the first one: the driver's `_xy` and
`polargraph.fluidnc_frame` are two independent expressions of the same
mapping, one used to send moves and one used to write the board's config. If
they ever disagree, the machine draws in the wrong place and nothing says so.
"""

from __future__ import annotations

import sys

import plotter
import polargraph

FAILS: list[str] = []


def require(ok: bool, claim: str) -> None:
    print(f"  {'pass' if ok else 'FAIL'}  {claim}")
    if not ok:
        FAILS.append(claim)


def main() -> int:
    geom = polargraph.whiteboard()
    p = plotter.Polargraph(geom, host="fluidnc.local")
    f = polargraph.fluidnc_frame(geom)
    tw, th = geom.travel

    print("\nthe driver's frame and the config's frame are the same mapping")
    for paper, want in (((0.0, 0.0), (f["x_min"], f["y_max"])),
                        ((tw, 0.0), (f["x_max"], f["y_max"])),
                        ((0.0, th), (f["x_min"], f["y_min"])),
                        ((tw, th), (f["x_max"], f["y_min"]))):
        got = p._xy(*paper)
        require(abs(got[0] - want[0]) < 1e-9 and abs(got[1] - want[1]) < 1e-9,
                f"paper {paper} maps to {got}, config frame says {want}")
    require(p._xy(*p.home) == (0.0, 0.0),
            "the park point maps to FluidNC's (0, 0), which is where it "
            "derives its zero cord lengths at boot")
    require(p._xy(tw / 2.0, 0.0)[1] > p._xy(tw / 2.0, th)[1],
            "y is INVERTED between the frames: the top of the paper is the "
            "larger FluidNC y, because FluidNC's y runs up and piplot's down")

    print("\nthe envelope check is the geometry's, not a rectangle")
    require(p.check([(tw / 2.0, th / 2.0)]) == [],
            "the middle of the board is accepted")
    require(any("paper is" in r for r in p.check([(-50.0, th / 2.0)])),
            "a point off the paper is refused")
    flat = plotter.Polargraph(polargraph.whiteboard(drop=40.0), host="x")
    require(any("degrees off horizontal" in r
                for r in flat.check([(tw / 2.0, 1.0)])),
            "on a badly hung rig a flat cord is refused by the ANGLE floor, "
            "which a rectangle check could never catch")
    require(p.check([(tw, th)]) == [],
            "the far corner of a correctly hung rig is accepted")

    print("\nrefusals that exist because this machine cannot home")
    try:
        p.set_origin_here()
        require(False, "set_origin_here refuses")
    except NotImplementedError as e:
        require("reboot" in str(e),
                "set_origin_here refuses and says to reboot instead, because "
                "a G10 work offset would move the labels without re-deriving "
                "the zero cord lengths")

    print("\none transport, never two and never none")
    for kw, label in (({}, "neither"),
                      ({"host": "a", "serial_port": "b"}, "both")):
        try:
            plotter.Polargraph(geom, **kw)
            require(False, f"{label} transport is refused")
        except ValueError:
            require(True, f"{label} transport is refused")

    print("\nthe G-code it emits")
    lines = p.encode([(0.0, th / 2.0), (tw / 2.0, th / 2.0), (tw, th / 2.0)])
    require(lines[0] == f"G0Z{p.pen_up_z:g}",
            "a path starts with a pen lift, not a move")
    require(lines[-1] == f"G0Z{p.pen_up_z:g}",
            "and ends with one, so the next travel does not drag")
    require(any(ln.startswith("G1") for ln in lines),
            "the drawn part uses G1 with a feed, not G0")
    require(all("Y" not in ln for ln in lines[5:-1]),
            "a horizontal line emits no Y at all after the first move, since "
            "modal economy is what buys planner lookahead")
    require(not any("nan" in ln.lower() for ln in lines),
            "no NaN reaches the wire")

    print("\nit does NOT pre-segment, because the board already does")
    long_path = [(0.0, th / 2.0), (tw, th / 2.0)]
    emitted = p.encode(long_path)
    moves = [ln for ln in emitted
             if ln.startswith("G1") or ln[:1] in ("X", "Y")]
    require(len(moves) == len(long_path) - 1,
            f"a {len(long_path)} point path emits {len(moves)} drawn move(s), "
            "one per segment given and not one more. Segmenting here as well "
            "as on the board would multiply the bytes for nothing, and bytes "
            "are what limit the planner's lookahead")
    would_be = len(geom.segment_path(long_path, tol=0.1)) - 1
    require(would_be > 30,
            f"the geometry WOULD cut that same move into {would_be} pieces if "
            "asked, so the driver leaving it alone is a decision and not an "
            "oversight")

    print()
    if FAILS:
        print(f"{len(FAILS)} claim(s) violated:")
        for c in FAILS:
            print(f"  - {c}")
        return 1
    print("all claims hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
