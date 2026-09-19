"""One thing at a time on a serial port.

Board discovery used to open each port to ask its nickname. That is fine until
something else is mid-plot on it, at which point the probe kills the drawing:
reloading the web page took out a running plot exactly this way, and neither
end had any idea the other existed.

An advisory flock per device fixes it, because it works across processes. The
server holds it for the length of a plot, and the command line tools take it
before they connect.

    with hold("axidraw-0", wait=3) as got:
        if not got:
            ...   # someone else is using this board

The lock is keyed on the real device path, whatever name the caller used. The
command line tools used to lock "axidraw-0" while the server locked
"/dev/ttyACM0", which are two different lock files, so the two never actually
excluded each other.
"""

from __future__ import annotations

import contextlib
import glob
import os
import time

try:
    import fcntl
    HAVE_FLOCK = True
except ImportError:      # Windows, where these tools only ever dry-run
    HAVE_FLOCK = False

LOCK_DIR = "/tmp"


def canonical(name: str) -> str:
    """The real device path for a machine name, a by-id link or a device path.

    A named EiBotBoard reports its nickname as its USB serial number, so udev
    gives it a stable /dev/serial/by-id link with the name in it. That is how a
    nickname is turned into a device without opening any port.

    The registry is asked first, because not every machine can be found that
    way. This used to glob only `*EiBotBoard_<name>_*`, which quietly returned
    the name unchanged for anything else: `hold("suidraw-0")` locked
    `/tmp/piplot-suidraw-0.lock` while the server locked
    `/tmp/piplot-_dev_ttyUSB0.lock`, so the two never excluded each other. That
    is the same bug described above, reintroduced for the one machine where
    losing the race costs more than a plot. The GRBL board has no USB serial
    number at all and is found by its physical socket instead.
    """
    if not name:
        return name
    if not name.startswith("/"):
        device = None
        try:
            import machines
            device = machines.device_for(name)
        except Exception:
            # A missing or broken registry must not stop a tool locking a port
            # it named explicitly, so fall through to the old behaviour.
            pass
        if device:
            return device
        hits = glob.glob(f"/dev/serial/by-id/*EiBotBoard_{name}_*")
        if not hits:
            return name
        name = hits[0]
    return os.path.realpath(name)


def lock_path(device: str) -> str:
    return os.path.join(LOCK_DIR, "piplot-" + device.replace("/", "_") + ".lock")


@contextlib.contextmanager
def hold(device: str, wait: float = 0.0):
    """Yield True if this process now owns the device, False if someone else does.

    wait retries for that many seconds before giving up, which is for checks
    that only need the port briefly and would otherwise lose to another short
    check by a fraction of a second. Never raises on contention.
    """
    if not HAVE_FLOCK or not device:
        yield True
        return

    device = canonical(device)
    fd = None
    got = False
    try:
        fd = os.open(lock_path(device), os.O_CREAT | os.O_RDWR, 0o666)
        deadline = time.time() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except (BlockingIOError, OSError):
                if time.time() >= deadline:
                    break
                time.sleep(0.1)
        yield got
    finally:
        if fd is not None:
            if got:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)
