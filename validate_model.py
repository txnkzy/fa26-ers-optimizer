"""Replay every recorded lap through the model and report the error.

    python validate_model.py                  # per-circuit table
    python validate_model.py --json out.json  # machine-readable, for diffing
    python validate_model.py --in-sample      # fit on the lap being replayed

A lap counts only if the map it was driven on is known: a setup snapshot
written before the lap, with deployment in it. The map is read back out of
that snapshot and simulated against the lap's own start-of-lap charge, and
the simulated lap time is compared with the one the game recorded.

Two ways to fit the car, and the difference between them is the whole point:

  holdout (the default)  fit drag, braking and traction from the *other* laps
                         of the session, never the lap being replayed
  --in-sample            fit from the lap itself

In-sample is what produced the README's old accuracy figures, and it flatters
the model for the same reason any curve fits its own points best: it has
already seen the answer. A change that improves in-sample error and worsens
holdout error has not improved the model, it has overfitted -- which has
happened here before, on the pooling tolerance, and cost 0.236 s on track.

Holdout is the number to quote, and the number to watch after any change to
`core/calibrate.py`, `core/sim26.py`, `core/profile.py` or `cars/fa26.py`.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "core"), str(ROOT / "cars")]

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


def telemetry_dir() -> Path:
    """Where the logger writes, honouring whatever the app has been told."""
    sys.path.insert(0, str(ROOT))
    import fa26_app
    return fa26_app.baseline_dir()


class Case:
    """One replayable lap: the file, its session, and the map it was driven on."""

    def __init__(self, path: Path, key: str, circuit: str, found, siblings):
        self.path = path
        self.key = key
        self.circuit = circuit
        self.found = found                #: provenance.Attribution
        self.splits = found.splits
        self.siblings = siblings          # every lap of the session, this one included

    @property
    def actual_s(self) -> float:
        return lapsession.parse(self.path)[2]


def cases(folder: Path, item_map) -> tuple[list[Case], dict[str, int]]:
    """Every lap whose deployment map can be established from its behaviour.

    Nothing here trusts a timestamp. Each lap is scored against every map
    snapshot recorded for its circuit, on the strategy the lap itself says the
    wheel was on, and kept only when one of them actually explains the power
    that was delivered. That rejects most of what the old rule accepted --
    laps driven on STRAT 2 and replayed against STRAT 1, laps driven before a
    setup was ever loaded, and two files the logger wrote with no deployment
    in them at all.
    """
    out: list[Case] = []
    refused: dict[str, int] = {}
    for key, paths in sorted(lapsession.group(folder.glob("*.csv")).items()):
        circuit = recharge.normalise("__".join(key.split("__")[:-1]))
        snaps = lapsession.snapshots(folder, circuit)
        if not snaps:
            refused["no map snapshot for the circuit"] = (
                refused.get("no map snapshot for the circuit", 0) + len(paths))
            continue
        for path in sorted(paths):
            try:
                driven = laplib.load(path)
            except Exception:
                refused["unreadable"] = refused.get("unreadable", 0) + 1
                continue
            trouble = laplib.problem(driven)
            if trouble:
                key_t = trouble.split(" -- ")[0].split(",")[0]
                refused[key_t] = refused.get(key_t, 0) + 1
                continue
            # Only laps the tool would actually model. A recharge lap is
            # deliberately slow -- it gives up time to put charge back -- and
            # replaying one against a profile built from push laps measures
            # something nothing here has ever claimed: an Austin 1:53 came
            # back as a predicted 1:40, which is the model correctly saying
            # "you could have driven that faster" and not an error in it.
            if path not in lapsession.select(paths):
                refused["a lap the pooling rule would not model"] = refused.get(
                    "a lap the pooling rule would not model", 0) + 1
                continue
            found = provenance.attribute(driven, snaps, item_map)
            if not found:
                refused[found.reason] = refused.get(found.reason, 0) + 1
                continue
            out.append(Case(path, key, circuit, found, list(paths)))
    return out, refused


#: Setup items two maps may differ in and still be a comparison of maps: the
#: deployment maps, and the ERS settings written alongside them.
ERS_PREFIXES = ("DEPLOYMENT_MAP_", "ERS_", "STRAT_MAP")


def car_setup(path, item_map, cache: dict) -> dict | None:
    """Every value in a setup file except its ERS settings, keyed by name.

    Two maps driven in one session are only a comparison of maps if nothing
    else about the car changed. Baku's first session this week put a
    server-shared setup against the driver's own, with a pit stop between and
    the driver still finding a second and a half a lap -- a difference of
    2.2 s that no map explains, and which alone took the benchmark from 0.213
    to 0.389 s. Every controlled A/B is two strats of one file, or files
    copied from one base with only the maps rewritten, and passes this.
    """
    if path is None:
        return None
    key = str(path)
    if key not in cache:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            cache[key] = None
            return None
        names = {index: name for name, index in item_map.items()}
        values, section = {}, None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif section and line.upper().startswith("VALUE="):
                name = section
                if section.startswith("CUSTOM_SCRIPT_ITEM_"):
                    try:
                        name = names.get(int(section.rsplit("_", 1)[1]), section)
                    except ValueError:
                        pass
                if not str(name).startswith(ERS_PREFIXES):
                    values[name] = line.split("=", 1)[1].strip()
        cache[key] = values
    return cache[key]


def arrival(entry: dict, item_map):
    """The map the car came across the line on, as (splits, switch_m), or None.

    What a lap can deploy before its first corner depends on the demand it
    carries across the line, and that demand was set by whichever map was live
    the last time the driver got back on the power -- which need not be the map
    the lap is driven on. Austin lap02 of the first A/B deployed 1.37 MJ in its
    opening only because the lap before ran a map whose final split asked for
    300 kW against this map's 200; the laps that followed a recharge lap, with
    the wheel turned at the line, carried nothing and deployed nothing.

    So the previous lap is read, and the strategy that matters is the one live
    while the throttle was coming back on for the final corner -- the floor-hold
    window after the last time it crossed the knee. Not the strategy just before
    the last switch: turning the rotary from 3 to 1 passes through 2 for a
    fraction of a second, and taking that for the arrival map credited laps
    with 300 kW they never carried.

    When the previous lap cannot be established -- no file for it, no recorded
    setup, or a store that does not carry over, which is a trip to the pits
    nobody logged -- the lap is taken to have arrived carrying nothing. That is
    what every such lap in the corpus did: the first laps of four Austin
    sessions and three laps after unlogged pit visits all deployed 0-1 kJ
    before their first lift, whatever their own map asked for at either end.
    Assuming instead that they arrived on their own map handed some of them an
    opening the car refused, and made the model's differences worse.
    """
    nothing = ([], 0.0)
    path = Path(entry["path"])
    try:
        number = int(path.name.split("__lap")[-1][:2])
    except ValueError:
        return nothing
    previous = sorted(path.parent.glob(
        path.name.split("__lap")[0] + "__lap%02d_*.csv" % (number - 1)))
    if not previous:
        return nothing
    try:
        before = laplib.load(previous[0])
    except Exception:
        return nothing
    lap = entry["lap"]
    if abs(before.store_kj[-1] - lap.store_kj[0]) > 0.05 * laplib.STORE_CAPACITY_KJ:
        return nothing
    sidecar = provenance.read_sidecar(previous[0])
    if not sidecar or not sidecar.get("setup_file"):
        return nothing
    knee = fa26.DEMAND_RESET_GAS
    last = None
    for i in range(1, len(before.gas)):
        if before.gas[i - 1] < knee <= before.gas[i]:
            last = i
    if last is None:
        return nothing
    hold = fa26.Powertrain().floor_hold_s
    window = [before.strat[i] for i in range(last, len(before.t))
              if before.t[i] - before.t[last] <= hold and 1 <= before.strat[i] <= fa26.MAPS]
    if not window:
        return nothing
    established = max(set(window), key=window.count)
    strat = entry["found"].strat
    if established == strat:
        return entry["found"].splits, 0.0
    try:
        text = Path(sidecar["setup_file"]).read_text(encoding="utf-8", errors="replace")
        came = fa26.read_map(text, item_map, established)
    except Exception:
        return nothing
    return came, 0.0


def _fuel(path: Path) -> float:
    with open(path, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return sum(float(r["fuel"]) for r in rows) / len(rows)


_ITEM_MAPS: dict = {}
_SETUPS: dict = {}


def replay(case: Case, data_dir: Path, in_sample: bool) -> dict | None:
    """Simulate this lap on the map it was driven on. None if it cannot be."""
    item_map = _ITEM_MAPS.get(str(data_dir))
    if item_map is None:
        item_map = _ITEM_MAPS[str(data_dir)] = setupfile.item_index_map(
            (Path(data_dir) / "setup.ini").read_text(encoding="utf-8"))
    driven = laplib.load(case.path)
    if laplib.problem(driven):
        return None

    if in_sample:
        fit_laps = [driven]
    else:
        # The other laps of the session, put through the same pooling rule the
        # app uses rather than taken whole.
        #
        # A qualifying run is a *mixed* session by construction -- recharge
        # lap, push lap, recharge lap, push lap -- and fitting a push lap on
        # its own session's recharge laps builds the car from a 1:55 and then
        # asks it to reproduce a 1:39. It came back 1:48. That is not the model
        # being wrong, it is the harness pooling laps the app never would, and
        # it only showed up once somebody drove the protocol properly.
        others = [p for p in case.siblings if p != case.path]
        # Only laps on the same car. A session can hold a setup change -- Baku
        # this week went from a server-shared setup to the driver's own after a
        # pit stop, anti-roll bars and all -- and fitting one on the other
        # builds a car nobody drove. Laps whose setup was never recorded
        # cannot be checked and are kept, as before.
        mine = car_setup((provenance.read_sidecar(case.path) or {}).get("setup_file"),
                         item_map, _SETUPS)
        if mine is not None:
            same = []
            for p in others:
                theirs = car_setup((provenance.read_sidecar(p) or {}).get("setup_file"),
                                   item_map, _SETUPS)
                if theirs is None or theirs == mine:
                    same.append(p)
            others = same
        loaded = []
        for p in lapsession.select(others) if others else []:
            try:
                other = laplib.load(p)
            except Exception:
                continue
            if not laplib.problem(other):
                loaded.append(other)
        if not loaded:
            return None                    # nothing to fit on but itself
        # And nothing the app would pool with this lap. Madrid on 30/09 was a
        # 1:48 out lap and a 1:37 push lap: fitted on the out lap alone the
        # push lap replays 10% slow, which measures the harness, not the
        # model -- the app would never build a car from a lap eleven seconds
        # slower than the one it is planning for.
        if min(l.lap_time_s for l in loaded) > case.actual_s * lapsession.TOLERANCE:
            return None
        fit_laps = loaded

    car = carphysics.load(data_dir)
    car.fuel_liters = _fuel(case.path)
    cal = calibrate.fit_longitudinal(fit_laps, car)
    # The speed ceiling is the driver's line, and it belongs to the same set of
    # laps as the fit: taking it from the replayed lap would leak its answer.
    shape = prof.Profile(fit_laps if len(fit_laps) > 1 else fit_laps[0])

    # The allowance and the reset windows are session-scoped, so which one
    # applies follows the strategy the lap was actually driven on rather than
    # the qualifying figure being applied to everything.
    session_name = {1: "qualify", 2: "race", 3: "practice"}.get(
        case.found.strat, "qualify")
    alloc = recharge.lookup(str(case.path))
    _, harvested = driven.totals()
    powertrain = fa26.Powertrain(taper=fa26.taper_for(alloc.curve_override))
    zones = dict(reset_zones=alloc.reset_zones_for(session_name, driven.length_m),
                 speed_zones=alloc.speed_zones_for(driven.length_m))
    cap_kj = max(harvested, alloc.mj(session_name)) * 1000.0
    # Replay the lap the way it was actually entered: the demand it carried
    # across the line was set on whatever map was live at the final corner.
    state = None
    came = arrival({"path": case.path, "lap": driven, "found": case.found},
                   item_map) if item_map else None
    if came is not None:
        state = sim26.run_to_line(
            came[0], case.splits, came[1], car, cal, shape, powertrain,
            start_store_kj=driven.store_kj[0], harvest_cap_kj=cap_kj, **zones)
        state.speed_ms = driven.speed[0]
        state.store_kj = driven.store_kj[0]
    result = sim26.simulate(
        case.splits, car, cal, shape, powertrain,
        start_store_kj=driven.store_kj[0], harvest_cap_kj=cap_kj,
        settle=False, entry=state, **zones)

    actual = case.actual_s
    return {
        "circuit": case.circuit,
        "session": case.key.split("__")[-1],
        "lap": case.path.name.split("__")[-1],
        "actual_s": actual,
        "predicted_s": result.lap_time_s,
        "error_s": result.lap_time_s - actual,
        "error_pct": (result.lap_time_s - actual) / actual * 100.0,
        "deployed_mj": result.deployed_mj,
        "driven_deployed_mj": driven.totals()[0],
        "fit_laps": len(fit_laps),
        "strat": case.found.strat,
        "map_skill": case.found.skill,
        "map_source": case.found.source,
        "map_recorded": case.found.recorded,
        "top_kph": max(shape.cap) * 3.6 if getattr(shape, "cap", None) else 0.0,
    }


def summarise(rows: list[dict]) -> dict:
    """Per-circuit and overall error, on absolute percentage."""
    by_circuit: dict[str, list[dict]] = {}
    for row in rows:
        by_circuit.setdefault(row["circuit"], []).append(row)
    circuits = {}
    for name, group in sorted(by_circuit.items()):
        errs = [abs(r["error_pct"]) for r in group]
        signed = [r["error_pct"] for r in group]
        circuits[name] = {
            "laps": len(group),
            "mean_abs_pct": statistics.fmean(errs),
            "worst_abs_pct": max(errs),
            "bias_pct": statistics.fmean(signed),
        }
    allerr = [abs(r["error_pct"]) for r in rows]
    return {
        "circuits": circuits,
        "laps": len(rows),
        "mean_abs_pct": statistics.fmean(allerr) if allerr else 0.0,
        "worst_abs_pct": max(allerr) if allerr else 0.0,
        "bias_pct": statistics.fmean([r["error_pct"] for r in rows]) if rows else 0.0,
    }


def report(summary: dict, rows: list[dict], mode: str) -> str:
    lines = ["", "  %s replay, %d laps" % (mode, summary["laps"]), ""]
    lines.append("  %-22s %5s %9s %9s %9s" % ("circuit", "laps", "mean", "worst", "bias"))
    for name, s in sorted(summary["circuits"].items(),
                          key=lambda kv: -kv[1]["mean_abs_pct"]):
        lines.append("  %-22s %5d %8.2f%% %8.2f%% %+8.2f%%"
                     % (name, s["laps"], s["mean_abs_pct"],
                        s["worst_abs_pct"], s["bias_pct"]))
    lines += ["", "  %-22s %5d %8.2f%% %8.2f%% %+8.2f%%"
              % ("ALL", summary["laps"], summary["mean_abs_pct"],
                 summary["worst_abs_pct"], summary["bias_pct"]), ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", metavar="PATH", help="write every replay as JSON")
    ap.add_argument("--in-sample", action="store_true",
                    help="fit on the replayed lap (flatters the model)")
    ap.add_argument("--circuit", help="only circuits whose key contains this")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    folder = telemetry_dir()
    if not folder.is_dir():
        print("no telemetry at %s" % folder)
        return 2
    data_dir = install.ensure_unpacked(fa26.CAR_ID)
    item_map = setupfile.item_index_map(
        (data_dir / "setup.ini").read_text(encoding="utf-8"))

    found, refused = cases(folder, item_map)
    rows = []
    for case in found:
        if args.circuit and args.circuit not in case.circuit:
            continue
        try:
            row = replay(case, data_dir, args.in_sample)
        except Exception as exc:               # a lap the model cannot take
            if not args.quiet:
                print("  skipped %s: %s" % (case.path.name[:40], exc))
            continue
        if row:
            rows.append(row)

    if not rows:
        print("no replayable laps found")
        return 2

    summary = summarise(rows)
    mode = "in-sample" if args.in_sample else "holdout"
    print(report(summary, rows, mode))
    recorded = sum(1 for r in rows if r.get("map_recorded"))
    print("  %d of %d laps carried the map the logger recorded; %d had to be "
          "matched to a snapshot by behaviour." % (recorded, len(rows),
                                                   len(rows) - recorded))
    print()
    if refused and not args.quiet:
        print("  laps that could not be attributed to a map:")
        for reason, count in sorted(refused.items(), key=lambda kv: -kv[1]):
            print("    %4d  %s" % (count, reason))
        print()
    if args.json:
        Path(args.json).write_text(
            json.dumps({"mode": mode, "summary": summary, "laps": rows}, indent=1),
            encoding="utf-8")
        print("  %d replays written to %s" % (len(rows), args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
