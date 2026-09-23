"""Polargraph kinematics: paper millimetres to and from two cord lengths.

This module is deliberately **pure**. Standard library only, no serial, no
piplot imports, no numpy. That is not tidiness for its own sake: the same
arithmetic has to run in three places that share nothing.

    on the Pi      driving a dumb two stepper board over serial
    in the browser previewing a path before anything moves
    on a board     FluidNC's WallPlotter kinematics, or a bare ESP32 sketch

Anything that imports pyserial cannot be transcribed to C++ in an afternoon.
This can. `inverse` and `forward` are a dozen lines between them and use only
`sqrt`, which an ESP32-S3 does in hardware.

The frame
---------
Two anchors sit `span` apart on one horizontal line. Machine coordinates put
the **left anchor at (0, 0)** with **+x right and +y down**, because down is
where the pen is and a polargraph never goes up past its anchors.

piplot's paper frame is also y down, but its origin is the top left corner of
the paper, which hangs somewhere below and between the anchors. `origin` is
the offset between the two, so the rest of piplot keeps thinking in paper mm
and never learns that this machine is not cartesian.

The thing that bites
--------------------
**A straight line on the paper is not a straight line in cord space.** Feed a
board two cord lengths and let it interpolate linearly between them and you
get a curve, bowing away from the line you asked for. Every polargraph does
this, and it is why long strokes come out wrong on a machine whose numbers all
look right.

The fix is to cut long moves into pieces short enough that the bow falls under
a tolerance you chose on purpose. `segment()` does that adaptively and `bow()`
measures it, so the tolerance can be argued with rather than guessed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple

Point = Tuple[float, float]

# Below this the cord runs so near horizontal that tension runs away. At 20
# degrees a cord already carries about three times the gondola's weight; at 6
# degrees it carries ten. Nothing breaks, but the belt stretches, the gondola
# judders, and the drawing is worst exactly where it is most visible. Every
# polargraph has dead corners. This is where ours are declared.
MIN_CORD_ANGLE_DEG = 20.0


@dataclass
class Polargraph:
    """One wall hanging plotter, described in millimetres.

    span          distance between the two anchor points
    travel        usable paper size (width, height)
    origin        paper's top left corner, in machine mm from the LEFT anchor
    gondola_g     pen carriage mass, used only by the sag and tension warnings
    cord_g_per_m  cord mass per metre, likewise. GT2 6 mm belt is about 4.5
    """

    span: float
    travel: Tuple[float, float]
    origin: Tuple[float, float]
    gondola_g: float = 800.0
    cord_g_per_m: float = 4.5
    min_cord_angle_deg: float = MIN_CORD_ANGLE_DEG

    # ---- frames ------------------------------------------------------------

    def to_machine(self, x: float, y: float) -> Point:
        """Paper mm into machine mm. Both are y down."""
        return (x + self.origin[0], y + self.origin[1])

    def to_paper(self, mx: float, my: float) -> Point:
        return (mx - self.origin[0], my - self.origin[1])

    # ---- kinematics --------------------------------------------------------

    def inverse(self, x: float, y: float) -> Point:
        """Paper mm to cord lengths (left, right) in mm.

        Straight line distance from each anchor. The cord's own sag is NOT
        modelled here and does not need to be: with a heavy gondola the error
        is under a hundredth of a millimetre on the bench rig and a few
        millimetres at nine metres. When it does need correcting it belongs in
        one named place, not buried in the transform everything else calls.
        """
        mx, my = self.to_machine(x, y)
        return (math.hypot(mx, my), math.hypot(self.span - mx, my))

    def forward(self, a: float, b: float) -> Point:
        """Cord lengths back to paper mm. The inverse of `inverse`.

        Two circles, one round each anchor, meeting below the anchor line.
        Used to check the transform against itself, and to work out where the
        pen actually ended up after a board interpolated something.
        """
        mx = (a * a - b * b + self.span * self.span) / (2.0 * self.span)
        under = a * a - mx * mx
        if under <= 0.0:
            raise ValueError(
                f"cords {a:.1f} and {b:.1f} mm across a {self.span:.0f} mm "
                "span do not reach each other"
            )
        return self.to_paper(mx, math.sqrt(under))

    # ---- the bow -----------------------------------------------------------

    def bow(self, p0: Point, p1: Point, samples: int = 32) -> float:
        """How far a straight move wanders, in mm, if nobody segments it.

        Interpolates the two cord lengths linearly from p0 to p1, which is
        exactly what a board does when handed two axis targets, and measures
        the worst gap between where the pen goes and the straight line that
        was asked for. This is the number `segment` exists to shrink.
        """
        a0, b0 = self.inverse(*p0)
        a1, b1 = self.inverse(*p1)
        worst = 0.0
        for i in range(1, samples):
            t = i / samples
            try:
                px, py = self.forward(a0 + (a1 - a0) * t, b0 + (b1 - b0) * t)
            except ValueError:
                continue
            wx = p0[0] + (p1[0] - p0[0]) * t
            wy = p0[1] + (p1[1] - p0[1]) * t
            worst = max(worst, math.hypot(px - wx, py - wy))
        return worst

    def segment(self, p0: Point, p1: Point, tol: float = 0.1,
                max_depth: int = 12) -> List[Point]:
        """p0 to p1 cut fine enough that the bow stays under `tol` mm.

        Returns the interior points plus p1, never p0, so paths chain without
        duplicating their joins. Bisects rather than stepping at a fixed
        length, because the bow is worst low and central and near zero up by
        the anchors, so one fixed step is either wasteful or wrong depending
        on where you happen to be drawing.

        `max_depth` caps one move at 4096 pieces, which no sane tolerance
        reaches. It is there so that a `tol` of zero cannot hang the server.
        """
        out: List[Point] = []

        def rec(a: Point, b: Point, depth: int) -> None:
            if depth >= max_depth or self.bow(a, b, samples=8) <= tol:
                out.append(b)
                return
            mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            rec(a, mid, depth + 1)
            rec(mid, b, depth + 1)

        rec(tuple(p0), tuple(p1), 0)
        return out

    def segment_path(self, points: Sequence[Point],
                     tol: float = 0.1) -> List[Point]:
        """A whole path, every move segmented. First point kept as given."""
        if not points:
            return []
        out: List[Point] = [tuple(points[0])]
        for nxt in points[1:]:
            out.extend(self.segment(out[-1], tuple(nxt), tol))
        return out

    # ---- physics, for warnings only ----------------------------------------

    def cord_angle_deg(self, x: float, y: float) -> Tuple[float, float]:
        """Each cord's angle from horizontal, in degrees. Small is bad."""
        mx, my = self.to_machine(x, y)
        return (math.degrees(math.atan2(my, mx)),
                math.degrees(math.atan2(my, self.span - mx)))

    def sag_error_mm(self, x: float, y: float) -> float:
        """Pen displacement caused by the cords hanging in a curve, in mm.

        A sagging cord is longer than the straight line `inverse` solves for,
        so the pen sits short of where it was sent. What lands on the paper is
        that arc excess, not the sag itself, which is an order of magnitude
        larger and much more alarming to look at.
        """
        g = 9.81
        w = self.cord_g_per_m / 1000.0 * g          # newtons per metre of cord
        weight = self.gondola_g / 1000.0 * g
        lengths = [v / 1000.0 for v in self.inverse(x, y)]
        angles = [math.radians(d) for d in self.cord_angle_deg(x, y)]
        grown: List[float] = []
        for length, ang in zip(lengths, angles):
            # Share the load by how vertical each cord is, then a parabola's
            # excess length over its chord is 8 s^2 / 3 L.
            tension = max((weight / 2.0) / max(math.sin(ang), 0.1), 1e-6)
            s = w * length * length / (8.0 * tension)
            grown.append(length + 8.0 * s * s / (3.0 * length))
        try:
            px, py = self.forward(grown[0] * 1000.0, grown[1] * 1000.0)
        except ValueError:
            return float("inf")
        return math.hypot(px - x, py - y)

    # ---- refusals ----------------------------------------------------------

    def check(self, points: Sequence[Point]) -> List[str]:
        """Reasons this path must not be sent, as plain sentences.

        Empty means it is safe. Same contract as `Grbl.check`, and for the
        same reason: a polargraph has no limit switches and no soft limits, so
        an unreachable point is refused nowhere else. The gondola jams against
        an anchor, the belt skips teeth, and every coordinate after that is
        wrong with nothing reported.
        """
        if not points:
            return ["the path has no points"]
        tw, th = self.travel
        bad: List[str] = []

        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        if min(xs) < 0 or max(xs) > tw:
            bad.append(f"x spans {min(xs):.1f} to {max(xs):.1f} mm, "
                       f"paper is 0 to {tw:.0f}")
        if min(ys) < 0 or max(ys) > th:
            bad.append(f"y spans {min(ys):.1f} to {max(ys):.1f} mm, "
                       f"paper is 0 to {th:.0f}")

        worst_angle = 90.0
        worst_at = tuple(points[0])
        for p in points:
            mx, my = self.to_machine(*p)
            if my <= 0:
                bad.append(f"({p[0]:.1f}, {p[1]:.1f}) is level with or above "
                           "the anchors, where the gondola cannot hang")
                break
            if not (0.0 < mx < self.span):
                bad.append(f"({p[0]:.1f}, {p[1]:.1f}) is outside the anchors "
                           f"horizontally, {mx:.1f} mm across a "
                           f"{self.span:.0f} mm span")
                break
            lo = min(self.cord_angle_deg(*p))
            if lo < worst_angle:
                worst_angle, worst_at = lo, tuple(p)

        if worst_angle < self.min_cord_angle_deg:
            pull = 1.0 / max(math.sin(math.radians(worst_angle)), 1e-6)
            bad.append(
                f"at ({worst_at[0]:.1f}, {worst_at[1]:.1f}) a cord is only "
                f"{worst_angle:.1f} degrees off horizontal, under the "
                f"{self.min_cord_angle_deg:.0f} degree floor. It carries "
                f"{pull:.1f}x the gondola weight there and the line will be poor"
            )
        return bad


