"""Offline checks for the machine registry and the claims that guard a plot.

Runs against the real machines.json on whatever Pi it is on, in its own
process, so it can be run while the server is up without touching it. It
deliberately never starts a real plot: every /api/plot here is expected to be
REFUSED, and the happy path is tested on paper with a person watching.

    ~/venv/bin/python test_registry.py
"""

from __future__ import annotations

import threading

import machines
import server


def check(label, cond, detail=""):
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))
    return bool(cond)


def main() -> int:
    ok = True
    app = server.app.test_client()

    print("=== the registry resolves without opening anything ===")
    reg = machines.load()
    for name, e in sorted(reg.items()):
        print(f"    {name:11} {e['driver']:7} {e['travel']} -> {e['device']}")
    ok &= check("every configured machine resolved to a device",
                all(e["device"] for e in reg.values()),
                ", ".join(n for n, e in reg.items() if not e["device"]) or "all found")

    print("\n=== /api/info and /api/boards ===")
    info = app.get("/api/info").get_json()
    ok &= check("info lists machines", set(info["machines"]) == set(reg),
                ", ".join(sorted(info["machines"])))
    ok &= check("machines carry an envelope",
                all(m["x"] and m["y"] for m in info["machines"].values()))
    boards = app.get("/api/boards").get_json()["boards"]
    ok &= check("boards lists the same machines",
                {b["label"] for b in boards} == set(reg))
    ok &= check("all report attached", all(b["attached"] for b in boards))

    print("\n=== claims refuse rather than raise ===")
    square = [(10, 10), (100, 10), (100, 100), (10, 100), (10, 10)]
    good, claims = server.check_claims(square, [430.0, 297.0], "axidraw-0")
    ok &= check("a path inside the envelope passes", good)

    # The old code indexed MODELS[model] before checking membership, so an
    # unknown machine was a KeyError and a 500 rather than a stated refusal.
    try:
        good, claims = server.check_claims(square, None, "nonexistent")
        ok &= check("an unknown machine is refused, not an exception", not good,
                    next(c["detail"] for c in claims if not c["ok"]))
    except Exception as exc:
        ok &= check("an unknown machine is refused, not an exception", False,
                    f"raised {type(exc).__name__}")

    tall = [(10, 10), (10, 400)]
    good, _ = server.check_claims(tall, [430.0, 297.0], "axidraw-0")
    ok &= check("A3 portrait is refused on the landscape machine", not good)
    good, _ = server.check_claims(tall, [297.0, 420.0], "suidraw-0")
    ok &= check("the same path passes on a 297x420 machine", good)

    print("\n=== a plot without the parked confirmation is refused ===")
    name = sorted(reg)[0]
    r = app.post("/api/plot", json={"points": square, "port": name})
    body = r.get_json()
    parked = [c for c in body.get("claims", [])
              if "parked" in c["label"]]
    ok &= check("refused with 400", r.status_code == 400, str(r.status_code))
    ok &= check("the parked claim is present and failing",
                bool(parked) and not parked[0]["ok"])
    ok &= check("nothing started", not any(j.busy for j in server.snapshot_jobs()))

    print("\n=== status polling does not blow up while jobs appear ===")
    # Iterating jobs unguarded while /api/plot inserts raises "dictionary
    # changed size during iteration", turning a routine poll into a 500.
    errors = []

    def poll():
        try:
            for _ in range(400):
                app.get("/api/status?port=" + name)
        except Exception as exc:
            errors.append(exc)

    def churn():
        try:
            for i in range(400):
                with server.jobs_lock:
                    server.jobs[f"/dev/fake{i}"] = server.Job(f"/dev/fake{i}", f"fake{i}")
                with server.jobs_lock:
                    server.jobs.pop(f"/dev/fake{i}", None)
        except Exception as exc:
            errors.append(exc)

    ts = [threading.Thread(target=poll), threading.Thread(target=churn)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    ok &= check("no races between status and job creation", not errors,
                "; ".join(f"{type(e).__name__}: {e}" for e in errors))

    print()
    print("ALL GOOD" if ok else "SOMETHING FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
