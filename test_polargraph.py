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
                "config steps_per_mm is 80, ie GT2 on a 20 tooth pulley "
                "(40 mm/rev) at 1/16 microstepping. This is the one number "
                "that silently scales the whole drawing")
        require("soft_limits: false" in text and "must_home: false" in text,
                "config admits it cannot home and has no soft limits, so the "
                "Pi side check() is known to be the only guard")
        used = re.findall(r"^[^#\n]*gpio\.(\d+)", text, re.M)
        require(len(used) == 6,
                f"exactly six pins are driven, found {len(used)}. This is "
                "here because an earlier regex crossed line breaks, saw four "
                "of the six, and passed the duplicate check anyway")
        require(len(used) == len(set(used)),
                f"no pin is assigned twice (used: {', '.join(used)})")

        # The board is a classic ESP32 WROOM devkit (the S3 SuperMini is
        # parked, see fluidnc/). Its traps, each of which fails differently:
        board = re.search(r"^board:\s*(.+)$", text, re.M).group(1)
        require("WROOM" in board,
                f"config is for the WROOM devkit these pin rules describe "
                f"(board: {board})")
        pins = {int(g) for g in used}
        forbidden = {
            "a strapping pin (0 2 5 12 15), read at reset; 12 sets the "
            "flash voltage": {0, 2, 5, 12, 15},
            "wired to the module's flash chip (6-11)": set(range(6, 12)),
            "input only (34-39), cannot drive a step pin": set(range(34, 40)),
            "UART0 (1 3), which is the USB console": {1, 3},
        }
        for why, bad in forbidden.items():
            hit = sorted(pins & bad)
            require(not hit, f"no pin is {why}"
                    + (f": {hit}" if hit else ""))
        require(pins <= set(range(0, 40)),
                f"every pin exists on a classic ESP32 "
                f"({', '.join(map(str, sorted(pins)))})")
        dis = re.search(r"^\s*shared_stepper_disable_pin:\s*(\S+)", text, re.M)
        require(dis is not None and dis.group(1).endswith(":high"),
                "the shared disable pin is :high. It drives the TMC2209's EN "
                "directly, which is active LOW, so DISABLE is the high level. "
                ":low reads as the same idea and switches the drivers off "
                "whenever FluidNC means to run them")

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