# ---- named rigs ------------------------------------------------------------

def whiteboard(span: float = 940.0,
               board: Tuple[float, float] = (930.0, 1500.0),
               drop: float = 500.0, **kw) -> Polargraph:
    """The bench rig: a portrait whiteboard, 930 x 1500 mm of usable area.

    The board is 1100 x 1700 mm physically. Only the usable area is modelled,
    because the frame is not drawable and a machine that believes otherwise
    will cheerfully try to draw on it.

    Both defaults came out of a sweep, and both are counterintuitive.

    **span = 940, ten millimetres wider than the board.** Widening the anchors
    makes a polargraph worse, not better, because a wider span lays the far
    cord flatter at the top corners, which is exactly where the angle floor
    bites. The worst corner cord goes 28.1, 26.3, 24.2, 21.6 degrees as the
    span goes 940, 1090, 1300, 1600 mm. Monotonic, no exceptions. At a 350 mm
    drop the same walk crosses the floor and corners start being refused
    outright. The anchors want to be barely wider than the paper.

    **drop = 500, half a metre of clear wall above the board.** 350 mm is the
    least that covers every corner, but it leaves the worst cord at 20.5
    degrees, half a degree inside the floor. 500 mm puts it at 28 degrees and
    cuts peak cord tension from 11.2 N to 8.3 N, for 150 mm of wall and about
    120 mm of extra belt. That margin is also what makes the span forgiving:
    at 500 mm even a 1600 mm span still clears the floor, so getting the
    mounting slightly wrong stops mattering.

    So the lever is height above the paper, not width: mount the motors high
    and barely wider than the sheet.

    An earlier sweep sampled cell centres and concluded a span NARROWER than
    the board was fine. It is not. It never tested a corner, which is the only
    place this fails. Sample the edges.
    """
    return Polargraph(span=span, travel=board,
                      origin=((span - board[0]) / 2.0, drop), **kw)


