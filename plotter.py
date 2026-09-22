"""Pen backends.

The AxiDraw is slow and the room is fast, so the plotter is never asked to
keep up. It is handed one segment at a time and draws whatever has piled up,
which is why the drawing lags and sediments rather than tracking live.

DryRun draws to an SVG file instead, at full speed, so the whole chain can be
run and looked at without the machine.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple


def merge_paths(paths, tol: float = 0.05) -> list:
    """Chain paths that meet end to end, so the pen lifts once instead of twice.

    Sorting strokes cuts the travel between them but not the number of lifts,
    and on the GRBL machine a lift is the expensive part: a Z move plus a
    dwell each way, about 0.8 s, paid per path however short the path is.
    Measured on the speed ladder, lifts were roughly a third of the run.

    Seven segment digits are the worst case. `label()` emits every stroke on
    its own, so an 8 is seven paths and seven lifts to put down about 20 mm of
    ink. Chained, it is one or two.

    Greedy: take the next path, then keep extending it with any path whose
    start, or whose end reversed, lands within tol of the current end. tol is
    mm and should be smaller than a gap anyone would see. Reversing is safe
    because a stroke drawn backwards leaves the same mark.

    The ends live in a grid rather than being scanned for. This was written
    against seven segment digits, where scanning a list of tens of paths costs
    nothing, and then pointed at an imported A0 plan of 128,757 strokes, where
    scanning is around sixteen billion comparisons and simply never returns.
    Same greedy rule, same output, one pass.
    """
    live = [list(p) for p in paths if p and len(p) >= 2]
    n = len(live)
    if n < 2:                       # two paths can still chain into one
        return live
    cell = max(tol * 2, 1e-9)
    ends: dict = {}
    for i, p in enumerate(live):
        for w, q in ((0, p[0]), (1, p[-1])):
            ends.setdefault((int(q[0] // cell), int(q[1] // cell)), []).append((i, w))
    used = bytearray(n)

    def find(pt):
        ci, cj = int(pt[0] // cell), int(pt[1] // cell)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for i, w in ends.get((ci + di, cj + dj), ()):
                    if used[i]:
                        continue
                    q = live[i][0] if w == 0 else live[i][-1]
                    if abs(q[0] - pt[0]) <= tol and abs(q[1] - pt[1]) <= tol:
                        return i, w
        return None

    out = []
    for s in range(n):
        if used[s]:
            continue
        used[s] = 1
        cur = live[s]
        while True:
            hit = find(cur[-1])
            if hit is None:
                break
            i, w = hit
            used[i] = 1
            cur.extend(live[i][1:] if w == 0 else list(reversed(live[i]))[1:])
        out.append(cur)
    return out


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

    def draw_path(self, points, stop_event=None) -> bool:
        """Draw a whole path in one planned move. False if unsupported.

        Segment-at-a-time drawing is what makes a plot jerky: each move is
        planned on its own, so the machine accelerates from rest and stops
        again for every segment. A path with 57,000 of them stops 57,000
        times. Handing the whole list over lets the planner carry speed
        through the corners.
        """
        return False

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
                 pen_pos_down: int = 0, pen_pos_up: int = 60,
                 port: Optional[str] = None, model: int = 1,
                 accel: int = 75, const_speed: bool = False):
        self.ad = None
        self.opts = dict(speed_pendown=speed_pendown, speed_penup=speed_penup,
                         pen_pos_down=pen_pos_down, pen_pos_up=pen_pos_up,
                         port=port, model=model, accel=accel,
                         const_speed=const_speed)
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
        o.accel = self.opts["accel"]
        o.const_speed = self.opts["const_speed"]
        if self.opts["port"]:
            # 0, not 1: in the driver port_config 1 means "ignore the name and
            # use the first AxiDraw found", which sent plots to the wrong board.
            o.port = self.opts["port"]
            o.port_config = 0
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

    def draw_path(self, points, stop_event=None) -> bool:
        """One planned move for the whole path, so the motors do not stutter.

        draw_path raises the pen when it finishes, so this cannot be chunked
        to poll a stop flag: every chunk boundary would stamp a pen lift on
        the paper. Instead the driver takes the threading.Event directly and
        checks it itself, which keeps the stop button working without
        breaking the path into pieces.
        """
        if stop_event is not None:
            self.ad.set_up_pause_receiver(stop_event)
        self.ad.draw_path([[float(x), float(y)] for x, y in points])
        self.down = False   # draw_path always leaves the pen raised
        return True

    def disconnect(self) -> None:
        if self.ad is None:
            return
        try:
            self.ad.penup()
            self.ad.moveto(0, 0)
        finally:
            self.ad.disconnect()


class Grbl(Plotter):
    """A GRBL 1.1 board over serial, with the pen servo on the Z axis.

    Written against the homemade CoreXY machine on lyre, whose particulars are
    worth stating because none of them are guessable:

    - **The pen is on Z, not the spindle.** Its build reports `[OPT:C,15,128]`
      with no `V`, so VARIABLE_SPINDLE is not compiled in, the spindle pin is a
      plain digital output, and `M3 S100` and `M3 S1000` are the same
      instruction. Two sessions went into sending spindle commands that the
      board cheerfully answered `ok` to while nothing moved.
    - **The servo is binary.** Every Z above zero gives the same lift, so
      `pen_up_z` is chosen for speed, not height. Z is a real timed move at
      `$112` mm/min, so Z1 costs about 0.12 s and Z15 costs 1.8 s. Across a
      few thousand strokes that is the difference between a plot and an
      afternoon.
    - **There is no homing and there are no limits** (`$20=$21=$22=0`), and
      `$130/$131` are left at 5000, so the firmware believes the machine is
      five metres wide. Nothing but this class will stop a path driving the
      carriage into a rail end, which is why `travel` is checked here and not
      merely hoped for.
    - **y runs up the page.** Origin is the bottom left corner with +Y away
      from the operator, while piplot's paper frame is y down the page. So the
      mapping is flipped here, once, rather than at every call site.

    **There is no travel speed setting here, and that is deliberate.** Pen-up
    moves are G0 rapids, and a G0 ignores F entirely: it runs at the board's
    own `$110/$111`. So the two speeds are already separate levers, and they
    want different values, because only pen-down moves have to look good.
    Laddered on paper on 19 Sep, drawing went bad somewhere between 5000 and
    8000 mm/min, but travel has no such limit. The machine is set to
    `$110/$111 = 11000` for travel with `feed = 5000` for ink.
    """

    # $I reports the real figure as the third field of OPT. Undersizing it only
    # costs throughput; oversizing it overruns the board and corrupts commands.
    RX_BUFFER = 128

    def __init__(self, port: str = "/dev/ttyUSB0", baud: int = 115200,
                 travel: Tuple[float, float] = (420.0, 297.0),
                 pen_up_z: float = 1.0, pen_down_z: float = 0.0,
                 pen_dwell_s: float = 0.25, feed: int = 5000,
                 flip_y: bool = True):
        self.sp = None
        self.port = port
        self.baud = baud
        self.travel = travel
        self.pen_up_z = pen_up_z
        self.pen_down_z = pen_down_z
        self.pen_dwell_s = pen_dwell_s
        self.feed = feed
        self.flip_y = flip_y
        self.down = False

    # ---- coordinates -------------------------------------------------------

    def _xy(self, x: float, y: float) -> Tuple[float, float]:
        """Paper mm, y down, into machine mm, y up."""
        return (x, self.travel[1] - y) if self.flip_y else (x, y)

    def check(self, points) -> list:
        """Reasons this path must not be sent, as plain sentences.

        Empty means it is safe. The board has no soft limits of its own, so a
        point past the end of a rail is not refused anywhere else: the carriage
        simply drives there, the belt skips, and every coordinate after it is
        wrong with nothing reported.
        """
        if not points:
            return ["the path has no points"]
        tx, ty = self.travel
        bad = []
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        if min(xs) < 0 or max(xs) > tx:
            bad.append(f"x spans {min(xs):.1f} to {max(xs):.1f} mm, "
                       f"machine has 0 to {tx:.0f}")
        if min(ys) < 0 or max(ys) > ty:
            bad.append(f"y spans {min(ys):.1f} to {max(ys):.1f} mm, "
                       f"machine has 0 to {ty:.0f}")
        return bad

    # ---- the wire ----------------------------------------------------------

    def _send(self, line: str, wait: float = 0.05) -> str:
        self.sp.write((line + "\n").encode())
        time.sleep(wait)
        return self.sp.read(self.sp.in_waiting or 1).decode(errors="replace").strip()

    def _state(self) -> str:
        """The one word inside GRBL's status report: Idle, Run, Hold, Alarm."""
        self.sp.reset_input_buffer()
        self.sp.write(b"?")
        time.sleep(0.15)
        reply = self.sp.read(self.sp.in_waiting or 1).decode(errors="replace")
        if "<" not in reply:
            return "?"
        return reply.split("<", 1)[1].split("|", 1)[0].split(">", 1)[0].strip()

    def _wait_idle(self, timeout: float = 600.0) -> bool:
        """Block until the machine stops moving, or raise.

        It raises rather than returning False because both callers used to
        ignore the result and carry on. `disconnect()` would then close the
        port while the carriage was still moving, and closing the port is a
        DTR reset: the move is abandoned and the position is lost, on a
        machine with no homing to recover it. Timing out is not a thing to
        shrug at here.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self._state()
            if state.startswith("Idle"):
                return True
            if state.startswith("Alarm"):
                raise RuntimeError("the board is in Alarm, so the position is "
                                   "no longer trustworthy; re-park the carriage")
            time.sleep(0.2)
        raise TimeoutError(f"the machine was still {self._state()} after "
                           f"{timeout:.0f}s; not closing the port on a moving "
                           f"machine, since that would lose its position")

    def connect(self) -> None:
        import serial

        self.sp = serial.Serial(self.port, self.baud, timeout=2)
        time.sleep(2.0)          # the CH340 asserts DTR, which resets the board
        self.sp.reset_input_buffer()
        self._send("", 0.3)      # shake off any half command left by a replug

        self._send("G21", 0.2)   # millimetres
        self._send("G90", 0.2)   # absolute
        self._send("G94", 0.2)   # feed is units per minute

        # Note what connect() deliberately does NOT do: set the origin. It used
        # to, on every connect, which is right only when the carriage happens
        # to be parked. After a stop, an error or a server restart it is
        # somewhere arbitrary, and zeroing there quietly writes a false origin
        # to EEPROM, so every later envelope check passes against a frame that
        # no longer matches the rails. Nothing on this machine would report it.
        # Setting the origin is now an explicit act: see set_origin_here.
        self.penup()

    def set_origin_here(self) -> None:
        """Call the carriage's current position (0, 0). Only when it is parked.

        The caller must have confirmed with a human that the carriage really
        is in the home corner. There are no homing switches, so this is a
        promise the software cannot check, which is why it is a separate
        method that has to be asked for rather than something connect() does
        on your behalf.

        G10 L20 rather than G92 deliberately: G92 offsets are wiped by the
        soft reset that stopping needs, while G10 L20 writes the work offset
        to EEPROM and survives it.
        """
        self._send("G10 L20 P0 X0 Y0 Z0", 0.3)

    # ---- the pen -----------------------------------------------------------

    def _pen(self, z: float) -> None:
        # The dwell is not superstition. GRBL's move can complete before the
        # servo has physically arrived, and the next move then starts with the
        # nib halfway down, which drags a tail into the start of the stroke.
        self._send(f"G0 Z{z:g}", 0.05)
        self._send(f"G4 P{self.pen_dwell_s:g}", 0.05)

    def penup(self) -> None:
        # No "already up" guard, unlike pendown. Lifting costs 0.12 s and a
        # redundant lift is harmless, while believing the pen is up when it is
        # not draws a line across the sheet on the next travel move.
        self._pen(self.pen_up_z)
        self.down = False

    def pendown(self) -> None:
        if self.down:
            return
        self._pen(self.pen_down_z)
        self.down = True

    def goto(self, x: float, y: float) -> None:
        mx, my = self._xy(x, y)
        if self.down:
            self._send(f"G1 X{mx:.3f} Y{my:.3f} F{self.feed}", 0.01)
        else:
            self._send(f"G0 X{mx:.3f} Y{my:.3f}", 0.01)

    # ---- streaming ---------------------------------------------------------

    def _stream(self, lines, stop_event=None) -> bool:
        """Character counting, because waiting for `ok` per line would stutter.

        Sending a line and waiting for its acknowledgement leaves the planner
        empty between every move, so the machine decelerates to a stop at each
        one. That is the same jerkiness `draw_path` exists to avoid on the
        AxiDraw. Instead, keep as many bytes in flight as the board's receive
        buffer will hold and let its fifteen block planner carry speed through
        the corners.

        Returns False if it was stopped part way.
        """
        pending: list[int] = []
        i = 0
        # A board that stops answering must not be waited on forever. The loop
        # below retries on an empty read, so without a deadline a wedged board
        # holds the flock for good and nothing else can ever use the machine.
        # The clock restarts on every acknowledgement, so a long slow path is
        # fine and only real silence trips it.
        quiet_for = 120.0
        last_progress = time.time()
        while i < len(lines) or pending:
            if stop_event is not None and stop_event.is_set():
                return False
            while i < len(lines):
                n = len(lines[i]) + 1
                # Always let one line through, or a line longer than the buffer
                # would wedge the loop against a condition it can never meet.
                if pending and sum(pending) + n >= self.RX_BUFFER:
                    break
                self.sp.write((lines[i] + "\n").encode())
                pending.append(n)
                i += 1
            reply = self.sp.readline().decode(errors="replace").strip()
            if not reply:
                if time.time() - last_progress > quiet_for:
                    raise TimeoutError(
                        f"the board said nothing for {quiet_for:.0f}s with "
                        f"{len(pending)} moves outstanding")
                continue
            last_progress = time.time()
            if reply.startswith("error") or reply.startswith("ALARM"):
                raise RuntimeError(f"grbl rejected a command: {reply}")
            # Push messages and status reports are not acknowledgements, so
            # only `ok` retires a line. Counting them would let the byte total
            # drift low and eventually overrun the board.
            if reply == "ok":
                pending.pop(0)
        return True

    def encode(self, points) -> list:
        """The path as the shortest G-code that means the same thing.

        This is a speed control, not tidiness. GRBL's planner decides how fast
        it dares go from the moves it can already see, and it can only see what
        fits in a 128 byte receive buffer. At `G1 X123.456 Y789.012 F3000`, 26
        bytes a move, that is four moves of lookahead, so the planner is always
        planning to stop within about 2 mm and never commits to speed. Measured
        on the CoreXY machine: 1000, 2500 and 5000 mm/min gave 6.6, 9.4 and
        10.1 mm/s actual. Asking for five times the feed bought half again the
        speed, because feed was never the thing in the way.

        Three savings, none of which lose anything:

        - Modal codes. G1 persists, so only the first move needs it.
        - No spaces, and F only when it changes.
        - Two decimals. At 101 steps/mm one step is 0.0099 mm, so 0.01 mm is
          the machine's own resolution. The third decimal was never real.

        `X123.46Y789.01` is 14 bytes, which is nine moves of lookahead instead
        of four. A point that rounds onto its predecessor is dropped, since a
        move to where the head already is still costs a planner block.
        """
        fx, fy = self._xy(*points[0])
        out = [f"G0Z{self.pen_up_z:g}",
               f"G0X{fx:.2f}Y{fy:.2f}",
               f"G4P{self.pen_dwell_s:g}",
               f"G0Z{self.pen_down_z:g}",
               f"G4P{self.pen_dwell_s:g}"]
        lsx, lsy = f"{fx:.2f}", f"{fy:.2f}"
        need_code = True
        for x, y in points[1:]:
            mx, my = self._xy(x, y)
            sx, sy = f"{mx:.2f}", f"{my:.2f}"
            parts = []
            if sx != lsx:
                parts.append("X" + sx)
            if sy != lsy:
                parts.append("Y" + sy)
            if not parts:
                continue
            if need_code:
                parts = ["G1"] + parts + [f"F{self.feed}"]
                need_code = False
            out.append("".join(parts))
            lsx, lsy = sx, sy
        out.append(f"G0Z{self.pen_up_z:g}")
        return out

    def draw_path(self, points, stop_event=None) -> bool:
        bad = self.check(points)
        if bad:
            raise ValueError("refusing to move: " + "; ".join(bad))

        finished = self._stream(self.encode(points), stop_event)
        if not finished:
            self._halt()
        else:
            self._wait_idle()
        self.down = False
        return finished

    def _halt(self) -> None:
        """Stop now, and still know where the carriage is afterwards.

        A soft reset alone would flush the planner but lose position, and with
        no homing switches the next plot would take wherever it stopped as its
        origin. So hold first and let the machine come to rest, which keeps
        position valid across the reset, then clear the queued moves.
        """
        self.sp.write(b"!")
        deadline = time.time() + 10
        while time.time() < deadline and not self._state().startswith("Hold"):
            time.sleep(0.1)
        self.sp.write(b"\x18")
        time.sleep(2.0)
        self.sp.reset_input_buffer()
        self._send("G21", 0.2)
        self._send("G90", 0.2)
        self._send("G94", 0.2)
        self._pen(self.pen_up_z)

    def disconnect(self) -> None:
        if self.sp is None:
            return
        try:
            self._pen(self.pen_up_z)
            self._send("G0 X0 Y0", 0.05)
            self._wait_idle(timeout=120)
        finally:
            self.sp.close()
            self.sp = None


class _Socket:
    """A TCP socket wearing pyserial's clothes.

    FluidNC's telnet server on port 23 speaks the same GRBL protocol as its
    USB port, byte for byte. So rather than teach `Grbl` about networks, this
    exposes the handful of members `Grbl` actually touches (`write`, `read`,
    `readline`, `in_waiting`, `reset_input_buffer`, `close`) and everything
    above it carries on unchanged.

    The important difference from a serial port is what OPENING one does.
    Opening the CH340 asserts DTR and resets the board, which is why
    `machines.py` goes to such lengths to identify boards from udev symlinks
    without ever opening them. Opening a socket does nothing to the machine.
    On a polargraph, which cannot home and so can never recover a lost
    position, that is not a convenience. It is the difference between a
    machine you can safely ask a question of and one you cannot.
    """

    def __init__(self, host: str, port: int = 23, timeout: float = 2.0):
        import socket

        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._timeout = timeout
        self._sock.settimeout(timeout)
        self._buf = bytearray()
        self.host, self.port = host, port

    def _fill(self, block: bool) -> None:
        import socket

        try:
            self._sock.settimeout(self._timeout if block else 0.0)
            chunk = self._sock.recv(4096)
            if chunk:
                self._buf.extend(chunk)
        except (socket.timeout, BlockingIOError):
            pass

    @property
    def in_waiting(self) -> int:
        self._fill(block=False)
        return len(self._buf)

    def write(self, data: bytes) -> int:
        self._sock.sendall(data)
        return len(data)

    def read(self, n: int = 1) -> bytes:
        while len(self._buf) < n:
            before = len(self._buf)
            self._fill(block=True)
            if len(self._buf) == before:
                break            # timed out, hand back whatever there is
        out, self._buf = bytes(self._buf[:n]), self._buf[n:]
        return out

    def readline(self) -> bytes:
        while b"\n" not in self._buf:
            before = len(self._buf)
            self._fill(block=True)
            if len(self._buf) == before:
                return b""       # the caller's quiet timer decides what next
        i = self._buf.index(b"\n") + 1
        out, self._buf = bytes(self._buf[:i]), self._buf[i:]
        return out

    def reset_input_buffer(self) -> None:
        self._fill(block=False)
        self._buf.clear()

    def close(self) -> None:
        self._sock.close()


class Polargraph(Grbl):
    """A wall hanging plotter on FluidNC, over WiFi or USB.

    FluidNC speaks GRBL 1.1 on the wire, so most of `Grbl` applies unchanged:
    the character counting streamer, the `?` status poll, the hold-then-reset
    halt, the modal G-code economy. What differs is everything that follows
    from this machine not being cartesian and not being able to home.

    **The board does the kinematics, not this class.** FluidNC's WallPlotter
    converts paper coordinates to cord lengths on the ESP32 and, crucially,
    cuts each move into `segment_length` pieces first, because a straight
    line on the paper is not a straight line in cord space. Unsegmented, a
    line across the bench board bows 82 mm. So this class sends ordinary
    cartesian G-code and does NOT pre-segment. Doing it in both places would
    multiply the byte count for no gain, and bytes are the scarce thing here:
    see `Grbl.encode`, where lookahead depth is set purely by how many moves
    fit in the board's receive buffer.

    `polargraph.max_segment_length` is what decides the board's number. There
    is no way to read it back over the wire, so the config file and the
    claims on it are the only thing keeping the two in step. That is the one
    silent failure left in this machine, and it is named here rather than
    papered over: a board flashed with too coarse a segment_length will draw
    bowed lines and report nothing wrong.

    **It cannot home, so a lost position is lost.** See `set_origin_here`,
    which refuses and says why.

    **Its limits are not a rectangle.** A polargraph has dead corners where
    the cords go near horizontal, so the envelope check is delegated to
    `polargraph.Polargraph.check`, which knows about cord angles. A paper
    rectangle alone would pass points the machine draws badly or cannot
    reach at all.
    """

    def __init__(self, geom, host: Optional[str] = None, port: int = 23,
                 serial_port: Optional[str] = None, baud: int = 115200,
                 home: Optional[Tuple[float, float]] = None,
                 pen_up_z: float = 5.0, pen_down_z: float = 0.0,
                 pen_dwell_s: float = 0.25, feed: int = 3000,
                 rx_buffer: int = 128):
        """`geom` is a `polargraph.Polargraph`. Give `host` OR `serial_port`.

        `home` is the paper point the gondola sits on at power on, in paper
        mm. It defaults to the centre of the sheet, which is what
        `polargraph.fluidnc_frame` assumes and what the config tells the
        operator to mark with a cross. Park somewhere else and you must say so
        here AND regenerate the config, or the two disagree with no symptom
        beyond a drawing in the wrong place.
        """
        if (host is None) == (serial_port is None):
            raise ValueError(
                "give exactly one of host (WiFi) or serial_port (USB), so it "
                "is never ambiguous which machine this is talking to"
            )
        super().__init__(port=serial_port or "", baud=baud,
                         travel=geom.travel, pen_up_z=pen_up_z,
                         pen_down_z=pen_down_z, pen_dwell_s=pen_dwell_s,
                         feed=feed, flip_y=False)
        self.geom = geom
        self.host, self.net_port = host, port
        self.home = home if home is not None else (geom.travel[0] / 2.0,
                                                   geom.travel[1] / 2.0)
        self.RX_BUFFER = rx_buffer

    # ---- coordinates -------------------------------------------------------

    def _xy(self, x: float, y: float) -> Tuple[float, float]:
        """Paper mm, y down from the top left, into FluidNC's frame.

        FluidNC's y runs UP, and its origin is wherever the gondola was parked
        at power on, because `WallPlotter::init()` derives its zero cord
        lengths from cartesian (0, 0) at boot. So the mapping is a translation
        onto the park point plus a y flip, not `Grbl`'s flip about the travel
        height. `flip_y` is forced off in the constructor for that reason.
        """
        return (x - self.home[0], self.home[1] - y)

    def check(self, points) -> list:
        """Delegated to the geometry, which knows this is not a rectangle."""
        return self.geom.check(points)

    # ---- connecting --------------------------------------------------------

    def connect(self) -> None:
        if self.host is not None:
            self.sp = _Socket(self.host, self.net_port)
            # No settling sleep and no flush of a reset banner, because
            # opening a socket does not reset the board. That is the whole
            # argument for preferring WiFi on a machine that cannot home.
        else:
            import serial

            # DTR and RTS low BEFORE the port opens. A devkit's auto-reset
            # circuit turns those lines into EN and BOOT, so pyserial's default
            # open resets the board, and on this machine a reset re-derives the
            # zero from wherever the gondola happens to hang. Learned on the
            # S3 on 21 Sep, where every probe knocked the firmware off. Whether
            # a given OS still pulses DTR during open is checked on the
            # target, not assumed: see test_polargraph_hw.py.
            self.sp = serial.Serial()
            self.sp.port, self.sp.baudrate, self.sp.timeout = self.port, self.baud, 2
            self.sp.dtr = False
            self.sp.rts = False
            self.sp.open()

            # Listen before speaking. Anything the board says unprompted in
            # the first moments is a boot banner, which means opening the port
            # reset it after all, and its zero is now wherever the gondola
            # hangs. That is fine only if it happened to be parked, which
            # nothing here can know, so say so rather than draw.
            time.sleep(1.5)
            unprompted = self.sp.read(self.sp.in_waiting or 0).decode(
                errors="replace")
            if any(k in unprompted for k in ("FluidNC v", "ets ", "rst:")):
                self.sp.close()
                self.sp = None
                raise RuntimeError(
                    "opening the port reset the board, so its zero is now "
                    "wherever the gondola was hanging. Park it on the centre "
                    "cross, power cycle the board, and retry. If this happens "
                    "on every connect, fit a 10 uF capacitor from EN to GND "
                    "on the devkit to disable its auto-reset")
            self._send("", 0.3)  # shake off any half command left by a replug

        ident = self._send("$I", 0.4)
        if "FluidNC" not in ident:
            self.disconnect()
            raise RuntimeError(
                f"this board is not running FluidNC: $I said {ident!r}. A "
                "plain GRBL board has no WallPlotter kinematics, so it would "
                "take the cord lengths as X and Y and draw something that is "
                "not your path, reporting nothing"
            )

        self._send("G21", 0.2)   # millimetres
        self._send("G90", 0.2)   # absolute
        self._send("G94", 0.2)   # feed is units per minute

        # Deliberately NOT setting the origin, same as Grbl.connect and for a
        # stronger reason here: on this machine it cannot be set at all.
        self._at = self._read_at()
        self.penup()

    # ---- never send a move that goes nowhere --------------------------------
    #
    # FluidNC 4.1.0's WallPlotter has a bug in cartesian_to_motors: when a
    # move's total distance is exactly zero it calls mc_move_motors(target)
    # with the CARTESIAN target, skipping the conversion to cord lengths. The
    # motors then go to cord offsets numerically equal to the paper
    # coordinates. Found 21 Sep on the bench: a redundant "G0 Z5" (Z already
    # 5) at (12, 7) threw the gondola to (-26.942, 2.926), which the bug
    # predicts to three decimals. At the park point the two frames coincide,
    # which is why nothing showed until then; near a corner the same bug
    # yanks the gondola more than a metre.
    #
    # So this class tracks where FluidNC believes it is and drops any move
    # within MOVE_EPS of it on every axis. The tolerance, not exact equality,
    # matters: the start position comes from MPos, which is forward
    # kinematics of whole steps (11.999 for a commanded 12), so an exact
    # compare would let a true zero-distance move through.
    MOVE_EPS = 0.05

    def _read_at(self):
        """(x, y, z) as FluidNC reports it, in its own frame."""
        import re

        self.sp.reset_input_buffer()
        self.sp.write(b"?")
        time.sleep(0.3)
        reply = self.sp.read(self.sp.in_waiting or 1).decode(errors="replace")
        m = re.search(r"MPos:([-\d.]+),([-\d.]+),([-\d.]+)", reply)
        if not m:
            raise RuntimeError(f"no position in the status reply {reply!r}, "
                               "so a move to nowhere cannot be ruled out")
        return tuple(float(v) for v in m.groups())

    def _move(self, code: str, x=None, y=None, z=None, feed=None):
        """One G0/G1 line, or None if it would go nowhere. Updates _at.

        Axes left as None are unchanged. `_at` None means not connected yet
        (the offline claims), where every line is emitted as asked.
        """
        at = getattr(self, "_at", None)
        tx = at[0] if (x is None and at) else x
        ty = at[1] if (y is None and at) else y
        tz = at[2] if (z is None and at) else z
        if at is not None and all(
                v is None or abs(v - a) < self.MOVE_EPS
                for v, a in zip((tx, ty, tz), at)):
            return None
        parts = [code]
        if x is not None:
            parts.append(f"X{x:.2f}")
        if y is not None:
            parts.append(f"Y{y:.2f}")
        if z is not None:
            parts.append(f"Z{z:g}")
        if feed is not None:
            parts.append(f"F{feed}")
        if at is not None:
            self._at = (tx, ty, tz)
        return "".join(parts)

    def _pen(self, z: float) -> None:
        line = self._move("G0", z=z)
        if line:
            self._send(line, 0.05)
            self._send(f"G4 P{self.pen_dwell_s:g}", 0.05)

    def penup(self) -> None:
        self._pen(self.pen_up_z)
        self.down = False

    def pendown(self) -> None:
        self._pen(self.pen_down_z)
        self.down = True

    def goto(self, x: float, y: float) -> None:
        mx, my = self._xy(x, y)
        line = (self._move("G1", mx, my, feed=self.feed) if self.down
                else self._move("G0", mx, my))
        if line:
            self._send(line, 0.01)

    def encode(self, points) -> list:
        """Grbl.encode's economy, with every move to nowhere dropped.

        Lift if not already up, travel if not already there, drop, draw,
        lift. Each G1 line carries only the axes that changed; the first one
        after the pen drop restates G1 and the feed, since the G0 lift and
        drop left the modal state at G0.
        """
        out = []
        up = self._move("G0", z=self.pen_up_z)
        if up:
            out.append(up)
        fx, fy = self._xy(*points[0])
        travel = self._move("G0", fx, fy)
        if travel:
            out.append(travel)
        out.append(f"G4P{self.pen_dwell_s:g}")
        down = self._move("G0", z=self.pen_down_z)
        if down:
            out.append(down)
            out.append(f"G4P{self.pen_dwell_s:g}")
        first = True
        last = (f"{fx:.2f}", f"{fy:.2f}")     # tracked here, so the economy
        for x, y in points[1:]:               # holds offline too
            mx, my = self._xy(x, y)
            line = self._move("G1", mx, my, feed=self.feed if first else None)
            if line is None:
                continue
            sx, sy = f"{mx:.2f}", f"{my:.2f}"
            # Only the axes that changed at two decimals, for lookahead's
            # sake. The first line restates G1 and the feed, since the pen
            # drop left the modal state at G0.
            axes = ((f"X{sx}" if sx != last[0] else "")
                    + (f"Y{sy}" if sy != last[1] else "")) or f"X{sx}"
            line = f"G1{axes}F{self.feed}" if first else axes
            out.append(line)
            last = (sx, sy)
            first = False
        lift = self._move("G0", z=self.pen_up_z)
        if lift:
            out.append(lift)
        return out

    def set_origin_here(self) -> None:
        """Refused, always. A polargraph's origin is set by rebooting it.

        `Grbl.set_origin_here` writes a work offset with `G10 L20`, which is
        the right answer on the CoreXY machine: shift the frame so the
        carriage's current position reads as zero.

        It is the WRONG answer here, and quietly so. FluidNC's WallPlotter
        computes `zero_left` and `zero_right` once, in `init()` at boot, from
        wherever cartesian (0, 0) was at that moment. A work offset moves the
        coordinate labels without re-deriving those cord lengths, so the
        machine would report exactly the numbers you asked for while driving
        the cords from a stale reference. Nothing anywhere would report it.

        The only way to re-zero is to park the gondola on its cross by hand
        and power cycle the board.
        """
        raise NotImplementedError(
            "a polargraph's origin cannot be set over the wire. Park the "
            "gondola on its centre cross and reboot the board: FluidNC "
            "derives its zero cord lengths at boot, and a G10 work offset "
            "would shift the labels without shifting the kinematics"
        )

    def disconnect(self) -> None:
        """Lift, go back to the park point, and only then let go.

        Mechanically the same as `Grbl.disconnect`, but for a sharper reason.
        Returning to (0, 0) puts the gondola back on its cross, so the next
        power on re-derives the same zero cord lengths and the machine wakes
        up still calibrated. Close the connection anywhere else and somebody
        has to re-park it by hand before it can draw again.

        Not super().disconnect(): that sends "G0 X0 Y0" unconditionally, and
        if the gondola is already home that is a move to nowhere, which is
        exactly what FluidNC's WallPlotter mishandles. Guarded here.
        """
        if self.sp is None:
            return
        try:
            self.penup()
            home = self._move("G0", 0.0, 0.0)
            if home:
                self._send(home, 0.05)
            self._wait_idle(timeout=120)
        finally:
            self.sp.close()
            self.sp = None
