"""Web front end for driving the AxiDraw from a browser.

Serves a single page where curves are designed with live preview, then posts
the finished path here to be plotted. The browser does all the maths so what
you see is literally what gets drawn: the path arrives as a list of points in
paper millimetres and nothing re-derives it on this end.

    ~/venv/bin/python server.py                  # binds the tailnet address
    ~/venv/bin/python server.py --host 0.0.0.0   # anyone on the local network

Plotting happens on a worker thread so the request returns immediately, and
there is a stop flag checked between every segment, because the thing you want
most from a half-hour plot going wrong is for it to stop now.
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import hashlib
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

from pen_box import MODELS  # noqa: E402  the machine travel envelopes
from portlock import hold  # noqa: E402  one thing at a time on a port

VERSION = "0.5.0"

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
app = Flask(__name__, static_folder=None)


class Job:
    """The one plot that may be running, and everything anyone can ask about it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.state = "idle"          # idle | running | done | stopped | error
        self.message = "nothing plotted yet"
        self.done = 0
        self.total = 0
        self.started = 0.0
        self.estimate_s = 0.0
        self.port = None

    def holds_port(self, device: str) -> bool:
        """True while a plot has this device open, so nothing probes it.

        Opening a board's serial port mid-plot to ask its name would fight the
        driver for the port. When the running job did not name a port, treat
        every board as busy: we cannot tell which one the driver picked.
        """
        if self.state != "running":
            return False
        return self.port is None or self.port == device

    def snapshot(self) -> dict:
        elapsed = time.time() - self.started if self.started else 0.0
        frac = (self.done / self.total) if self.total else 0.0
        remaining = (elapsed / frac - elapsed) if frac > 0.02 else None
        return {
            "state": self.state,
            "message": self.message,
            "done": self.done,
            "total": self.total,
            "elapsed_s": round(elapsed, 1),
            "remaining_s": round(remaining, 1) if remaining else None,
            "busy": self.state == "running",
        }


job = Job()


def _progress_by_time():
    """Advance the progress figure while draw_path has the machine.

    draw_path gives no per-segment callback, so this walks the counter
    forward against elapsed time instead. It is a readout, not a measurement,
    and it stops short of 99% so it never claims to be finished early.
    """
    while job.state == "running" and not job.stop.is_set():
        est = job.estimate_s or 0
        if est > 0 and job.total:
            frac = min(0.99, (time.time() - job.started) / est)
            job.done = max(job.done, int(frac * job.total))
        time.sleep(2)


def path_length(points) -> float:
    return sum(math.dist(points[i - 1], points[i]) for i in range(1, len(points)))


def check_claims(points, model: int) -> tuple[bool, list]:
    """State what makes this path plottable, and refuse rather than warn.

    The AxiDraw has no home switches and no soft limits of its own. Send it a
    point past the end of the rail and it will drive there, grind, lose steps,
    and report nothing. So the envelope is checked here, before the motors are
    enabled, not hoped for.
    """
    tx, ty, name = MODELS[model]
    out = []

    def req(label, cond, detail=""):
        out.append({"label": label, "ok": bool(cond), "detail": detail})
        return bool(cond)

    ok = True
    ok &= req("path has at least two points", len(points) >= 2, f"{len(points)} points")
    if len(points) < 2:
        return False, out

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    ok &= req("every point is a finite number",
              all(math.isfinite(v) for v in xs + ys))
    ok &= req("x stays within travel", min(xs) >= 0 and max(xs) <= tx,
              f"x spans {min(xs):.1f} to {max(xs):.1f} mm, machine has 0 to {tx:.0f}")
    ok &= req("y stays within travel", min(ys) >= 0 and max(ys) <= ty,
              f"y spans {min(ys):.1f} to {max(ys):.1f} mm, machine has 0 to {ty:.0f}")
    ok &= req("machine is one this code knows", model in MODELS, name)
    return bool(ok), out


EBB_VID_PID = (0x04D8, 0xFD92)