def stairwell(span: float = 2500.0,
              sheet: Tuple[float, float] = (2200.0, 8500.0),
              drop: float = 300.0, gondola_g: float = 1500.0,
              **kw) -> Polargraph:
    """The full shaft: 2.5 m anchors, a sheet most of three flights tall."""
    return Polargraph(span=span, travel=sheet,
                      origin=((span - sheet[0]) / 2.0, drop),
                      gondola_g=gondola_g, **kw)


def max_segment_length(m: Polargraph, tol: float = 0.1,
                       grid_n: int = 15) -> float:
    """The largest FIXED segment length that keeps the bow under `tol` mm.

    `segment()` is adaptive, which is right when piplot is doing the work.
    FluidNC's WallPlotter kinematics is not: its YAML takes one
    `segment_length` in mm and applies it everywhere, with a source comment
    saying that a small enough value hides the nonlinearity. It does not say
    what small enough is, because that depends on the machine.

    So this sweeps the sheet in the worst direction (horizontal, where the bow
    is largest) and returns the number to put in the config. Round it DOWN
    before using it.
    """
    tw, th = m.travel
    lo, hi = 0.5, max(tw, th)
    for _ in range(24):
        mid = (lo + hi) / 2.0
        worst = 0.0
        for j in range(grid_n):
            y = th * (j + 0.5) / grid_n
            x = 0.0
            while x < tw:
                nxt = min(x + mid, tw)
                worst = max(worst, m.bow((x, y), (nxt, y), samples=8))
                if worst > tol:
                    break
                x = nxt
            if worst > tol:
                break
        if worst <= tol:
            lo = mid
        else:
            hi = mid
    return lo


