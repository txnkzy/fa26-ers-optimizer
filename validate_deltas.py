"""Score the model on what the optimiser actually uses: differences between maps.

    python validate_deltas.py                   # per-comparison table
    python validate_deltas.py --json out.json   # machine-readable, for diffing

`validate_model.py` asks whether the model can reproduce a lap on the map it
was driven on, and it can, to 0.75%. The optimiser never asks that. It asks
which of two maps is quicker and by how much, and that is a different question
with a different answer:

    cota_v2      predicted new map 1.224 s quicker than map B; drove +0.005 s
    map B        predicted 100.154 s for a map driven in 98.794 s

A model can be excellent on the first question and poor on the second. The
error that matters for choosing a map is the part that *differs* between two
maps, and an offset shared by every lap of a session -- the track rubbering in,
a heavier fuel load, the driver finding a tenth -- cancels out of it and is
rightly ignored here.

What counts as a comparison
---------------------------
Two maps driven in the same session, each lap matched to its map through the
setup file the logger recorded and checked against the power the car actually
delivered, so a file overwritten since cannot pass. Recharge laps (strat 3)
are not timed laps and are left out, as are laps the logger recorded badly.

How each lap is predicted
-------------------------
The car is fitted and the speed ceiling built from laps of the same circuit
driven in *other* sessions. Nothing from the session being judged is shown to
the model, so a map whose laps went faster through a corner cannot lend that
speed to the other map's prediction -- the leak that made pooled Austin laps
look like they vindicated a ceiling they did not. Each lap is then simulated on
its own map from the charge it actually started with, so a battery that drained
across a run is an input the model has to handle rather than noise.

Two scores
----------
**Whole-lap difference**: for each pair of maps in a session, the mean
predicted difference against the mean measured one. Honest, but a single lap
carries a tenth or more of driver scatter.

**Segment difference**: the same, cut into 500 m pieces and timed off the
logger's own clock rather than by integrating speed, which disagreed with the
recorded lap by up to 0.2 s. Where two maps agree the driver repeats a segment
to about 0.02 s, so this resolves effects a whole lap cannot.
"""

from __future__ import annotations

import argparse
import itertools
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "core"), str(ROOT / "cars"), str(ROOT)]

import calibrate
import carphysics
import fa26
import install
import lap as laplib
import profile as prof
import provenance
import recharge
import session as lapsession
import setupfile
import sim26
import validate_model

#: Length of the pieces a lap is cut into for the segment score.
SEGMENT_M = 500.0

#: Laps from other sessions to fit on, at most. Enough to average out one bad
#: lap; not so many that a circuit driven for weeks is dominated by old pace.
FIT_LAPS = 12


def timed_laps(folder: Path, item_map) -> dict:
    """Every lap matched to its map through the recorded setup, by session."""
    sessions: dict = {}
    for key, paths in sorted(lapsession.group(folder.glob("*.csv")).items()):
        circuit = key.split("__")[0]
        for path in paths:
            if provenance.read_sidecar(path) is None:
                continue
            try:
                lap = laplib.load(path)
            except Exception:
                continue
            if laplib.problem(lap):
                continue
            found = provenance.attribute(lap, [], item_map)
            # The recorded setup route only. A lap matched by behaviour alone
            # was matched to the map that best explains its deployment, and
            # judging the model on maps chosen for fitting the laps is circular.
            if not found or not found.recorded or found.strat == 3:
                continue
            identity = (found.source, found.strat)
            sessions.setdefault((circuit, key), []).append(
                {"path": path, "lap": lap, "found": found, "map": identity})
    return sessions


def fit_for(circuit: str, exclude_key: str, folder: Path):
    """Laps of this circuit from other sessions, pooled the way the app pools."""
    groups = lapsession.group(folder.glob("%s__*.csv" % circuit))
    chosen = []
    for key, paths in groups.items():
        if key == exclude_key:
            continue
        for path in lapsession.select(paths):
            try:
                lap = laplib.load(path)
            except Exception:
                continue
            if not laplib.problem(lap):
                chosen.append(lap)
    chosen.sort(key=lambda l: l.lap_time_s)
    return chosen[:FIT_LAPS]


