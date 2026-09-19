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
import hashlib
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

from pen_box import MODELS  # noqa: E402  the machine travel envelopes
from portlock import hold  # noqa: E402  one thing at a time on a port

VERSION = "0.6.1"

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
app = Flask(__name__, static_folder=None)


class Job:
    """One board's plot, and everything anyone can ask about it.

    There is one of these per board rather than one for the server, so two
    machines can draw at once. Each has its own stop flag, because stopping
    the A3 must never stop the A1 beside it.
    """

    def __init__(self, device: str, label: str) -> None:
        self.device = device
        self.label = label
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.state = "idle"          # idle | running | done | stopped | error
        self.message = "nothing plotted yet"
        self.done = 0
        self.total = 0
        self.started = 0.0
        self.estimate_s = 0.0

    @property
    def busy(self) -> bool:
        return self.state == "running"

    def snapshot(self) -> dict:
        elapsed = time.time() - self.started if self.started else 0.0
        frac = (self.done / self.total) if self.total else 0.0
        remaining = (elapsed / frac - elapsed) if frac > 0.02 else None
        return {
            "board": self.label,
            "device": self.device,
            "state": self.state,
            "message": self.message,
            "done": self.done,
            "total": self.total,
            "elapsed_s": round(elapsed, 1),
            "remaining_s": round(remaining, 1) if remaining else None,
            "busy": self.busy,
        }


jobs: dict[str, Job] = {}            # device path -> that board's job
jobs_lock = threading.Lock()         # guards creating and starting jobs



def device_busy(device: str) -> bool:
    j = jobs.get(device)
    return bool(j and j.busy)


def _progress_by_time(job: Job):
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
    """Find every EiBotBoard attached, and its name, without opening any port.

    A named EBB reports its nickname as its USB serial number once it has
    re-enumerated, so the name comes straight from the USB descriptor. Opening
    ports to ask was slow, a second or more per board per request, and it
    raced: two plots starting together each probed the other's board while
    that board's own check was trying to read its voltage, and one was refused.

    Only a board that has never been named, and so has no serial number, still
    gets asked over serial, and only when nothing else holds it.
    """
    import serial
    import serial.tools.list_ports as lp

    CR = chr(13).encode()
    out = []
    for p in sorted(lp.comports(), key=lambda x: x.device):
        if (p.vid, p.pid) != EBB_VID_PID:
            continue
        busy = device_busy(p.device)
        nickname = (p.serial_number or "").strip()
        if not nickname and not busy:
            try:
                with hold(p.device) as got:
                    if got:
                        with serial.Serial(p.device, 9600, timeout=1.5) as sp:
                            time.sleep(0.15)
                            sp.write(CR)          # end any half command first
                            time.sleep(0.2)
                            sp.reset_input_buffer()
                            sp.write(b"QT" + CR)
                            time.sleep(0.3)
                            reply = sp.read(100).decode(errors="replace").strip()
                            first = reply.splitlines()[0].strip() if reply else ""
                            if first not in ("", "OK") and "Err" not in first:
                                nickname = first
                    else:
                        busy = True
            except Exception:
                pass
        out.append({"device": p.device, "nickname": nickname,
                    "busy": busy, "label": nickname or p.device})
    return out


def resolve(port):
    """Turn a nickname or device path into a known board, or None.

    Asks the running jobs first, so a board that is mid-plot can still be
    found by name without anything opening its port.
    """
    if not port:
        return None        # never guess a board: that is what bit us today
    for j in jobs.values():
        if port in (j.label, j.device):
            return {"device": j.device, "label": j.label,
                    "nickname": j.label, "busy": j.busy}
    for b in list_boards():
        if port in (b["nickname"], b["device"]):
            return b
    return None


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

    with hold(device, wait=3) as got:
        if not got:
            raise RuntimeError("another process is using this board")
        with serial.Serial(device, 9600, timeout=2) as sp:
            time.sleep(0.25)
            sp.write(chr(13).encode())     # end any half command left by a replug
            time.sleep(0.2)
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
        return False, out, None

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
            return False, out, None
    else:
        if not req("only one plotter, so auto-select is safe", len(boards) == 1,
                   f"{len(boards)} boards attached, pick one by name"):
            return False, out, None
        chosen = boards[0]

    try:
        h = board_health(chosen["device"])
    except Exception as exc:
        # A board that another plot is using cannot be asked anything, and
        # saying so plainly beats the lock's generic complaint.
        if device_busy(chosen["device"]):
            req("the board is free", False,
                f"{chosen['label']} is already drawing")
        else:
            req("the board answers", False, f"{type(exc).__name__}: {exc}")
        return False, out, chosen

    ok = req("the board answers", True, h["firmware"])
    ok &= req("motor power is present",
              h["volts"] >= MIN_MOTOR_VOLTS,
              f"only {h['volts']} V on the motor rail. The board runs its logic "
              f"off USB, so with the supply off it accepts a whole plot and "
              f"reports it finished while nothing moves. Check the barrel jack.",
              ok_detail=f"{h['volts']} V on the motor rail")
    out.append({"label": "board status byte", "ok": True,
                "detail": f"QG {h['qg']}, recorded not judged"})
    return bool(ok), out, chosen