def list_boards():
    """Find every EiBotBoard attached, and ask each one its name.

    These boards report no USB serial number, and two of them produce an
    identical /dev/serial/by-id entry, so only one symlink survives. udev
    rules keyed on serial number cannot tell them apart. The EBB stores a
    nickname in its own EEPROM instead (ST sets it, QT reads it), which
    survives replugging and does not care which ttyACM it enumerated as.
    """
    import serial
    import serial.tools.list_ports as lp

    out = []
    for p in sorted(lp.comports(), key=lambda x: x.device):
        if (p.vid, p.pid) != EBB_VID_PID:
            continue
        nickname = ""
        busy = job.holds_port(p.device)
        # Probing opens the port, which kills whatever is drawing on it, so
        # only ask a board its name when nothing else has claimed it.
        if not busy:
            try:
                with hold(p.device) as got:
                    if not got:
                        raise RuntimeError("in use")
                    with serial.Serial(p.device, 9600, timeout=1.5) as sp:
                        time.sleep(0.15)
                        sp.reset_input_buffer()
                        sp.write(b"QT\r")
                        time.sleep(0.3)
                        reply = sp.read(100).decode(errors="replace").strip()
                        first = reply.splitlines()[0].strip() if reply else ""
                        # A board with no nickname answers with a bare OK.
                        nickname = "" if first in ("", "OK") else first
            except Exception:
                nickname = ""
                busy = True
        out.append({"device": p.device, "nickname": nickname,
                    "busy": busy,
                    "label": nickname or p.device})
    return out


# Calibrated against these boards today: a powered AxiDraw read 0303 counts on
# the V+ channel with a 9 V supply, an unpowered one read 0026. The board runs
# its logic off USB, so it answers every command and reports a finished plot
# with the motor supply off.
ADC_VOLTS_PER_COUNT = 0.0295
MIN_MOTOR_VOLTS = 5.0


def board_health(device):
    """Ask a board about its motor supply before trusting it with a plot."""
    import serial

    def ask(sp, cmd):
        sp.reset_input_buffer()
        sp.write((cmd + "\r").encode())
        time.sleep(0.35)
        return sp.read(150).decode(errors="replace").strip()

    with hold(device) as got:
        if not got:
            raise RuntimeError("another process is using this board")
        with serial.Serial(device, 9600, timeout=2) as sp:
            time.sleep(0.25)
            qc = ask(sp, "QC").splitlines()[0]
            qg = ask(sp, "QG").splitlines()[0]
            ver = ask(sp, "V").splitlines()[0]
    volts = int(qc.split(",")[1]) * ADC_VOLTS_PER_COUNT
    return {"volts": round(volts, 1), "qg": qg, "firmware": ver, "qc": qc}


def preflight(port):
    """Hardware claims, checked before the motors are asked to do anything.

    A whole afternoon went into a board that was in a fault state: it returned
    OK to every command, advanced its step counter, and finished a plot with
    timings that matched real motion exactly, while nothing turned. Geometry
    claims cannot see any of that. These ask the board itself.
    """
    out = []

    def req(label, cond, detail="", ok_detail=""):
        # The long explanation is why it failed, so only say it when it did.
        out.append({"label": label, "ok": bool(cond),
                    "detail": ok_detail if cond else detail})
        return bool(cond)

    boards = list_boards()
    if not boards:
        req("a plotter is attached", False, "no EiBotBoard found on USB")
        return False, out

    chosen = None
    if port:
        for b in boards:
            if port in (b["nickname"], b["device"]):
                chosen = b
                break
        if not req("the chosen plotter is attached", chosen is not None,
                   f"{port} is not among " +
                   ", ".join(b["label"] for b in boards),
                   ok_detail=port):
            return False, out
    else:
        if not req("only one plotter, so auto-select is safe", len(boards) == 1,
                   f"{len(boards)} boards attached, pick one by name"):
            return False, out
        chosen = boards[0]

    try:
        h = board_health(chosen["device"])
    except Exception as exc:
        req("the board answers", False, f"{type(exc).__name__}: {exc}")
        return False, out

    ok = req("the board answers", True, h["firmware"])
    ok &= req("motor power is present",
              h["volts"] >= MIN_MOTOR_VOLTS,
              f"only {h['volts']} V on the motor rail. The board runs its logic "
              f"off USB, so with the supply off it accepts a whole plot and "
              f"reports it finished while nothing moves. Check the barrel jack.",
              ok_detail=f"{h['volts']} V on the motor rail")
    out.append({"label": "board status byte", "ok": True,
                "detail": f"QG {h['qg']}, recorded not judged"})
    return bool(ok), out


