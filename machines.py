"""The machines this Pi can drive, and how to find each one without opening it.

Every machine here is described in `machines.json`, which lives next to this
file on the Pi and is deliberately NOT in the repo, so a machine can be added
or its envelope corrected without a deploy and a server restart.

**Nothing in this module opens a serial port, and nothing in it ever should.**
That is the whole point of it. Discovery used to find boards by scanning USB
ids and then opening the port of any board that had no serial number, to ask
its name over serial. That is merely rude to an EiBotBoard, which loses its
plot. It is much worse to the GRBL machine: opening its port resets the board,
which aborts the plot AND loses its position, and with no homing switches a
lost position needs a human to re-park the carriage by hand.

So machines are found through udev's symlinks instead, which are already on
disk and cost nothing to read:

    by_id    /dev/serial/by-id/*     a named EiBotBoard reports its nickname as
                                     its USB serial number, so the name is in
                                     the link
    by_path  /dev/serial/by-path/*   the physical USB socket, which is how you
                                     find a board that has no serial number at
                                     all, like the CH340 on the GRBL machine

A machine with no `machines.json` at all still works: `autodiscover()` finds
the EiBotBoards the same way the server always did, minus the probing.
"""

from __future__ import annotations

import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "machines.json")

BY_ID = "/dev/serial/by-id"
BY_PATH = "/dev/serial/by-path"

# What an entry may say. Anything else is ignored rather than rejected, so an
# older server does not choke on a key added for a newer machine.
DRIVERS = ("axidraw", "grbl", "polargraph")

# A polargraph's shape is not a rectangle, so its entry names a rig from
# polargraph.py instead of trusting a bare travel. The rig carries the anchor
# span, the drop and the paper, which is what check() needs to refuse the dead
# corners. Optional per-entry overrides: span, drop, gondola_g.
POLAR_RIGS = ("bench", "whiteboard", "stairwell")


def geometry(entry: dict):
    """The polargraph.Polargraph an entry describes, or None if not a polargraph.

    Pure: builds the geometry from the entry, opens nothing. Raises ValueError
    for an unknown rig, since a polargraph plotted against the wrong geometry
    draws something plausible in the wrong place and reports success.
    """
    if entry.get("driver") != "polargraph":
        return None
    import polargraph

    rig = entry.get("rig")
    if rig not in POLAR_RIGS:
        raise ValueError(f"polargraph entry needs a rig, one of "
                         f"{', '.join(POLAR_RIGS)}; got {rig!r}")
    kw = {k: float(entry[k]) for k in ("span", "drop", "gondola_g") if k in entry}
    return getattr(polargraph, rig)(**kw)


def _resolve(entry: dict) -> str | None:
    """The real device path for one entry, or None if it is not plugged in.

    Reads symlinks only. A machine that is switched off but still plugged in
    resolves fine, which is correct: whether it answers is a different question
    from whether it is attached, and answering it means opening the port.
    """
    pattern = entry.get("by_id")
    if pattern:
        hits = sorted(glob.glob(os.path.join(BY_ID, pattern)))
        if hits:
            return os.path.realpath(hits[0])
    name = entry.get("by_path")
    if name:
        link = os.path.join(BY_PATH, name)
        if os.path.exists(link):
            return os.path.realpath(link)
    device = entry.get("device")
    if device and os.path.exists(device):
        return os.path.realpath(device)
    return None


def autodiscover() -> dict:
    """The EiBotBoards attached, for a Pi with no machines.json.

    A named EBB puts its nickname in its by-id link, so this reads names
    without opening anything. Unnamed boards are skipped rather than probed:
    the probe is what this module exists to avoid, and a board worth plotting
    to is worth naming with `ST,<name>`.

    The envelope is assumed to be the A3 machine, which is what both boards on
    lyre are and what the pages already default to. Put a machines.json in
    place if that is wrong; guessing is exactly what the config file is for.
    """
    out = {}
    for link in sorted(glob.glob(os.path.join(BY_ID, "*EiBotBoard_*"))):
        base = os.path.basename(link)
        try:
            name = base.split("EiBotBoard_", 1)[1].split("_")[0]
        except IndexError:
            continue
        if not name:
            continue
        out[name] = {
            "driver": "axidraw",
            "model": 2,
            "travel": [430.0, 297.0],
            "by_id": f"*EiBotBoard_{name}_*",
            "assumed": True,          # so the server can say the size is a guess
        }
    return out


def load(path: str | None = None) -> dict:
    """The registry: machine name -> entry, with the device resolved.

    Falls back to autodiscovery when there is no config, so a fresh Pi with two
    AxiDraws plugged in still works with no setup. A malformed config is NOT
    silently replaced by the fallback: it raises, because quietly plotting with
    a guessed envelope is how a carriage ends up in a rail end.
    """
    path = path or DEFAULT_PATH
    if os.path.exists(path):
        with open(path) as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: expected an object of machine name to entry")
    else:
        raw = autodiscover()

    out = {}
    for name, entry in raw.items():
        # Keys starting with "_" are notes for the human editing the file.
        # machines.example.json opens with "_comment" and tells you to copy
        # it, so without this a machines.json made exactly as instructed
        # failed to load. Found 22 Sep setting up polarpi.
        if name.startswith("_"):
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: {name} is not an object")
        driver = entry.get("driver", "axidraw")
        if driver not in DRIVERS:
            raise ValueError(f"{path}: {name} has unknown driver {driver!r}, "
                             f"expected one of {', '.join(DRIVERS)}")
        travel = entry.get("travel")
        if not (isinstance(travel, (list, tuple)) and len(travel) == 2
                and all(isinstance(v, (int, float)) and v > 0 for v in travel)):
            raise ValueError(f"{path}: {name} needs a travel of [width, height] "
                             f"in mm, got {travel!r}. Nothing else knows how big "
                             f"this machine is, and $130/$131 on a GRBL board "
                             f"are not to be believed.")
        if driver == "polargraph":
            try:
                geom = geometry(entry)
            except ValueError as exc:
                raise ValueError(f"{path}: {name}: {exc}") from None
            if [round(v, 3) for v in geom.travel] != [round(float(v), 3)
                                                     for v in travel]:
                raise ValueError(
                    f"{path}: {name} says travel {travel} but its rig "
                    f"{entry['rig']!r} is {list(geom.travel)}. One of them is "
                    f"wrong, and plotting against the wrong one puts the "
                    f"drawing in the wrong place with nothing reported.")
        e = dict(entry)
        e["name"] = name
        e["driver"] = driver
        e["travel"] = [float(travel[0]), float(travel[1])]
        e["device"] = _resolve(entry)
        out[name] = e
    return out


def device_for(name: str, registry: dict | None = None) -> str | None:
    """The real device path for a machine name, without opening anything."""
    reg = registry if registry is not None else load()
    entry = reg.get(name)
    return entry["device"] if entry else None