def times_at(dist, speed, edges):
    """Seconds at each edge, integrated along a simulated trace."""
    out, elapsed, j = [], 0.0, 0
    for k in range(1, len(dist)):
        while j < len(edges) and edges[j] <= dist[k]:
            span = max(dist[k] - dist[k - 1], 1e-9)
            f = (edges[j] - dist[k - 1]) / span
            v = (speed[k - 1] + speed[k]) * 0.5
            out.append(elapsed + f * span / max(v, 1.0))
            j += 1
        v = (speed[k - 1] + speed[k]) * 0.5
        elapsed += (dist[k] - dist[k - 1]) / max(v, 1.0)
    while len(out) < len(edges):
        out.append(elapsed)
    return out


def clock_at(lap, edges):
    """Seconds at each edge off the logger's own clock, interpolated."""
    out, i = [], 0
    for edge in edges:
        while i + 1 < len(lap.dist) and lap.dist[i + 1] < edge:
            i += 1
        if i + 1 >= len(lap.dist):
            out.append(lap.t[-1])
            continue
        span = max(lap.dist[i + 1] - lap.dist[i], 1e-9)
        f = min(1.0, max(0.0, (edge - lap.dist[i]) / span))
        out.append(lap.t[i] + f * (lap.t[i + 1] - lap.t[i]))
    return out


arrival = validate_model.arrival


def predict(entry: dict, fit_laps, data_dir: Path, edges, item_map=None) -> dict:
    """Simulate one lap on its own map, arriving the way it actually arrived."""
    lap, found = entry["lap"], entry["found"]
    car = carphysics.load(data_dir)
    car.fuel_liters = validate_model._fuel(entry["path"])
    cal = calibrate.fit_longitudinal(fit_laps, car)
    shape = prof.Profile(fit_laps if len(fit_laps) > 1 else fit_laps[0])
    kind = {1: "qualify", 2: "race"}.get(found.strat, "qualify")
    alloc = recharge.lookup(str(entry["path"]))
    _dep, harvested = lap.totals()
    powertrain = fa26.Powertrain(taper=fa26.taper_for(alloc.curve_override))
    zones = dict(reset_zones=alloc.reset_zones_for(kind, lap.length_m),
                 speed_zones=alloc.speed_zones_for(lap.length_m))
    cap_kj = max(harvested, alloc.mj(kind)) * 1000.0
    came = arrival(entry, item_map) if item_map is not None else None
    state = None
    if came is not None:
        before_map, switch_m = came
        state = sim26.run_to_line(
            before_map, found.splits, switch_m, car, cal, shape, powertrain,
            start_store_kj=lap.store_kj[0], harvest_cap_kj=cap_kj,
            approach_m=max(sim26.PRIME_M, switch_m + 50.0), **zones)
        # The demand is what the approach decides. Speed and charge were
        # measured, so they are taken as measured.
        state.speed_ms = lap.speed[0]
        state.store_kj = lap.store_kj[0]
    result = sim26.simulate(
        found.splits, car, cal, shape, powertrain,
        start_store_kj=lap.store_kj[0], harvest_cap_kj=cap_kj,
        settle=False, entry=state, **zones)
    sim_t = times_at(result.dist, result.speed, edges)
    real_t = clock_at(lap, edges)
    return {
        "lap": entry["path"].name.split("__")[-1].replace(".csv", ""),
        "map": "%s strat %d" % entry["map"],
        "actual_s": lap.lap_time_s,
        "predicted_s": result.lap_time_s,
        "start_pct": 100.0 * lap.store_kj[0] / laplib.STORE_CAPACITY_KJ,
        "seg_actual": [b - a for a, b in zip(real_t, real_t[1:])],
        "seg_predicted": [b - a for a, b in zip(sim_t, sim_t[1:])],
    }


def compare(rows_a, rows_b) -> dict:
    """Mean difference B minus A, measured and predicted, whole lap and per piece."""
    mean = statistics.fmean
    real = mean(r["actual_s"] for r in rows_b) - mean(r["actual_s"] for r in rows_a)
    pred = (mean(r["predicted_s"] for r in rows_b)
            - mean(r["predicted_s"] for r in rows_a))
    n = min(len(r["seg_actual"]) for r in rows_a + rows_b)
    seg = []
    for k in range(n):
        ra = mean(r["seg_actual"][k] for r in rows_b) - mean(r["seg_actual"][k] for r in rows_a)
        pa = (mean(r["seg_predicted"][k] for r in rows_b)
              - mean(r["seg_predicted"][k] for r in rows_a))
        seg.append((ra, pa))
    return {"measured_s": real, "predicted_s": pred, "error_s": pred - real,
            "segments": seg}


