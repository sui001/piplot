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
import hmac
import ipaddress
import threading
import time

from flask import Flask, abort, jsonify, request, send_from_directory

import machines  # noqa: E402  which machines exist, and how to find them
from pen_box import MODELS  # noqa: E402  the AxiDraw travel envelopes
from portlock import hold  # noqa: E402  one thing at a time on a port

VERSION = "0.10.0"

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "docs")
app = Flask(__name__, static_folder=None)


# --------------------------------------------------------------- access gate
#
# Anyone can look and play: the pages, previews, imports and envelope checks
# are open. Only the two routes that move a machine, plot and stop, need the
# password, and only for visitors. Sui's own devices come over the tailnet,
# which Tailscale has already authenticated, so they go straight through.
# Everyone else comes via Tailscale Funnel, which proxies the public internet
# to 127.0.0.1 on this machine. The TCP peer address is what is checked, and a
# visitor cannot forge it: every Funnel request reaches the server from
# loopback.
#
# The password is not in this repo. It lives in access_password.txt beside
# this file, or PIPLOT_PASSWORD, and is re-read on every request so it can be
# set or changed without a restart. No password set means visitors cannot plot
# at all: a Funnel left open by accident must not mean three machines anyone
# can drive.

# The only routes that make a machine move.
ACTIONS = {"/api/plot", "/api/stop"}

# The server faces the internet, and nothing it legitimately takes is anywhere
# near this. A 57,000 point plot is about 2 MB.
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

TAILNET = [ipaddress.ip_network("100.64.0.0/10"),
           ipaddress.ip_network("fd7a:115c:a1e0::/48")]
PASSWORD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "access_password.txt")
_failures: list = []          # times of recent wrong passwords


def access_password() -> str:
    env = os.environ.get("PIPLOT_PASSWORD", "").strip()
    if env:
        return env
    try:
        with open(PASSWORD_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def from_tailnet(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in TAILNET)


def needs_password() -> bool:
    return not from_tailnet(request.remote_addr or "")


@app.before_request
def gate():
    if request.path not in ACTIONS or not needs_password():
        return None
    pw = access_password()
    if not pw:
        return jsonify({"ok": False, "need_password": True,
                        "message": "plotting is locked: no password is set on "
                                   "this machine"}), 403
    now = time.time()
    _failures[:] = [t for t in _failures if now - t < 60]
    if len(_failures) >= 20:
        return jsonify({"ok": False, "need_password": True,
                        "message": "too many wrong passwords, try again in a "
                                   "minute"}), 429
    given = request.headers.get("X-Plot-Password", "")
    if given and hmac.compare_digest(given.encode(), pw.encode()):
        return None
    if given:
        # A wrong guess costs time, so the password cannot be walked through
        # quickly. A request with no password at all is not counted.
        _failures.append(now)
        time.sleep(0.5)
    return jsonify({"ok": False, "need_password": True,
                    "message": "wrong password" if given
                               else "the password is needed to plot or stop"}), 401


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


def geometry_for(chosen):
    """The polargraph geometry behind a chosen machine, or None.

    `chosen` comes from list_boards, which carries only the rectangle, so the
    rig is read back from the registry. Opens nothing.
    """
    if not chosen or chosen.get("driver") != "polargraph":
        return None
    entry = machines.load().get(chosen["label"])
    return machines.geometry(entry) if entry else None


def check_claims(points, travel, machine_name="", geom=None) -> tuple[bool, list]:
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

    # A polargraph's envelope is not the rectangle above. It has dead corners
    # where a cord lies near flat and the gondola cannot hold a line, and
    # places above or outside the anchors it cannot reach at all. The
    # rectangle passes those; only the geometry knows. Refused here, before
    # the motors are enabled, rather than halfway through a drawing.
    if geom is not None:
        extra = [r for r in geom.check(points)
                 if not r.startswith(("x spans", "y spans"))]
        ok &= req("every point is reachable, with no cord near flat",
                  not extra, "; ".join(extra))
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
                              + ("probed here: the worker's own connect checks "
                                 "it is FluidNC, and refuses if opening the "
                                 "port reset it"
                                 if chosen["driver"] == "polargraph" else
                                 "probed: opening this board's port resets it")})
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
    from plotter import Grbl, Polargraph, merge_paths

    # The polargraph runs FluidNC, which speaks the same GRBL protocol, so it
    # rides this worker. What differs is the origin: a GRBL carriage is zeroed
    # here by G10 because the operator confirmed it is parked, but FluidNC's
    # WallPlotter derives its zero cord lengths once, at boot, and a G10 would
    # move the labels without moving the kinematics. Polargraph.set_origin_here
    # refuses for exactly that reason, so it is simply not called.
    polar = entry["driver"] == "polargraph"
    if polar:
        home = entry.get("home")
        g = Polargraph(machines.geometry(entry),
                       serial_port=job.device,
                       home=tuple(home) if home else None,
                       feed=int(entry.get("feed", 3000)),
                       pen_up_z=float(entry.get("pen_up_z", 5.0)),
                       pen_down_z=float(entry.get("pen_down_z", 0.0)),
                       pen_dwell_s=float(entry.get("pen_dwell_s", 0.25)))
    else:
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
        if not polar:
            g.set_origin_here()  # legitimate: the operator confirmed the park
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
        # Tells the page to show a password box beside Plot. False on the
        # tailnet, true for anyone arriving through Funnel.
        "needs_password": needs_password(),
    })


