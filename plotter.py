"""Pen backends.

The AxiDraw is slow and the room is fast, so the plotter is never asked to
keep up. It is handed one segment at a time and draws whatever has piled up,
which is why the drawing lags and sediments rather than tracking live.

DryRun draws to an SVG file instead, at full speed, so the whole chain can be
run and looked at without the machine.
"""

from __future__ import annotations

from typing import Optional, Tuple


class Plotter:
    def connect(self) -> None:
        pass

    def penup(self) -> None:
        raise NotImplementedError

    def pendown(self) -> None:
        raise NotImplementedError

    def goto(self, x: float, y: float) -> None:
        """Move to paper mm with the pen in whatever state it is already in."""
        raise NotImplementedError

    def disconnect(self) -> None:
        pass


class DryRun(Plotter):
    """Accumulate strokes and write an SVG. No hardware, no waiting."""

    def __init__(self, path: str = "lyre_output.svg",
                 paper_mm: Tuple[float, float] = (297.0, 210.0),
                 flush_every: int = 25):
        self.path = path
        self.paper = paper_mm
        self.flush_every = flush_every
        self.strokes: list[list[tuple]] = []
        self.down = False
        self.pos: Optional[Tuple[float, float]] = None
        self._since_flush = 0

    def penup(self) -> None:
        self.down = False

    def pendown(self) -> None:
        self.down = True
        if self.pos is not None:
            self.strokes.append([self.pos])

    def goto(self, x: float, y: float) -> None:
        self.pos = (x, y)
        if self.down:
            if not self.strokes:
                self.strokes.append([])
            self.strokes[-1].append((x, y))
            self._since_flush += 1
            if self._since_flush >= self.flush_every:
                self.write()
                self._since_flush = 0

    def write(self) -> None:
        w, h = self.paper
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}mm" '
            f'height="{h}mm" viewBox="0 0 {w} {h}">',
            f'<rect width="{w}" height="{h}" fill="white"/>',
        ]
        for s in self.strokes:
            if len(s) < 2:
                continue
            d = "M" + " L".join(f"{px:.2f},{py:.2f}" for px, py in s)
            parts.append(f'<path d="{d}" fill="none" stroke="black" '
                         f'stroke-width="0.4" stroke-linecap="round"/>')
        parts.append("</svg>")
        with open(self.path, "w") as fh:
            fh.write("\n".join(parts))

    def disconnect(self) -> None:
        self.write()


class AxiDraw(Plotter):
    """pyaxidraw in interactive mode, talking to the EBB over USB serial."""

    def __init__(self, speed_pendown: int = 25, speed_penup: int = 75,
                 pen_pos_down: int = 40, pen_pos_up: int = 60,
                 port: Optional[str] = None, model: int = 1):
        self.ad = None
        self.opts = dict(speed_pendown=speed_pendown, speed_penup=speed_penup,
                         pen_pos_down=pen_pos_down, pen_pos_up=pen_pos_up,
                         port=port, model=model)
        self.down = False

    def connect(self) -> None:
        from pyaxidraw import axidraw

        self.ad = axidraw.AxiDraw()
        self.ad.interactive()
        o = self.ad.options
        o.units = 2  # 0 inch, 1 cm, 2 mm
        o.model = self.opts["model"]
        o.speed_pendown = self.opts["speed_pendown"]
        o.speed_penup = self.opts["speed_penup"]
        o.pen_pos_down = self.opts["pen_pos_down"]
        o.pen_pos_up = self.opts["pen_pos_up"]
        if self.opts["port"]:
            o.port = self.opts["port"]
        if not self.ad.connect():
            raise RuntimeError("no AxiDraw found; check the USB cable and that "
                               "nothing else holds the serial port")
        self.ad.update()
        self.ad.penup()
        self.down = False

    def penup(self) -> None:
        if self.down:
            self.ad.penup()
            self.down = False

    def pendown(self) -> None:
        if not self.down:
            self.ad.pendown()
            self.down = True

    def goto(self, x: float, y: float) -> None:
        # moveto/lineto are absolute; lineto draws, moveto travels pen-up.
        if self.down:
            self.ad.lineto(x, y)
        else:
            self.ad.moveto(x, y)

    def disconnect(self) -> None:
        if self.ad is None:
            return
        try:
            self.ad.penup()
            self.ad.moveto(0, 0)
        finally:
            self.ad.disconnect()
