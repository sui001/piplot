"""Claims about the polargraph transform, checked against the transform.

Run it: `python test_polargraph.py`. Exits 1 on any violation and says which.

This is a claims ledger, not a print out. The failure it is written against is
the one every polargraph build hits: the maths round trips perfectly, the
numbers all look right, and the drawing still bows, because nothing ever
measured the one thing that goes wrong between the transform and the paper.
"""

from __future__ import annotations

import math
import os
import re
import sys

from polargraph import (Polargraph, belt_length_mm, fluidnc_frame,
                        max_segment_length, stairwell, whiteboard)

YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "fluidnc-polargraph-bench.yaml")

NL = chr(10)

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
    for m, name in ((bench, "bench"), (shaft, "shaft")):
        tw, th = m.travel
        b = m.bow((0.0, th / 2.0), (tw, th / 2.0))
        note(f"{name}: one {tw:.0f} mm horizontal line at mid height, "
             f"unsegmented, bows {b:.2f} mm")
    require(bench.bow((0.0, 750.0), (930.0, 750.0)) > 1.0,
            "the bow is real and larger than a pen nib, so segmenting is not "
            "optional")

    print("\nsegmenting shrinks the bow to the tolerance asked for")
    for tol in (1.0, 0.25, 0.05):
        pts = bench.segment_path([(0.0, 750.0), (930.0, 750.0)], tol=tol)
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
    require(bench.check([(465.0, 750.0)]) == [],
            "the middle of the board is accepted")
    require(any("paper is" in r for r in bench.check([(-50.0, 750.0)])),
            "a point off the left edge is refused")
    # Deliberately badly hung: the board's top edge 40 mm under the anchors,
    # which is where every first build puts it. The floor has to catch that.
    flat = whiteboard(drop=40.0)
    require(any("degrees off horizontal" in r
                for r in flat.check([(465.0, 1.0)])),
            "on a badly hung rig the flat top edge is refused by the angle "
            "floor and not silently drawn")
    require(bench.check([(465.0, 1.0)]) == [],
            "on the 500 mm drop the same point is safe, which is the whole "
            "reason for hanging it lower")
    require(bench.check([(465.0, 1400.0)]) == [],
            "low on the board, where the cords hang steep, is accepted")

    print("\nthe dead corners are declared, not discovered on paper")
    tw, th = bench.travel
    corners = [(0.0, 0.0), (tw, 0.0), (0.0, th), (tw, th)]
    require(all(not bench.check([c]) for c in corners),
            "every CORNER of the board is reachable. Cell centres are not "
            "enough: an earlier sweep sampled only those and concluded a span "
            "NARROWER than the board was fine, which it is not")
    n = 21
    edges = [(tw * i / (n - 1), th * j / (n - 1))
             for i in range(n) for j in range(n)]
    frac = sum(1 for p in edges if not bench.check([p])) / len(edges)
    note(f"bench: {frac * 100:.0f}% of the board is drawable at a "
         f"{bench.min_cord_angle_deg:.0f} degree floor, edges included")
    require(frac == 1.0,
            "all of the usable area is drawable, edges and corners included")
    # Widening the anchors makes a polargraph WORSE, and this is the evidence.
    # The worst corner cord lies flatter at every step out, at any drop. At a
    # 350 mm drop that crosses the floor and corners start being refused; at
    # 500 mm it does not, which is what the extra 150 mm of wall bought.
    for drop, refuses in ((350.0, True), (500.0, False)):
        angs = [min(min(whiteboard(span=s, drop=drop).cord_angle_deg(*c))
                    for c in corners)
                for s in (940.0, 1090.0, 1300.0, 1600.0)]
        note(f"drop {drop:.0f} mm: worst corner cord goes "
             f"{' -> '.join(f'{a:.1f}' for a in angs)} deg as the span goes "
             "940 -> 1600 mm")
        require(all(b < a for a, b in zip(angs, angs[1:])),
                f"at a {drop:.0f} mm drop every widening of the span lays the "
                "worst corner cord flatter, with no exception")
        hit = any(whiteboard(span=1600.0, drop=drop).check([c])
                  for c in corners)
        require(hit is refuses,
                f"at a {drop:.0f} mm drop a 1600 mm span "
                f"{'does' if refuses else 'does not'} refuse a corner")

    print(NL + "belt, which is the one part that might not be in a drawer")
    b = belt_length_mm(bench)
    note(f"bench: {b['per_side'] / 1000:.2f} m per side, "
         f"{b['total'] / 1000:.2f} m total, longest cord "
         f"{b['longest_cord'] / 1000:.2f} m")
    note(f"bench: tail hangs {b['tail_at_closest'] / 1000:.2f} m at the "
         f"gondola's closest point, so leave that under each motor")
    require(b["per_side"] > b["longest_cord"],
            "belt per side exceeds the longest cord, or the gondola cannot "
            "reach the far corner at all")
    require(b["per_side"] < b["longest_cord"] + b["shortest_cord"],
            "belt per side is NOT longest cord plus travel range. An earlier "
            "version added the range on top and overstated it by 70%, which "
            "is the difference between having enough spares and not")
    require(b["tail_at_closest"] < 2000.0,
            "the tail never hangs more than 2 m, which is the clear wall "
            "under the motors")

    print(NL + "the FluidNC config says the same thing this module does")
    if not os.path.exists(YAML):
        require(False, f"{os.path.basename(YAML)} is present")
    else:
        text = open(YAML, encoding="utf-8").read()

        def key(name: str):
            hit = re.search(rf"^\s*{name}:\s*(-?[\d.]+)", text, re.M)
            return float(hit.group(1)) if hit else None

        want = fluidnc_frame(bench)
        for k in ("left_anchor_x", "left_anchor_y",
                  "right_anchor_x", "right_anchor_y"):
            require(key(k) is not None and abs(key(k) - want[k]) < 1e-6,
                    f"config {k} is {key(k)}, model says {want[k]}")

        seg = key("segment_length")
        cap = max_segment_length(bench, 0.1)
        require(seg is not None and seg <= cap,
                f"config segment_length {seg} mm is at or under the "
                f"{cap:.1f} mm that holds the bow to 0.1 mm")

        spm = key("steps_per_mm")
        require(spm == 80.0,
                "config steps_per_mm is 80, ie GT2 on a 20 tooth pulley at "
                "1/16 microstepping")
        require("soft_limits: false" in text and "must_home: false" in text,
                "config admits it cannot home and has no soft limits, so the "
                "Pi side check() is known to be the only guard")
        require("gpio.3" not in text,
                "config uses no gpio.3, which is an ESP32-S3 strapping pin")
        pins = set(re.findall(r"gpio\.(\d+)", text))
        require(all(1 <= int(g) <= 13 for g in pins),
                f"every pin used ({', '.join(sorted(pins, key=int))}) is on "
                "the SuperMini header, not a pad needing solder")

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
