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

from flask import Flask, abort, jsonify, request, send_from_directory

import machines  # noqa: E402  which machines exist, and how to find them
from pen_box import MODELS  # noqa: E402  the AxiDraw travel envelopes
from portlock import hold  # noqa: E402  one thing at a time on a port

VERSION = "0.8.0"

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
app = Flask(__name__, static_folder=None)


class Job:
    """One board's plot, and everything anyone can ask about it.

    There is one of these per board rather than one for the server, so two
    machines can draw at once. Each has its own stop flag, because stopping
    the A3 must never stop the A1 beside it.
    """

    def __init__(self, device: str, label: str, driver: str = "axidraw",
                 travel=(430.0, 297.0)) -> None:
        self.device = device
        self.label = label
        self.driver = driver
        self.travel = list(travel)
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


def snapshot_jobs() -> list:
    """The current jobs as a list, taken under the lock.

    Flask runs threaded, so iterating `jobs` directly while `/api/plot`
    inserts a new one raises "dictionary changed size during iteration" and
    turns a routine status poll into a 500. Every read of every job goes
    through here.
    """
    with jobs_lock:
        return list(jobs.values())


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


def check_claims(points, travel, machine_name="") -> tuple[bool, list]:
    """State what makes this path plottable, and refuse rather than warn.

    None of these machines has home switches or soft limits of its own. Send
    one a point past the end of a rail and it will drive there, grind, lose
    steps, and report nothing. So the envelope is checked here, before the
    motors are enabled, not hoped for.

    The envelope arrives as the chosen machine's own travel rather than being
    looked up from a model number. That table only ever described AxiDraws,
    and it was indexed before its own "is this a machine we know" claim had
    run, so an unknown number was a 500 instead of a refusal.
    """
    out = []

    def req(label, cond, detail="", ok_detail=""):
        # The long text explains a failure, so only say it when it failed.
        out.append({"label": label, "ok": bool(cond),
                    "detail": ok_detail if cond else detail})
        return bool(cond)

    ok = True
    ok &= req("the machine is one this server knows", bool(travel),
              f"{machine_name or 'no machine'} is not in machines.json",
              ok_detail=machine_name)
    if not travel:
        return False, out
    tx, ty = travel

    ok &= req("path has at least two points", len(points) >= 2, f"{len(points)} points")
    if len(points) < 2:
        return False, out

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    ok &= req("every point is a finite number",
              all(math.isfinite(v) for v in xs + ys))
    ok &= req("x stays within travel", min(xs) >= 0 and max(xs) <= tx,
              f"x spans {min(xs):.1f} to {max(xs):.1f} mm, "
              f"{machine_name or 'the machine'} has 0 to {tx:.0f}")
    ok &= req("y stays within travel", min(ys) >= 0 and max(ys) <= ty,
              f"y spans {min(ys):.1f} to {max(ys):.1f} mm, "
              f"{machine_name or 'the machine'} has 0 to {ty:.0f}")
    return bool(ok), out


def list_boards():
    """Every machine in the registry, with its device, read from udev only.

    This used to scan USB ids and then OPEN THE PORT of any board with no
    serial number to ask its name over serial. That probe is gone, not merely
    guarded, because the machine it would have found is exactly the one it
    must never touch: opening the GRBL board's port resets it, which aborts
    the plot and loses its position, and with no homing switches that needs a
    human to re-park the carriage. `/api/status` polls every few seconds, so
    the probe would have fired continuously against an idle board.

    A machine that is configured but unplugged is still listed, with
    attached False, because "your machine is not plugged in" is a better
    answer than silently not offering it.
    """
    reg = machines.load()
    out = []
    for name, e in sorted(reg.items()):
        device = e["device"]
        out.append({
            "device": device,
            "nickname": name,
            "label": name,
            "busy": device_busy(device) if device else False,
            "attached": bool(device),
            "driver": e["driver"],
            "travel": e["travel"],
            "assumed": bool(e.get("assumed")),
        })
    return out


