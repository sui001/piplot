"""Claims about the polargraph transform, checked against the transform.

Run it: `python test_polargraph.py`. Exits 1 on any violation and says which.

This is a claims ledger, not a print out. The failure it is written against is
the one every polargraph build hits: the maths round trips perfectly, the
numbers all look right, and the drawing still bows, because nothing ever
measured the one thing that goes wrong between the transform and the paper.
"""

from __future__ import annotations

import math
import sys

from polargraph import Polargraph, stairwell, whiteboard

FAILS: list[str] = []
NOTES: list[str] = []


def require(ok: bool, claim: str) -> None:
    print(f"  {'pass' if ok else 'FAIL'}  {claim}")
    if not ok:
        FAILS.append(claim)


def note(text: str) -> None:
    print(f"  note  {text}")
    NOTES.append(text)


def grid(m: Polargraph, n: int = 9):
    tw, th = m.travel
    for i in range(n):
        for j in range(n):
            yield (tw * (i + 0.5) / n, th * (j + 0.5) / n)


def main() -> int:
    bench = whiteboard()
    shaft = stairwell()

    print("\nround trip: inverse then forward returns the point it was given")
    worst = 0.0
    for m, name in ((bench, "bench"), (shaft, "shaft")):
        w = max(math.hypot(*(a - b for a, b in zip(m.forward(*m.inverse(*p)), p)))
                for p in grid(m, 15))
        worst = max(worst, w)
        note(f"{name}: worst round trip error {w * 1e6:.3f} nanometres")
    require(worst < 1e-6, "round trip closes to under a micron everywhere")

    print("\nthe bow: what an unsegmented move actually does")
    for m, name, span in ((bench, "bench", 1500.0), (shaft, "shaft", 2200.0)):
        tw, th = m.travel
        y = th * 0.75
        b = m.bow((tw * 0.05, y), (tw * 0.95, y))
        note(f"{name}: one {span * 0.9:.0f} mm horizontal line, unsegmented, "
             f"bows {b:.2f} mm")
    require(bench.bow((75.0, 750.0), (1425.0, 750.0)) > 1.0,
            "the bow is real and larger than a pen nib, so segmenting is not "
            "optional")

    print("\nsegmenting shrinks the bow to the tolerance asked for")
    for tol in (1.0, 0.25, 0.05):
        pts = bench.segment_path([(75.0, 750.0), (1425.0, 750.0)], tol=tol)
        worst = max(bench.bow(a, b) for a, b in zip(pts, pts[1:]))
        require(worst <= tol + 1e-9,
                f"tol {tol:>5.2f} mm: {len(pts) - 1:>3d} pieces, worst piece "
                f"bows {worst:.4f} mm")

    print("\nsegment counts stay sane over a whole sheet")
    for m, name in ((bench, "bench"), (shaft, "shaft")):
        tw, th = m.travel
        box = [(tw * 0.1, th * 0.1), (tw * 0.9, th * 0.1),
               (tw * 0.9, th * 0.9), (tw * 0.1, th * 0.9), (tw * 0.1, th * 0.1)]
        pts = m.segment_path(box, tol=0.1)
        note(f"{name}: a 4 sided box becomes {len(pts)} points at 0.1 mm")
        require(len(pts) < 2000,
                f"{name} box segments to under 2000 points, not a stream the "
                "board will choke on")

    print("\nsag, which is a warning and not a correction")
    for m, name in ((bench, "bench"), (shaft, "shaft")):
        vals = [m.sag_error_mm(*p) for p in grid(m, 11)]
        note(f"{name}: sag error median {sorted(vals)[len(vals) // 2]:.3f} mm, "
             f"worst {max(vals):.3f} mm, gondola {m.gondola_g:.0f} g")
    require(max(bench.sag_error_mm(*p) for p in grid(bench, 11)) < 0.1,
            "on the bench rig sag is under 0.1 mm, so it can be ignored there")
    require(max(shaft.sag_error_mm(*p) for p in grid(shaft, 11)) < 5.0,
            "in the shaft a 1.5 kg gondola keeps sag under 5 mm over 8.5 m")

    print("\nrefusals: the machine says no before the belt skips")
    require(bench.check([]) != [], "an empty path is refused")
    require(bench.check([(750.0, 500.0)]) == [],
            "the middle of the board is accepted")
    require(any("paper is" in r for r in bench.check([(-50.0, 500.0)])),
            "a point off the left edge is refused")
    # Deliberately badly hung: the board's top edge 40 mm under the anchors,
    # which is where every first build puts it. The floor has to catch that.
    flat = whiteboard(drop=40.0)
    require(any("degrees off horizontal" in r
                for r in flat.check([(750.0, 1.0)])),
            "on a badly hung rig the flat top edge is refused by the angle "
            "floor and not silently drawn")
    require(bench.check([(750.0, 1.0)]) == [],
            "on the 600 mm drop the same point is safe, which is the whole "
            "reason for hanging it lower")
    require(bench.check([(750.0, 900.0)]) == [],
            "low on the board, where the cords hang steep, is accepted")

    print("\nthe dead corners are declared, not discovered on paper")
    tw, th = bench.travel
    good = [p for p in grid(bench, 21) if not bench.check([p])]
    frac = len(good) / (21 * 21)
    note(f"bench: {frac * 100:.0f}% of the board is drawable at a "
         f"{bench.min_cord_angle_deg:.0f} degree floor")
    require(frac > 0.5,
            "more than half the whiteboard is usable, or the anchors are in "
            "the wrong place")

    print()
    if FAILS:
        print(f"{len(FAILS)} claim(s) violated:")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print(f"all claims hold ({len(NOTES)} notes recorded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
