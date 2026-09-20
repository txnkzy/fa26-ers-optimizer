"""A record of finished runs, so two of them can be compared.

Every question worth asking about this tool has been a comparison — is this
map better than the one before it, did that change help, why is this circuit
suddenly a second slower. Answering any of them meant keeping notes by hand
and trusting them, because a run replaced the last one on screen and was then
gone.

So a run is kept. Not the telemetry and not the maps in full: the numbers that
identify a run and the numbers worth comparing, a few hundred bytes of it, in
the tool's own folder. Nothing is sent anywhere and nothing outside
`%LOCALAPPDATA%` is written.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import install

#: How many runs to keep. Enough to cover a week of testing a circuit without
#: the file ever being something anyone has to think about.
KEEP = 200

#: Bumped when a stored run gains or loses a field, so a file written by an
#: older build is dropped rather than half-read.
VERSION = 1


#: Where the record lives. Overridable for the same reason the map snapshot
#: directory is: a run made by the checks is not a run the user made, and it
#: has no business in their history.
DIR: "Path | None" = None


def path() -> Path:
    return (DIR or install.app_dir()) / "runs.json"


def load() -> list[dict]:
    """Every kept run, newest first. Never raises."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return []
    runs = data.get("runs")
    return runs if isinstance(runs, list) else []


def record(circuit: str, session_key: str, summary: list[dict],
           lap_s: float, laps_used: int, limits: dict) -> dict | None:
    """Keep one finished run. Returns the record, or None if it could not be."""
    entry = {
        "id": "%s-%d" % (circuit, int(time.time() * 1000)),
        "at": time.time(),
        "circuit": circuit,
        "session": session_key,
        "best_real_s": lap_s,
        "laps_used": laps_used,
        "limits": dict(limits),
        "strats": [
            {"strat": s.get("strat"), "label": s.get("label"),
             "lap_s": s.get("lap_s"), "deploy_mj": s.get("deploy_mj"),
             "harvest_mj": s.get("harvest_mj"),
             "store_start": s.get("store_start"), "store_end": s.get("store_end"),
             "zones": len(s.get("map") or ())}
            for s in summary],
    }
    runs = [entry] + load()
    try:
        path().parent.mkdir(parents=True, exist_ok=True)
        path().write_text(
            json.dumps({"version": VERSION, "runs": runs[:KEEP]}, indent=1),
            encoding="utf-8")
    except OSError:
        return None
    return entry


def find(run_id: str) -> dict | None:
    for run in load():
        if run.get("id") == run_id:
            return run
    return None


def compare(first_id: str, second_id: str) -> dict | None:
    """Two runs side by side, and what changed between them.

    Ordered oldest first whatever order they were asked for, so "quicker"
    always means the later run was quicker. Reading a comparison backwards is
    how a result gets reported the wrong way round.
    """
    a, b = find(first_id), find(second_id)
    if not a or not b:
        return None
    if a["at"] > b["at"]:
        a, b = b, a

    rows = []
    by_strat = {s["strat"]: s for s in a["strats"]}
    for later in b["strats"]:
        earlier = by_strat.get(later["strat"])
        if not earlier:
            continue
        rows.append({
            "strat": later["strat"],
            "label": later.get("label"),
            "before_s": earlier.get("lap_s"),
            "after_s": later.get("lap_s"),
            "delta_s": (later.get("lap_s") or 0) - (earlier.get("lap_s") or 0),
            "before_mj": earlier.get("deploy_mj"),
            "after_mj": later.get("deploy_mj"),
            "delta_mj": (later.get("deploy_mj") or 0) - (earlier.get("deploy_mj") or 0),
            "before_zones": earlier.get("zones"),
            "after_zones": later.get("zones"),
        })
    return {
        "a": a, "b": b, "rows": rows,
        "same_circuit": a["circuit"] == b["circuit"],
        "same_session": a["session"] == b["session"],
        "best_real_delta_s": (b.get("best_real_s") or 0) - (a.get("best_real_s") or 0),
    }