@app.get("/api/rig")
def rig_current():
    """The polargraph's geometry as configured, with everything derived from it.

    Read only. The numbers here are what /setup.html starts from, so someone
    with a tape measure can see what the machine currently believes before
    changing anything.
    """
    import polargraph

    for name, entry in sorted(machines.load().items()):
        if entry["driver"] != "polargraph":
            continue
        geom = machines.geometry(entry)
        return jsonify({"ok": True, "machine": name, "rig": entry.get("rig"),
                        "report": polargraph.rig_report(geom)})
    return jsonify({"ok": False, "message": "no polargraph in machines.json"}), 404


# What a human could plausibly build, in mm. Outside these something has been
# typed wrong, and a refusal naming the bound is kinder than a report full of
# nonsense.
RIG_BOUNDS = {"span": (200.0, 6000.0), "drop": (0.0, 4000.0),
              "sheet_w": (50.0, 5000.0), "sheet_h": (50.0, 12000.0),
              "gondola_g": (50.0, 5000.0)}


@app.post("/api/rig/check")
def rig_check():
    """Score a proposed rig. Changes nothing, moves nothing, writes nothing.

    This is the arithmetic that decides whether a beam hung at a given height
    can actually draw the corners of a given sheet. It refuses rather than
    warns, the same as every other check here, because a rig that cannot
    reach its own corners produces a drawing that looks plausible and is
    wrong at the edges.
    """
    import polargraph

    body = request.get_json(force=True) or {}
    vals, errors = {}, []
    for key, (lo, hi) in RIG_BOUNDS.items():
        raw = body.get(key)
        try:
            v = float(raw)
        except (TypeError, ValueError):
            errors.append(f"{key}: {raw!r} is not a number")
            continue
        if not (lo <= v <= hi):
            errors.append(f"{key}: {v:g} mm is outside {lo:g} to {hi:g}")
            continue
        vals[key] = v
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    if vals["span"] < vals["sheet_w"]:
        errors.append(f"the anchors ({vals['span']:g} mm apart) are narrower "
                      f"than the paper ({vals['sheet_w']:g} mm). The gondola "
                      f"cannot go outside its own anchors, so the sheet edges "
                      f"are unreachable whatever the height.")

    geom = polargraph.Polargraph(
        span=vals["span"], travel=(vals["sheet_w"], vals["sheet_h"]),
        origin=((vals["span"] - vals["sheet_w"]) / 2.0, vals["drop"]),
        gondola_g=vals["gondola_g"])
    report = polargraph.rig_report(geom)
    return jsonify({"ok": not errors and not report["refusals"],
                    "errors": errors, "report": report})


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


# A pdf big enough to exceed this is not a drawing, it is a scan, and the
# geometry we want is not in it.
PDF_MAX_BYTES = 32 * 1024 * 1024


def pdf_items(page):
    """One page's vector geometry, curves still curved.

    Deliberately does not flatten. The import page already turns svg curves
    into points with an adaptive subdivision the tolerance slider drives, and
    a second flattener here would be a second thing to keep in step with it
    for as long as both exist. So the control points go back untouched and
    the browser does the same maths to a pdf that it does to an svg.

    Outlines only: every drawing contributes its path whether the pdf meant
    to fill it or stroke it, which is what the svg importer already does when
    it walks the shapes and ignores fill. A pen has no other option.

    Two things the geometry has to be put through on the way out:

    Rotation. get_drawings() reports points in the page's UNROTATED space,
    while page.rect reports the rotated size a viewer shows. On a /Rotate 270
    A0 plan that is a drawing turned on its side, and nothing downstream can
    tell, because a sideways plan still fits the paper and still plots. So the
    page's own rotation matrix is applied here and everything leaves in one
    frame, the one you see when you open the file.

    Rounding. Two decimals of a pdf point is 0.0035 mm, an order of magnitude
    under anything these machines can hold, and it takes about a third off the
    reply. A big plan is 500,000 numbers and they all cross a wifi link.
    """
    m = page.rotation_matrix                  # identity when /Rotate is 0

    def pt(p):
        q = p * m
        return [round(q.x, 2), round(q.y, 2)]

    out = []
    for d in page.get_drawings():
        items = []
        for it in d["items"]:
            kind = it[0]
            if kind == "l":
                items.append(["l", pt(it[1]), pt(it[2])])
            elif kind == "c":
                items.append(["c"] + [pt(p) for p in it[1:5]])
            elif kind == "re":
                # Rotating by a multiple of 90 leaves a rect axis aligned, but
                # can swap which corner is which, so it is put back in order.
                a, b = pt(it[1].tl), pt(it[1].br)
                items.append(["re", [min(a[0], b[0]), min(a[1], b[1]),
                                     max(a[0], b[0]), max(a[1], b[1])]])
            elif kind == "qu":
                q = it[1]
                items.append(["qu"] + [pt(p) for p in (q.ul, q.ur, q.lr, q.ll)])
            # Anything else is not geometry a pen can follow.
        if items:
            out.append({"items": items, "closed": bool(d.get("closePath"))})
    return out


