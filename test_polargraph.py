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

from polargraph import (Polargraph, fluidnc_frame, max_segment_length,
                        stairwell, whiteboard)

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
