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


def _fuel(path: Path) -> float:
    with open(path, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return sum(float(r["fuel"]) for r in rows) / len(rows)


def replay(case: Case, data_dir: Path, in_sample: bool) -> dict | None:
    """Simulate this lap on the map it was driven on. None if it cannot be."""
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
    result = sim26.simulate(
        case.splits, car, cal, shape, powertrain,
        start_store_kj=driven.store_kj[0],
        harvest_cap_kj=max(harvested, alloc.mj(session_name)) * 1000.0,
        settle=False,
        reset_zones=alloc.reset_zones_for(session_name, driven.length_m),
        speed_zones=alloc.speed_zones_for(driven.length_m))

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