def run(folder: Path) -> dict:
    data_dir = install.ensure_unpacked(fa26.CAR_ID)
    item_map = setupfile.item_index_map(
        (data_dir / "setup.ini").read_text(encoding="utf-8"))
    out = {"comparisons": [], "noise": []}
    for (circuit, key), entries in timed_laps(folder, item_map).items():
        maps = sorted({e["map"] for e in entries})
        if len(maps) < 2:
            continue
        fit_laps = fit_for(circuit, key, folder)
        if len(fit_laps) < 2:
            continue
        length = min(e["lap"].length_m for e in entries)
        edges = [x * SEGMENT_M for x in range(int(length // SEGMENT_M) + 1)] + [length]
        rows = {}
        for e in entries:
            rows.setdefault(e["map"], []).append(
                predict(e, fit_laps, data_dir, edges, item_map))
        for a, b in itertools.combinations(maps, 2):
            c = compare(rows[a], rows[b])
            c.update({"circuit": circuit, "session": key.split("__")[-1],
                      "a": "%s strat %d" % a, "b": "%s strat %d" % b,
                      "laps_a": [r["lap"] for r in rows[a]],
                      "laps_b": [r["lap"] for r in rows[b]]})
            out["comparisons"].append(c)
        # Laps on the same map: what the measured difference is when the map
        # is not the cause. That is the floor any segment score is judged on.
        for m, rs in rows.items():
            for r1, r2 in itertools.combinations(rs, 2):
                for x1, x2, p1, p2 in zip(r1["seg_actual"], r2["seg_actual"],
                                          r1["seg_predicted"], r2["seg_predicted"]):
                    out["noise"].append(((x2 - x1), (p2 - p1)))
    return out


def score(result: dict) -> dict:
    comps = result["comparisons"]
    if not comps:
        return {}
    whole = [c["error_s"] for c in comps]
    pairs = [p for c in comps for p in c["segments"]]
    seg_err = [p - r for r, p in pairs]
    signs = [(r > 0) == (p > 0) for r, p in pairs if abs(r) > 0.03]
    noise = [abs(x - p) for x, p in result["noise"]]
    return {
        "comparisons": len(comps),
        "whole_mean_abs_s": statistics.fmean(abs(e) for e in whole),
        "segments": len(pairs),
        "segment_rms_s": (statistics.fmean(e * e for e in seg_err)) ** 0.5,
        "segment_sign_agreement": (sum(signs) / len(signs)) if signs else float("nan"),
        "segment_noise_s": statistics.fmean(noise) if noise else float("nan"),
    }


def report(result: dict) -> str:
    lines = []
    for c in result["comparisons"]:
        lines.append("%s %s" % (c["circuit"], c["session"]))
        lines.append("   A %-34s %s" % (c["a"], ", ".join(c["laps_a"])))
        lines.append("   B %-34s %s" % (c["b"], ", ".join(c["laps_b"])))
        lines.append("   B - A   measured %+7.3f s   predicted %+7.3f s   error %+7.3f s"
                     % (c["measured_s"], c["predicted_s"], c["error_s"]))
    s = score(result)
    if s:
        lines.append("")
        lines.append("  %d comparisons: whole-lap difference out by %.3f s on average"
                     % (s["comparisons"], s["whole_mean_abs_s"]))
        lines.append("  %d segments: difference out by %.3f s rms, direction right %.0f%% "
                     "of the time where it moved more than 0.03 s"
                     % (s["segments"], s["segment_rms_s"],
                        100 * s["segment_sign_agreement"]))
        lines.append("  same map, different lap: segments disagree by %.3f s -- the floor"
                     % s["segment_noise_s"])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", help="write the full result here")
    args = parser.parse_args(argv)
    result = run(validate_model.telemetry_dir())
    print(report(result))
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"score": score(result), **result}, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