def fluidnc_frame(m: Polargraph, home: Point | None = None) -> dict:
    """The numbers FluidNC's WallPlotter kinematics wants, from this rig.

    FluidNC does not share piplot's frame and cannot be talked into it:

    - **Its y runs up.** The stock config puts both anchors at y = +100, ie
      above the origin. piplot's y runs down the page.
    - **Its origin is wherever the pen is at power on**, because `WallPlotter`
      derives its zero cord lengths from cartesian (0, 0) in `init()` and
      `canHome()` returns false. There is no homing on a polargraph, ever.

    So the origin is a spot you park the gondola on by hand. `home` says which
    paper point that is, defaulting to the middle of the sheet, which is the
    easiest place to measure to and the most forgiving of a small error, since
    it puts the mistake in the middle rather than at an edge.

    Returns the four anchor values plus the paper's extents in FluidNC's frame,
    so a config can be checked against this rather than typed twice.
    """
    if home is None:
        home = (m.travel[0] / 2.0, m.travel[1] / 2.0)
    hx, hy = m.to_machine(*home)
    return {
        "left_anchor_x": 0.0 - hx,
        "left_anchor_y": hy,                    # machine y is down, FluidNC's is up
        "right_anchor_x": m.span - hx,
        "right_anchor_y": hy,
        "x_min": -home[0],
        "x_max": m.travel[0] - home[0],
        "y_min": home[1] - m.travel[1],
        "y_max": home[1],
        "home_paper": home,
    }


def belt_length_mm(m: Polargraph, pulley_teeth: int = 20, pitch: float = 2.0,
                   clamp: float = 60.0, tail_min: float = 250.0,
                   grid_n: int = 41) -> dict:
    """How much belt one side needs, and how much tail hangs below the motor.

    A toothed belt polargraph has no spool. One end is clamped to the gondola,
    the belt runs up and over the pulley, and the rest hangs down the back as
    a tail with a small weight on it. Belt transfers from tail to gondola as
    the gondola descends, so the total length never changes.

    That means the length is set by the LONGEST cord plus the shortest tail
    you will tolerate, and nothing else. It is NOT the longest cord plus the
    travel range: an earlier version of this calculation added the range on
    top and overstated the requirement by seventy percent, which is the
    difference between having enough printer spares and not.

    `tail_min` is how much tail must still be hanging when the gondola is at
    its furthest, so the weight never gets drawn up into the pulley.
    """
    tw, th = m.travel
    pts = [(tw * i / (grid_n - 1), th * j / (grid_n - 1))
           for i in range(grid_n) for j in range(grid_n)]
    cords = [m.inverse(*p) for p in pts]
    longest = max(max(c) for c in cords)
    shortest = min(min(c) for c in cords)
    wrap = math.pi * (pulley_teeth * pitch) / (2.0 * math.pi)   # half the pitch circle
    per_side = longest + wrap + clamp + tail_min
    return {
        "per_side": per_side,
        "total": 2.0 * per_side,
        "longest_cord": longest,
        "shortest_cord": shortest,
        "tail_at_furthest": tail_min,
        "tail_at_closest": per_side - shortest - wrap - clamp,
    }