def plot_worker(job, points, model, speed, pen_up, pen_down, preview, accel=75):
    from pyaxidraw import axidraw

    # The board was resolved to a device before this started, so the lock is on
    # the same path a command line tool would use. Held for the whole plot.
    # The driver is told the device path too, not the nickname, so it cannot
    # wander off to a different board if two are attached.
    device = job.device
    port = device
    ad = axidraw.AxiDraw()
    lock = hold(device, wait=3)
    if not lock.__enter__():
        job.state = "error"
        job.message = (f"{job.label} is in use by something else. A command "
                       f"line tool has it.")
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

        # port_config 1 does NOT mean "use the port I named". In the driver it
        # means "ignore the name and use the first AxiDraw found". Setting it,
        # as this code did, sent every plot to whichever board enumerated
        # first: axidraw-1's jobs drew on axidraw-0, and two "simultaneous"
        # plots fought over one board. 0 is the setting that honours o.port.
        o.port = port
        o.port_config = 0
        if not ad.connect():
            job.state, job.message = "error", f"could not open {job.label} ({port})"
            return

        # Ask the board its name before moving anything. This is the claim that
        # would have caught the port_config bug on the first plot: a board that
        # answers to the wrong name is the wrong machine, and it gets no ink.
        answered = (ad.usb_query("QT\r") or "").strip().splitlines()
        answered = answered[0].strip() if answered else ""
        if answered.lower() != job.label.lower() and job.label != port:
            job.state = "error"
            job.message = (f"refused: asked for {job.label}, but the board that "
                           f"answered is {answered or 'unnamed'}")
            ad.disconnect()
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
            prog = threading.Thread(target=_progress_by_time, args=(job,),
                                    daemon=True)
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


def _pick_job(port):
    """The job a request means: the named board, or the only live one."""
    if port:
        b = resolve(port)
        return jobs.get(b["device"]) if b else None
    running = [j for j in jobs.values() if j.busy]
    if len(running) == 1:
        return running[0]
    if len(jobs) == 1:
        return next(iter(jobs.values()))
    return None


@app.get("/api/status")
def status():
    port = request.args.get("port") or None
    j = _pick_job(port)
    if j:
        snap = j.snapshot()
    else:
        snap = {"board": port or "", "state": "idle", "done": 0, "total": 0,
                "elapsed_s": 0, "remaining_s": None, "busy": False,
                "message": "nothing plotted yet on this board"}
    # What the other machines are up to, so watching one does not mean losing
    # track of the rest.
    snap["others"] = [
        {"board": o.label, "state": o.state,
         "pct": round(100 * o.done / o.total) if o.total else 0,
         "remaining_s": o.snapshot()["remaining_s"]}
        for o in jobs.values() if o is not j and o.state != "idle"]
    return jsonify(snap)


@app.post("/api/stop")
def stop():
    body = request.get_json(silent=True) or {}
    port = body.get("port") or request.args.get("port") or None
    j = _pick_job(port)
    if not j or not j.busy:
        running = [o.label for o in jobs.values() if o.busy]
        why = (f"{port} is not drawing" if port else
               "more than one board is drawing, say which" if len(running) > 1
               else "nothing is running")
        return jsonify({"ok": False, "message": why, "running": running}), 409
    j.stop.set()
    return jsonify({"ok": True, "message": f"stopping {j.label}"})


@app.post("/api/check")
def check():
    body = request.get_json(force=True)
    points = body.get("points") or []
    model = int(body.get("model", 2))
    ok, claims = check_claims(points, model)
    # Ask the hardware too. Preflight leaves a board that is mid-plot alone and
    # says it is busy, so checking one machine never disturbs the other.
    hw_ok, hw, _ = preflight(body.get("port") or None)
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
    body = request.get_json(force=True)
    points = [(float(p[0]), float(p[1])) for p in (body.get("points") or [])]
    model = int(body.get("model", 2))

    # Claims run outside the jobs lock: preflight talks to hardware and takes
    # a second or two, and one board's check must not hold up the other.
    ok, claims = check_claims(points, model)
    hw_ok, hw, chosen = preflight(body.get("port") or None)
    claims = claims + hw
    ok = ok and hw_ok
    if not ok:
        failed = [c for c in claims if not c["ok"]]
        return jsonify({"ok": False, "claims": claims,
                        "message": "refusing to move: " +
                        "; ".join(f"{c['label']} ({c['detail']})" if c["detail"]
                                  else c["label"] for c in failed)}), 400

    with jobs_lock:
        dev = chosen["device"]
        job = jobs.get(dev)
        if job and job.busy:
            return jsonify({"ok": False,
                            "message": f"{job.label} is already drawing"}), 409
        job = Job(dev, chosen["label"])
        jobs[dev] = job
        job.state = "running"
        job.message = "starting"
        job.total = len(points) - 1
        job.started = time.time()
        job.thread = threading.Thread(
            target=plot_worker,
            args=(job, points, model, int(body.get("speed", 25)),
                  int(body.get("pen_up", 60)), int(body.get("pen_down", 0)),
                  bool(body.get("preview", True)), int(body.get("accel", 75))),
            daemon=True)
        job.thread.start()
    return jsonify({"ok": True, "message": f"plotting on {job.label}",
                    "board": job.label, "segments": job.total})


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
