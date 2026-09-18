"""One thing at a time on a serial port.

Board discovery opens each port to ask its nickname. That is fine until
something else is mid-plot on it, at which point the probe kills the drawing:
reloading the web page took out a running plot exactly this way, and neither
end had any idea the other existed.

An advisory flock per device fixes it, because it works across processes. The
server holds it for the length of a plot, discovery takes it only if nobody
else has it, and the command line tools take it before they connect.

    with hold("/dev/ttyACM0") as got:
        if not got:
            ...   # someone else is using this board
"""

from __future__ import annotations

import contextlib
import os

try:
    import fcntl
    HAVE_FLOCK = True
except ImportError:      # Windows, where these tools only ever dry-run
    HAVE_FLOCK = False

LOCK_DIR = "/tmp"


def lock_path(device: str) -> str:
    return os.path.join(LOCK_DIR, "piplot-" + device.replace("/", "_") + ".lock")


@contextlib.contextmanager
def hold(device: str, blocking: bool = False):
    """Yield True if this process now owns the device, False if someone else does.

    Never raises on contention: a caller that cannot get the lock should say
    so and carry on, not crash. The lock is advisory, so it only works because
    everything that touches a board goes through here.
    """
    if not HAVE_FLOCK or not device:
        yield True
        return

    fd = None
    got = False
    try:
        fd = os.open(lock_path(device), os.O_CREAT | os.O_RDWR, 0o666)
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
            got = True
        except (BlockingIOError, OSError):
            got = False
        yield got
    finally:
        if fd is not None:
            if got:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(fd)