def resolve(port):
    """Turn a machine name or device path into a known machine, or None.

    Safe to call from a request thread at any time, including mid-plot, since
    nothing here opens a port. Running jobs are consulted as a fallback so a
    machine unplugged mid-plot can still be found by name in order to be
    stopped.
    """
    if not port:
        return None        # never guess a board: that is what bit us today
    for b in list_boards():
        if port in (b["nickname"], b["device"]):
            return b
    for j in snapshot_jobs():
        if port in (j.label, j.device):
            return {"device": j.device, "label": j.label, "nickname": j.label,
                    "busy": j.busy, "attached": True, "driver": j.driver,
                    "travel": j.travel, "assumed": False}
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
        attached = [b for b in boards if b["attached"]]
        if not req("only one plotter, so auto-select is safe", len(attached) == 1,
                   f"{len(attached)} machines attached, pick one by name"):
            return False, out, None
        chosen = attached[0]

    if not req("the machine is plugged in", chosen["attached"],
               f"{chosen['label']} is in machines.json but its device is not "
               f"there. Check the cable, and that it is in the USB socket its "
               f"by_path names."):
        return False, out, chosen

    # Busy FIRST, before anything opens anything. This check used to live in
    # the exception handler below, which meant board_health had already opened
    # the port by the time it ran. On an EiBotBoard that kills the running
    # plot; on the GRBL board opening the port is a reset, so it also loses the
    # position, and with no homing that needs a human to re-park the carriage.
    if not req("the machine is free", not device_busy(chosen["device"]),
               f"{chosen['label']} is already drawing"):
        return False, out, chosen

    if chosen["driver"] != "axidraw":
        # Deliberately no health check for GRBL. Every way of asking this board
        # a question opens its port, and opening its port resets it. The
        # worker's own connect is the first thing allowed to touch it, and any
        # problem surfaces there where it can be reported against a real job.
        out.append({"label": "machine is ready", "ok": True,
                    "detail": f"{chosen['label']} ({chosen['driver']}), not "
                              f"probed: opening this board's port resets it"})
        return True, out, chosen

    try:
        h = board_health(chosen["device"])
    except Exception as exc:
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


def grbl_worker(job, paths, entry):
    """Drive a GRBL machine for one plot, start to finish, on one connection.

    One connection is not a style choice. GRBL has no homing here, so its
    position means nothing after a reset, and opening the serial port IS a
    reset. Reconnecting part way through would silently move the origin to
    wherever the carriage happened to be. The operator has already confirmed
    the carriage is parked, which is what makes the zeroing below legitimate.
    """
    from plotter import Grbl, merge_paths

    g = Grbl(port=job.device,
             travel=tuple(entry["travel"]),
             feed=int(entry.get("feed", 5000)),
             pen_up_z=float(entry.get("pen_up_z", 1.0)),
             pen_down_z=float(entry.get("pen_down_z", 0.0)),
             pen_dwell_s=float(entry.get("pen_dwell_s", 0.25)),
             flip_y=bool(entry.get("flip_y", True)))

    lock = hold(job.device, wait=3)
    if not lock.__enter__():
        job.state = "error"
        job.message = f"{job.label} is in use by something else"
        return
    try:
        g.connect()
        g.set_origin_here()      # legitimate: the operator confirmed the park
        drawn = merge_paths(paths)
        job.total = sum(len(p) - 1 for p in drawn)
        job.message = "drawing"
        done = 0
        for i, path in enumerate(drawn):
            if job.stop.is_set():
                break
            job.message = f"drawing path {i + 1} of {len(drawn)}"
            if not g.draw_path(path, job.stop):
                break
            done += len(path) - 1
            job.done = done
        g.disconnect()
        if job.stop.is_set():
            job.state = "stopped"
            job.message = f"stopped by request after {job.done} of {job.total} segments"
        else:
            job.state = "done"
            job.message = (f"finished, {job.total} segments in "
                           f"{time.time() - job.started:.0f}s")
    except Exception as exc:
        job.state, job.message = "error", f"{type(exc).__name__}: {exc}"
        # Leave the machine safe, not mid-stroke with moves still queued. The
        # pen is the urgent part: a stopped carriage with the nib down bleeds
        # a blot through the paper.
        try:
            g._halt()
            g.disconnect()
        except Exception:
            pass
    finally:
        lock.__exit__(None, None, None)


def plot_worker(job, paths, model, speed, pen_up, pen_down, preview, accel=75):
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
            xs = [q[0] for p in paths for q in p]
            ys = [q[1] for p in paths for q in p]
            box = [(min(xs), min(ys)), (max(xs), min(ys)),
                   (max(xs), max(ys)), (min(xs), max(ys)), (min(xs), min(ys))]
            for x, y in box:
                if job.stop.is_set():
                    break
                ad.moveto(x, y)

        if not job.stop.is_set():
            # One planned move per path. Drawing segment by segment with lineto
            # plans each move on its own, so the machine accelerates from rest
            # and stops again for every segment, which is the jerkiness.
            #
            # draw_path travels pen-up to the start of its path, lowers, draws
            # and raises again, so a list of paths is simply one call each:
            # that is the pen lift between strokes a map or a hatch needs. The
            # driver takes the stop Event itself, and the loop checks it too so
            # a stop between paths does not start the next one.
            ad.set_up_pause_receiver(job.stop)
            if len(paths) == 1:
                # No per-segment callback exists, so a single long path reports
                # progress against a length over speed estimate, and says so.
                job.message = "drawing"
                job.estimate_s = path_length(paths[0]) / (2.18 * max(1, speed))
                threading.Thread(target=_progress_by_time, args=(job,),
                                 daemon=True).start()
            done = 0
            for i, path in enumerate(paths):
                if job.stop.is_set():
                    break
                if len(paths) > 1:
                    job.message = f"drawing path {i + 1} of {len(paths)}"
                ad.draw_path([[float(x), float(y)] for x, y in path])
                # Only a path that ran to the end is complete. draw_path also
                # returns when stopped, and counting that one as done made a
                # stop at 10 of 40 report "stopped after 40 of 40".
                if not job.stop.is_set():
                    done += len(path) - 1
                    job.done = done

        if job.stop.is_set():
            # After a pause the driver flags the plot as stopped and from then
            # on every move returns silently, so the trip home below did
            # nothing and a stopped machine was left hanging mid-sheet. That is
            # worse than untidy: with no home switches, the next plot would take
            # wherever it hung as its origin. Clear the flag, and hand the
            # driver a fresh unset signal so it does not pause the trip home
            # too. job.stop stays set, since it is what reports "stopped".
            ad.plot_status.stopped = 0
            ad.set_up_pause_receiver(threading.Event())
            ad.clear_pause_request()

        job.message = "returning home"
        ad.penup()
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