@app.post("/api/import/pdf")
def import_pdf():
    """Raw pdf bytes in, vector geometry out, in pdf points.

    Extraction is here rather than in the browser because pdf.js would mean
    maintaining a whole second geometry parser next to the svg one. Nothing
    else about the import pipeline changes: what this returns is fed through
    the same flatten, fit, tolerance and envelope checks as everything else.

    First page only. A multi-page pdf of a drawing is a document, and picking
    a page is a decision the page has nowhere to ask about yet.
    """
    try:
        import pymupdf                     # not `fitz`: deprecated since 1.24
    except ImportError:
        return jsonify({"error": "pymupdf is not installed on this pi, "
                                 "`~/venv/bin/pip install pymupdf`"}), 501
    data = request.get_data(cache=False)
    if not data:
        return jsonify({"error": "no pdf in the request body"}), 400
    if len(data) > PDF_MAX_BYTES:
        return jsonify({"error": f"pdf is {len(data) // 1024 // 1024} MB, over "
                                 f"the {PDF_MAX_BYTES // 1024 // 1024} MB "
                                 f"limit"}), 413
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        return jsonify({"error": f"that is not a pdf we can read: {e}"}), 400
    try:
        if doc.page_count < 1:
            return jsonify({"error": "the pdf has no pages"}), 400
        page = doc[0]
        paths = pdf_items(page)
        r = page.rect
        return jsonify({
            "pages": doc.page_count,
            # Pdf points, 72 to the inch, y down the page, which is the frame
            # the import page already thinks in.
            "width": r.width,
            "height": r.height,
            "paths": paths,
            "items": sum(len(p["items"]) for p in paths),
        })
    finally:
        doc.close()


@app.post("/api/check")
def check():
    body = request.get_json(force=True)
    paths = read_paths(body)
    points = [q for p in paths for q in p]
    # Ask the hardware too. Preflight leaves a machine that is mid-plot alone
    # and says it is busy, so checking one never disturbs the other.
    hw_ok, hw, chosen = preflight(body.get("port") or None)
    travel = chosen["travel"] if chosen else None
    ok, claims = check_claims(points, travel, chosen["label"] if chosen else "",
                              geometry_for(chosen))
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
    ok, claims = check_claims(points, travel, chosen["label"] if chosen else "",
                              geometry_for(chosen))
    claims = claims + hw
    ok = ok and hw_ok

    # No machine here has home switches. Every one of them believes the
    # carriage is at (0, 0) wherever it happens to be sitting, so a plot sent
    # after a stop, an error or a restart is measured from a corner that is
    # not the corner. There is nothing in software that can check this, which
    # is exactly why it has to be asserted by a person each time.
    parked = bool(body.get("origin_confirmed"))
    ok = ok and parked
    if chosen and chosen.get("driver") == "polargraph":
        # Same confirmation, different meaning. The polargraph's zero is set
        # at boot, from wherever the gondola hangs, so what has to be true is
        # that it was on its centre cross when the board last powered up, and
        # has not been moved by hand or lost steps since. Every clean plot
        # ends by returning there, which keeps it true between jobs.
        claims.append({
            "label": "the gondola is on its centre cross",
            "ok": parked,
            "detail": "" if parked else
                      "confirm the gondola was on the centre cross when the "
                      "board last booted, and has not been moved since. The "
                      "polargraph takes its zero from wherever it hung at "
                      "power on, and nothing in software can check it.",
        })
    else:
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
        if entry["driver"] in ("grbl", "polargraph"):
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

    # Also listen on loopback, because Tailscale Funnel proxies to 127.0.0.1
    # and a tailnet-only bind gives the public side a 502. Loopback is where
    # the password gate applies, so opening it does not open the machines.
    from werkzeug.serving import make_server
    hosts = [host] if host in ("127.0.0.1", "0.0.0.0") else [host, "127.0.0.1"]
    servers = [make_server(h, args.port, app, threaded=True) for h in hosts]
    for h in hosts:
        print(f"serving on http://{h}:{args.port}")
    print("tailnet: open.  visitors via Funnel: can look and design, "
          + ("password needed to plot or stop" if access_password() else
             "CANNOT plot, no access password set"))
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers[0].serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