def plot_worker(points, model, speed, pen_up, pen_down, preview, accel=75,
                port=None):
    from pyaxidraw import axidraw

    # Resolve a nickname to a device so the lock is on the same name a command
    # line tool would use, then hold it for the whole plot.
    device = port
    for b in list_boards():
        if port in (b["nickname"], b["device"]):
            device = b["device"]
            break

    ad = axidraw.AxiDraw()
    lock = hold(device) if device else None
    holder = lock.__enter__() if lock else True
    if not holder:
        job.state = "error"
        job.message = (f"{port} is in use by something else. A command line "
                       f"tool or another plot has it.")
        return
    try:
        ad.interactive()
        o = ad.options
        o.units = 2
        o.model = model
        o.speed_pendown = speed
        o.speed_penup = 75
        o.pen_pos_up = pen_up
        o.pen_pos_down = pen_down
        o.accel = accel

        if port:
            # port_config must be 1 or the port is ignored and the driver
            # autodetects anyway, which is what silently broke nicknames.
            o.port = port
            o.port_config = 1
        if not ad.connect():
            job.state, job.message = "error", (
                f"could not open {port}" if port else
                "no AxiDraw found. If more than one is attached, pick which "
                "board to use: with several plugged in the driver cannot choose.")
            return

        job.message = "pen up, moving to start"
        ad.penup()

        if preview:
            # A pen-up lap round the bounding box, so a misplaced sheet shows
            # itself before any ink lands.
            job.message = "pen-up preview lap"
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            box = [(min(xs), min(ys)), (max(xs), min(ys)),
                   (max(xs), max(ys)), (min(xs), max(ys)), (min(xs), min(ys))]
            for x, y in box:
                if job.stop.is_set():
                    break
                ad.moveto(x, y)

        if not job.stop.is_set():
            # One planned move for the whole path. Drawing it segment by
            # segment with lineto plans each move on its own, so the machine
            # accelerates from rest and stops again for every segment: a
            # 57,000 segment path stops 57,000 times, which is the jerkiness.
            #
            # draw_path raises the pen when it returns, so it cannot be split
            # into chunks to poll the stop flag without stamping a pen lift at
            # every boundary. The driver takes the Event itself instead.
            job.message = "drawing"
            ad.set_up_pause_receiver(job.stop)
            vertices = [[float(x), float(y)] for x, y in points]
            # No per-segment callback exists, so progress is reported against
            # the driver's own time estimate. It is an estimate, and says so.
            # ad.time_estimate is only populated after a plot, so it is
            # useless for driving progress during one. Use the same length
            # over speed figure that /api/check already reports.
            job.estimate_s = path_length(points) / (2.18 * max(1, speed))
            prog = threading.Thread(target=_progress_by_time, daemon=True)
            prog.start()
            ad.draw_path(vertices)
            job.done = job.total

        job.message = "returning home"
        ad.moveto(0, 0)
        ad.disconnect()

        if job.stop.is_set():
            job.state = "stopped"
            job.message = f"stopped by request after {job.done} of {job.total} segments"
        else:
            job.state = "done"
            job.message = f"finished, {job.total} segments in {time.time() - job.started:.0f}s"
    except Exception as exc:
        job.state, job.message = "error", f"{type(exc).__name__}: {exc}"
        try:
            ad.disconnect()
        except Exception:
            pass
    finally:
        if lock:
            lock.__exit__(None, None, None)


@app.get("/")
def index():
    return send_from_directory(DOCS, "index.html")