PAGES = ("index", "import")


@app.get("/")
def index():
    return send_from_directory(DOCS, "index.html")


@app.get("/<name>.html")
def page(name):
    # Only the pages that exist. The import page linked to import.html, which
    # this server never served, so it was a 404 on the Pi and only ever
    # worked on GitHub Pages or through a test harness.
    if name not in PAGES:
        abort(404)
    return send_from_directory(DOCS, name + ".html")


def page_stamp():
    """A short hash of the page as it is on disk right now.

    The server version only moves when server.py changes, so it cannot see a
    deploy that only touched the page. This can, which is what lets the browser
    notice it is running yesterday's javascript against today's server.
    """
    h = hashlib.md5()
    try:
        for name in PAGES:
            with open(os.path.join(DOCS, name + ".html"), "rb") as fh:
                h.update(fh.read())
        return h.hexdigest()[:8]
    except OSError:
        return "unknown"


@app.get("/api/info")
def info():
    return jsonify({
        "version": VERSION,
        "page": page_stamp(),
        "models": {k: {"x": v[0], "y": v[1], "name": v[2]} for k, v in MODELS.items()},
        # The machines that actually exist here, which is what the pages should
        # offer. `models` stays for the moment so an older cached page keeps
        # working through the deploy rather than breaking halfway.
        "machines": {b["label"]: {"x": b["travel"][0], "y": b["travel"][1],
                                  "driver": b["driver"], "attached": b["attached"],
                                  "assumed": b["assumed"]}
                     for b in list_boards()},
        "host": os.uname().nodename,
    })


@app.get("/api/boards")
def boards():
    return jsonify({"boards": list_boards()})


def _pick_job(port):
    """The job a request means: the named machine, or the only live one.

    Falls back to matching the job's own label when the registry cannot
    resolve the name. A stop that cannot find its machine returns "not
    drawing" while the machine carries on plotting, and a stop button that
    silently does nothing is the worst failure available here.
    """
    js = snapshot_jobs()
    if port:
        b = resolve(port)
        if b and b["device"] in jobs:
            return jobs[b["device"]]
        for j in js:
            if port in (j.label, j.device):
                return j
        return None
    running = [j for j in js if j.busy]
    if len(running) == 1:
        return running[0]
    if len(js) == 1:
        return js[0]
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
        for o in snapshot_jobs() if o is not j and o.state != "idle"]
    return jsonify(snap)


@app.post("/api/stop")
def stop():
    body = request.get_json(silent=True) or {}
    port = body.get("port") or request.args.get("port") or None
    j = _pick_job(port)
    if not j or not j.busy:
        running = [o.label for o in snapshot_jobs() if o.busy]
        why = (f"{port} is not drawing" if port else
               "more than one board is drawing, say which" if len(running) > 1
               else "nothing is running")
        return jsonify({"ok": False, "message": why, "running": running}), 409
    j.stop.set()
    return jsonify({"ok": True, "message": f"stopping {j.label}"})


# Rough servo time for one pen lift and lower. Guessed, not measured, and it
# matters: at half a second a map of 17,761 separate strokes spends about two
# and a half hours on lifts alone, which a length over speed estimate hides
# completely and turned a plot that would take hours into "16 min".
LIFT_S = 0.5


def read_paths(body):
    """A request's drawing as a list of paths, from either payload shape.

    The curve studio sends one continuous line as "points". The import page
    sends "paths", one per stroke, so the pen can lift between them.
    """
    if body.get("paths"):
        paths = [[(float(q[0]), float(q[1])) for q in p]
                 for p in body["paths"] if p and len(p) >= 2]
    else:
        pts = [(float(q[0]), float(q[1])) for q in (body.get("points") or [])]
        paths = [pts] if pts else []
    return paths


