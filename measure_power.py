"""Read the car's speed taper out of telemetry, knowing what the map asked for.

    python measure_power.py

Article C5.2.8 caps deployment by speed, on a curve named per circuit in the
event notice. `cars/fa26.py` holds two of them, Standard and Overtake, and
falls back to Overtake at the ten circuits that name neither.

Reading the curve back out of recorded laps was tried once and abandoned, for
a stated reason: "the ceiling it measures is only ever the most the driver
happened to ask for". A lap where the map commanded 150 kW at 320 km/h says
nothing about what the car would have given.

That objection is answerable now `core/provenance.py` establishes which map
each lap ran. A sample counts only where the map commanded the car's maximum
*and* the demand latch had not already pulled the level down -- and what the
car delivers there is a ceiling the car chose.

What this reports is therefore a lower bound on the true curve, over whatever
speed range the recorded maps happen to ask for full power. Where no map ever
asked, it stays silent rather than guessing.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "core"), str(ROOT / "cars"), str(ROOT)]

import fa26
import install
import lap as laplib
import provenance
import recharge
import session as lapsession
import setupfile
import validate_model as V

#: Speed bin width, kph. Narrow enough to see a knee, wide enough to fill.
BIN_KPH = 5.0
#: A bin needs this many samples before its best is worth printing.
MIN_SAMPLES = 8
#: Below this the taper has nothing to say.
FLOOR_KPH = 200.0


def latched_demand(lap, splits, reset_zones) -> list[float]:
    """The power level the map is allowed to be asking for, sample by sample.

    The regulation caps the demand, and the cap only lifts when the driver
    comes off the throttle. A sample where the latch has already dropped the
    level is a sample where the map is the limit, not the car, so it cannot
    be used to measure the car.
    """
    out = [0.0] * len(lap)
    demand = 0.0
    on_throttle_s = 0.0
    for i in range(len(lap)):
        step = lap.t[i] - lap.t[i - 1] if i else 0.0
        if lap.gas[i] < 0.2:
            demand = on_throttle_s = 0.0
            continue
        on_throttle_s += step
        commanded, _clip = fa26.map_command_at(splits, lap.dist[i], lap.length_m)
        in_reset = any(a <= lap.dist[i] < b for a, b in reset_zones)
        if (on_throttle_s <= 1.0 or in_reset
                or lap.gas[i] < fa26.DEMAND_RESET_GAS):
            demand = commanded
        else:
            demand = min(demand, commanded)
        out[i] = demand
    return out


def collect(cases, data_dir) -> dict:
    """{curve name: {speed bin: [delivered kW]}} over every verified lap."""
    groups: dict = {}
    for case in cases:
        driven = laplib.load(case.path)
        alloc = recharge.lookup(str(case.path))
        curve = (alloc.curve_override if case.found.strat == 1
                 else alloc.curve_standard)
        name = curve or "(no curve named: %s)" % (
            "Overtake assumed" if case.found.strat == 1 else "Standard assumed")
        session = {1: "qualify", 2: "race"}.get(case.found.strat, "practice")
        want = latched_demand(driven, case.splits,
                              alloc.reset_zones_for(session, driven.length_m))
        bins = groups.setdefault(name, {})
        for i in range(len(driven)):
            if driven.gas[i] < 0.99 or driven.brake[i] > 0.01:
                continue
            if want[i] < fa26.MAX_DEPLOY_KW - 1.0:
                continue
            kph = driven.speed[i] * 3.6
            if kph < FLOOR_KPH:
                continue
            bins.setdefault(int(kph // BIN_KPH) * BIN_KPH, []).append(
                driven.deploy_kw[i])
    return groups


def main() -> int:
    folder = V.telemetry_dir()
    data_dir = install.ensure_unpacked(fa26.CAR_ID)
    item_map = setupfile.item_index_map(
        (data_dir / "setup.ini").read_text(encoding="utf-8"))
    cases, _refused = V.cases(folder, item_map)
    if not cases:
        print("no laps with a provable map, so nothing can be measured")
        return 2

    groups = collect(cases, data_dir)
    for name in sorted(groups):
        rows = [(k, v) for k, v in sorted(groups[name].items())
                if len(v) >= MIN_SAMPLES]
        if not rows:
            continue
        print()
        print("== %s   %d bins, %d samples"
              % (name, len(rows), sum(len(v) for _k, v in rows)))
        print("   %8s %8s %9s %9s %9s" % ("kph", "n", "best kW",
                                          "Standard", "Overtake"))
        for k, values in rows:
            mid = (k + BIN_KPH / 2) / 3.6
            print("   %8.0f %8d %9.0f %9.0f %9.0f"
                  % (k, len(values), max(values),
                     fa26.Powertrain(taper=fa26.TAPER_BASE).taper_limit_kw(mid),
                     fa26.Powertrain(taper=fa26.TAPER_OVERTAKE).taper_limit_kw(mid)))
        top = max(k for k, _v in rows) + BIN_KPH
        print("   no recorded map asks for full power above %.0f kph, so the"
              % top)
        print("   curve is unconstrained by measurement from there upwards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