def page_stamp():
    """A short hash of the page as it is on disk right now.

    The server version only moves when server.py changes, so it cannot see a
    deploy that only touched the page. This can, which is what lets the browser
    notice it is running yesterday's javascript against today's server.
    """
    try:
        with open(os.path.join(DOCS, "index.html"), "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()[:8]
    except OSError:
        return "unknown"


@app.get("/api/info")
def info():
    return jsonify({
        "version": VERSION,
        "page": page_stamp(),
        "models": {k: {"x": v[0], "y": v[1], "name": v[2]} for k, v in MODELS.items()},
        "host": os.uname().nodename,
    })


@app.get("/api/boards")
def boards():
    return jsonify({"boards": list_boards()})


@app.get("/api/status")
def status():
    return jsonify(job.snapshot())


@app.post("/api/stop")
def stop():
    if job.state != "running":
        return jsonify({"ok": False, "message": "nothing is running"}), 409
    job.stop.set()
    return jsonify({"ok": True, "message": "stopping after the current segment"})


@app.post("/api/check")
def check():
    body = request.get_json(force=True)
    points = body.get("points") or []
    model = int(body.get("model", 2))
    ok, claims = check_claims(points, model)
    # Ask the hardware too, but only when nothing is using it.
    if job.state != "running":
        hw_ok, hw = preflight(body.get("port") or None)
        claims = claims + hw
        ok = ok and hw_ok
    length = path_length(points) if len(points) >= 2 else 0.0
    # Rough: AxiDraw full pen-down speed is about 218 mm/s, and the speed
    # option is a percentage of it. Acceleration and pen lifts are ignored, so
    # treat this as a lower bound rather than a promise.
    speed = max(1, int(body.get("speed", 25)))
    est = length / (2.18 * speed)
    return jsonify({
        "ok": ok,
        "claims": claims,
        "segments": max(0, len(points) - 1),
        "length_mm": round(length, 1),
        "estimate_s": round(est),
    })


@app.post("/api/plot")
def plot():
    with job.lock:
        if job.state == "running":
            return jsonify({"ok": False, "message": "a plot is already running"}), 409

        body = request.get_json(force=True)
        points = [(float(p[0]), float(p[1])) for p in (body.get("points") or [])]
        model = int(body.get("model", 2))

        ok, claims = check_claims(points, model)
        hw_ok, hw = preflight(body.get("port") or None)
        claims = claims + hw
        ok = ok and hw_ok
        if not ok:
            failed = [c for c in claims if not c["ok"]]
            return jsonify({"ok": False, "claims": claims,
                            "message": "refusing to move: " +
                            "; ".join(f"{c['label']} ({c['detail']})" if c["detail"]
                                      else c["label"] for c in failed)}), 400

        job.stop.clear()
        job.state = "running"
        job.message = "starting"
        job.done = 0
        job.total = len(points) - 1
        job.started = time.time()
        job.port = body.get("port") or None
        job.thread = threading.Thread(
            target=plot_worker,
            args=(points, model, int(body.get("speed", 25)),
                  int(body.get("pen_up", 60)), int(body.get("pen_down", 0)),
                  bool(body.get("preview", True)), int(body.get("accel", 75)),
                  body.get("port") or None),
            daemon=True)
        job.thread.start()
    return jsonify({"ok": True, "message": "plotting", "segments": job.total})


def default_host() -> str:
    """Bind the tailnet address when there is one.

    Binding 0.0.0.0 on campus wireless puts a plotter control panel in front of
    everyone on the subnet. The tailnet is already the way in, so default to it
    and make the wider bind an explicit choice.
    """
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=5)
        addr = out.stdout.strip().splitlines()[0]
        if addr:
            return addr
    except Exception:
        pass
    return "127.0.0.1"


def main() -> int:
    p = argparse.ArgumentParser(description="browser front end for the AxiDraw")
    p.add_argument("--host", default=None,
                   help="bind address, default is the tailnet address")
    p.add_argument("--port", type=int, default=8080)
    args = p.parse_args()

    host = args.host or default_host()
    print(f"=== piplot v{VERSION} ===")
    print("Design curves in a browser, preview them, send them to the AxiDraw")
    print("https://github.com/sui001/piplot")
    print()
    print(f"serving on http://{host}:{args.port}")
    if host not in ("0.0.0.0",):
        print("(bound to the tailnet only, pass --host 0.0.0.0 to open it up)")
    app.run(host=host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