def estimate(paths, speed):
    """Seconds, counting ink, pen-up travel and the lifts between strokes."""
    ink = sum(path_length(p) for p in paths)
    travel, at = 0.0, (0.0, 0.0)
    for p in paths:
        travel += math.dist(at, p[0])
        at = p[-1]
    travel += math.dist(at, (0.0, 0.0))            # and home again
    return (ink / (2.18 * max(1, speed)) + travel / (2.18 * 75)
            + len(paths) * LIFT_S), ink, travel


@app.post("/api/check")
def check():
    body = request.get_json(force=True)
    paths = read_paths(body)
    points = [q for p in paths for q in p]
    # Ask the hardware too. Preflight leaves a machine that is mid-plot alone
    # and says it is busy, so checking one never disturbs the other.
    hw_ok, hw, chosen = preflight(body.get("port") or None)
    travel = chosen["travel"] if chosen else None
    ok, claims = check_claims(points, travel, chosen["label"] if chosen else "")
    claims = claims + hw
    ok = ok and hw_ok
    speed = max(1, int(body.get("speed", 25)))
    est, ink, travel = estimate(paths, speed) if paths else (0, 0.0, 0.0)
    return jsonify({
        "ok": ok,
        "claims": claims,
        "paths": len(paths),
        "segments": sum(len(p) - 1 for p in paths),
        "length_mm": round(ink, 1),
        "travel_mm": round(travel, 1),
        # Still rough: acceleration is ignored and the lift time is a guess.
        "estimate_s": round(est),
    })


@app.post("/api/plot")
def plot():
    body = request.get_json(force=True)
    paths = read_paths(body)
    points = [q for p in paths for q in p]
    # Claims run outside the jobs lock: preflight talks to hardware and takes
    # a second or two, and one board's check must not hold up the other.
    hw_ok, hw, chosen = preflight(body.get("port") or None)
    travel = chosen["travel"] if chosen else None
    ok, claims = check_claims(points, travel, chosen["label"] if chosen else "")
    claims = claims + hw
    ok = ok and hw_ok

    # No machine here has home switches. Every one of them believes the
    # carriage is at (0, 0) wherever it happens to be sitting, so a plot sent
    # after a stop, an error or a restart is measured from a corner that is
    # not the corner. There is nothing in software that can check this, which
    # is exactly why it has to be asserted by a person each time.
    parked = bool(body.get("origin_confirmed"))
    ok = ok and parked
    claims.append({
        "label": "the carriage is parked in the home corner",
        "ok": parked,
        "detail": "" if parked else
                  "confirm the carriage is parked before plotting. No machine "
                  "here has home switches, so it will take wherever it is "
                  "standing as (0, 0) and every coordinate after that is wrong.",
    })

    if not ok:
        failed = [c for c in claims if not c["ok"]]
        return jsonify({"ok": False, "claims": claims,
                        "message": "refusing to move: " +
                        "; ".join(f"{c['label']} ({c['detail']})" if c["detail"]
                                  else c["label"] for c in failed)}), 400

    with jobs_lock:
        # Re-resolve inside the lock. `chosen` was read before preflight, which
        # takes a second or two talking to hardware, and in that window the
        # machine could have been unplugged, replugged onto a different device
        # node, or claimed by another request.
        entry = machines.load().get(chosen["label"])
        dev = entry["device"] if entry else None
        if not dev:
            return jsonify({"ok": False,
                            "message": f"{chosen['label']} went away between "
                                       f"the check and the plot"}), 409
        job = jobs.get(dev)
        if job and job.busy:
            return jsonify({"ok": False,
                            "message": f"{job.label} is already drawing"}), 409
        job = Job(dev, chosen["label"], entry["driver"], entry["travel"])
        jobs[dev] = job
        job.state = "running"
        job.message = "starting"
        job.total = sum(len(p) - 1 for p in paths)
        job.started = time.time()
        if entry["driver"] == "grbl":
            job.thread = threading.Thread(
                target=grbl_worker, args=(job, paths, entry), daemon=True)
        else:
            job.thread = threading.Thread(
                target=plot_worker,
                args=(job, paths, int(entry.get("model", 2)),
                      int(body.get("speed", entry.get("speed", 25))),
                      int(body.get("pen_up", entry.get("pen_up", 60))),
                      int(body.get("pen_down", entry.get("pen_down", 0))),
                      bool(body.get("preview", True)),
                      int(body.get("accel", entry.get("accel", 75)))),
                daemon=True)
        job.thread.start()
    return jsonify({"ok": True, "message": f"plotting on {job.label}",
                    "board": job.label, "paths": len(paths),
                    "segments": job.total})


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