def min_drop_mm(span: float, sheet: Tuple[float, float],
                min_angle: float = MIN_CORD_ANGLE_DEG,
                step: float = 5.0, limit: float = 3000.0) -> float | None:
    """The least clearance above the paper that keeps every corner legal.

    The number to hold a tape measure against when hanging the beam. A wider
    span needs more: the far cord lies flatter at the top corners, and height
    above the paper is what lifts it back up.
    """
    w, h = sheet
    drop = step
    while drop <= limit:
        m = Polargraph(span=span, travel=(w, h), origin=((span - w) / 2.0, drop),
                       min_cord_angle_deg=min_angle)
        if all(not m.check([c]) for c in ((0.0, 0.0), (w, 0.0), (0.0, h), (w, h))):
            return drop
        drop += step
    return None


def rig_report(m: Polargraph, samples: int = 15) -> dict:
    """Everything worth knowing about a rig, from its geometry alone.

    Pure arithmetic, no hardware: this is what the setup page shows while
    someone is still holding a tape measure, and what a claim can be written
    against. Every length is mm, every angle degrees, force newtons.

    `refusals` is the honest bottom line: empty means every corner and edge
    of the paper can actually be drawn.
    """
    w, h = m.travel
    n = max(3, samples)
    edges = [(w * i / (n - 1), h * j / (n - 1)) for i in range(n) for j in range(n)]
    corners = [(0.0, 0.0), (w, 0.0), (0.0, h), (w, h)]

    angles = [min(m.cord_angle_deg(*p)) for p in edges]
    cords = [m.inverse(*p) for p in edges]
    longest = max(max(c) for c in cords)
    shortest = min(min(c) for c in cords)

    weight = m.gondola_g / 1000.0 * 9.81
    tensions = [(weight / 2.0) / math.sin(math.radians(a)) for a in angles]
    belt = belt_length_mm(m)
    ok_edges = sum(1 for p in edges if not m.check([p]))

    refusals = []
    for label, p in (("top left", corners[0]), ("top right", corners[1]),
                     ("bottom left", corners[2]), ("bottom right", corners[3])):
        for r in m.check([p]):
            refusals.append(f"{label}: {r}")

    return {
        "span": m.span,
        "drop": m.origin[1],
        "sheet": [w, h],
        "gondola_g": m.gondola_g,
        "min_angle_deg": min(angles),
        "min_angle_floor": m.min_cord_angle_deg,
        "min_drop_mm": min_drop_mm(m.span, (w, h), m.min_cord_angle_deg),
        "usable_fraction": ok_edges / len(edges),
        "longest_cord": longest,
        "shortest_cord": shortest,
        "belt_per_side": belt["per_side"],
        "belt_total": belt["total"],
        "tail_at_closest": belt["tail_at_closest"],
        "peak_tension_n": max(tensions),
        "min_tension_n": min(tensions),
        "tail_weight_max_g": min(tensions) / 9.81 * 1000.0,
        "torque_20t_ncm": max(tensions) * (20 * 2.0 / (2 * math.pi)) / 1000.0 * 100.0,
        "segment_length_mm": max_segment_length(m, 0.1),
        "bow_unsegmented_mm": m.bow((0.0, h / 2.0), (w, h / 2.0)),
        "worst_sag_mm": max(m.sag_error_mm(*p) for p in edges),
        "wall_width": m.span + 200.0,
        "wall_height": m.origin[1] + h,
        "fluidnc": fluidnc_frame(m),
        "refusals": refusals,
    }
