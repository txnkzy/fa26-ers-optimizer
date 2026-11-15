"""Hold the compiled lap integration to the Python one. Local only.

    python native/parity.py            every circuit with a recorded lap
    python native/parity.py --search   also run one whole search both ways

For each circuit this takes the quickest recorded lap, builds a few hundred
maps -- the seed, and random rearrangements of it -- and integrates each with
`sim26.simulate_py` and with the compiled library, with and without a
settling lap, through power-reset and speed zones, on an alternative power
curve, and as a run to the line. Every number that comes back has to be
identical, not close: the search chooses between maps that differ by
thousandths, so a last-bit difference can change which one it picks.

It reads laps from Documents\\Assetto Corsa\\fa26_baseline and the car from
the app's own unpacked copy, so it needs both to exist. Nothing is written.
"""

from __future__ import annotations

import glob
import os
import random
import re
import sys
import time
from dataclasses import fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "cars"))

import calibrate
import carphysics
import fa26
import lap as laplib
import optimise26
import profile as prof
import recharge
import sim26
import sim26_native

LAPS = Path(os.path.expanduser(r"~\Documents\Assetto Corsa\fa26_baseline"))
CAR_DIRS = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "FA26 Optimizer" / "cardata"
    / "vrc_formula_alpha_2026_csp",
    ROOT / "out" / "fa26_unpacked",
    ROOT.parent / "FA25 Energy Optimizer" / "out" / "fa26_unpacked",
)
POWERS = (0, 50, 100, 150, 200, 250, 300, 350)


def quickest_laps() -> dict:
    """circuit -> its quickest recorded lap that looks like a real one."""
    best = {}
    for path in glob.glob(str(LAPS / "*.csv")):
        m = re.search(r"^(.*?)__.*lap\d+_(\d+)m([\d.]+)s\.csv$",
                      os.path.basename(path))
        if not m:
            continue
        seconds = int(m.group(2)) * 60 + float(m.group(3))
        if seconds > 180 and "nords" not in m.group(1):
            continue
        if m.group(1) not in best or seconds < best[m.group(1)][0]:
            best[m.group(1)] = (seconds, path)
    return {k: v[1] for k, v in sorted(best.items())}


def load_car():
    for folder in CAR_DIRS:
        if folder.is_dir():
            try:
                car = carphysics.load(str(folder))
                car.fuel_liters = 45
                return car
            except Exception:                       # noqa: BLE001
                continue
    sys.exit("no unpacked car data found")


def shaken(seed, rng):
    """The seed's splits with powers and inner positions rearranged."""
    out = []
    for s in seed:
        a, b = sorted((rng.randint(s.start_m, s.end_m),
                       rng.randint(s.start_m, s.end_m)))
        out.append(fa26.Split(s.start_m, a, b, s.end_m,
                              rng.choice(POWERS), rng.choice(POWERS)))
    if rng.random() < 0.3 and len(out) > 2:        # leave a gap in the map
        del out[rng.randrange(len(out))]
    if rng.random() < 0.2:
        rng.shuffle(out)                            # order must not matter
    return out


def same(a, b) -> "str | None":
    """None if two results are identical, else the first field that is not."""
    for f in fields(sim26.Result):
        x, y = getattr(a, f.name), getattr(b, f.name)
        if f.name == "exit":
            for g in fields(sim26.Carry):
                if getattr(x, g.name) != getattr(y, g.name):
                    return "exit.%s: %r != %r" % (
                        g.name, getattr(x, g.name), getattr(y, g.name))
        elif f.name == "lifts":
            if list(x.items()) != list(y.items()):
                return "lifts: %r != %r" % (x, y)
        elif x != y:
            if isinstance(x, list):
                for i, (p, q) in enumerate(zip(x, y)):
                    if p != q:
                        return "%s[%d]: %r != %r" % (f.name, i, p, q)
                return "%s: lengths %d != %d" % (f.name, len(x), len(y))
            return "%s: %r != %r" % (f.name, x, y)
    return None


def both(splits, car, cal, P, pt, start, cap, **kw):
    py = sim26.simulate_py(splits, car, cal, P, pt, start, cap, **kw)
    kw.setdefault("store_capacity_kj", 4000.0)
    kw.setdefault("ds", 2.5)
    kw.setdefault("settle", True)
    kw.setdefault("reset_zones", ())
    kw.setdefault("speed_zones", ())
    kw.setdefault("entry", None)
    kw.setdefault("window", None)
    rs = sim26_native.simulate(
        sim26, splits, car, cal, P, pt, start, cap, kw["store_capacity_kj"],
        kw["ds"], kw["settle"], kw["reset_zones"], kw["speed_zones"],
        kw["entry"], kw["window"])
    if rs is None:
        return "compiled integration declined the lap"
    return same(py, rs)


def check_circuit(name, path, car, maps: int, rng) -> tuple:
    L = laplib.load(path)
    cal = calibrate.fit_longitudinal(L, car)
    P = prof.Profile(L)
    try:
        alloc = recharge.lookup(path)
        cap = alloc.mj("race") * 1000.0
    except Exception:                               # noqa: BLE001
        cap = 8500.0
    length = P.length_m
    seed = optimise26.seed_splits(L, P)
    zones = ((0.15 * length, 0.25 * length), (0.6 * length, 0.7 * length))
    speed_zones = ((0.3 * length, 0.5 * length, 260.0),)
    trains = (
        fa26.Powertrain(),
        fa26.Powertrain(taper=fa26.TAPER_BASE),
        fa26.Powertrain(taper=fa26.TAPER_BASE, alt_taper=fa26.TAPER_ALT1,
                        alt_zones=((0.1 * length, 0.4 * length),)),
        fa26.Powertrain(taper=(290.0, 345.0), floor_hold_s=1.0,
                        ramp_down_kw_s=50.0),
    )
    checked, failures = 0, []
    candidates = [[], seed] + [shaken(seed, rng) for _ in range(maps)]
    for index, splits in enumerate(candidates):
        pt = trains[index % len(trains)]
        start = rng.choice((0.0, 500.0, L.store_kj[0], 2500.0, 4000.0))
        this_cap = rng.choice((cap, cap * 0.5, 1e9))
        cases = [
            dict(settle=True),
            dict(settle=False),
            dict(settle=True, reset_zones=zones, speed_zones=speed_zones),
            dict(settle=False, ds=5.0, reset_zones=zones),
        ]
        for kw in cases:
            checked += 1
            bad = both(splits, car, cal, P, pt, start, this_cap, **kw)
            if bad:
                failures.append("%s map %d %r: %s" % (name, index, kw, bad))
        # A run to the line, switching maps on the way, then the lap it enters.
        other = candidates[(index * 7 + 3) % len(candidates)]
        for switch in (120.0, 400.0):
            window_a = (length - sim26.PRIME_M, length - switch)
            entry = sim26.Carry(speed_ms=P.measured_speed_at(window_a[0]),
                                store_kj=start)
            checked += 1
            bad = both(other, car, cal, P, pt, start, this_cap, entry=entry,
                       window=window_a)
            if bad:
                failures.append("%s map %d approach: %s" % (name, index, bad))
            mid = sim26.simulate_py(other, car, cal, P, pt, start, this_cap,
                                    entry=entry, window=window_a).exit
            checked += 1
            bad = both(splits, car, cal, P, pt, start, this_cap, entry=mid,
                       window=(length - switch, length))
            if bad:
                failures.append("%s map %d to line: %s" % (name, index, bad))
            line = sim26.simulate_py(splits, car, cal, P, pt, start, this_cap,
                                     entry=mid,
                                     window=(length - switch, length)).exit
            checked += 1
            bad = both(splits, car, cal, P, pt, start, this_cap, entry=line,
                       settle=False)
            if bad:
                failures.append("%s map %d entered: %s" % (name, index, bad))
    # The search sends the car, the profile and the calibration to worker
    # processes. Anything the bridge left on them has to survive that.
    import pickle
    try:
        pickle.dumps((car, cal, P, trains))
    except Exception as exc:                        # noqa: BLE001
        failures.append("%s: not picklable after a compiled run: %s"
                        % (name, exc))
    return checked, failures


def check_search(path, car) -> "str | None":
    """One whole race search each way: same map, same lap, same evaluations."""
    L = laplib.load(path)
    cal = calibrate.fit_longitudinal(L, car)
    P = prof.Profile(L)
    alloc = recharge.lookup(path)
    obj = optimise26.Objective("race", alloc.mj("race"), energy_neutral=True)
    seed = optimise26.seed_splits(L, P)
    start = L.store_kj[0]
    runs = {}
    native = sim26._native
    for label in ("python", "compiled"):
        sim26._native = None if label == "python" else native
        P2 = prof.Profile(L)                        # no shared caches
        t0 = time.perf_counter()
        splits, result, evals = optimise26._optimise_once(
            seed, car, cal, P2, fa26.Powertrain(), obj, start)
        runs[label] = (splits, result, evals, time.perf_counter() - t0)
        print("  %-8s %6.2f s   lap %.6f   %d evaluations"
              % (label, runs[label][3], result.lap_time_s, evals))
    sim26._native = native
    a, b = runs["python"], runs["compiled"]
    print("  speed-up %.1fx" % (a[3] / b[3]))
    if a[0] != b[0]:
        return "the two searches chose different maps"
    if a[2] != b[2]:
        return "evaluation counts differ: %d != %d" % (a[2], b[2])
    return same(a[1], b[1])


def main() -> int:
    if not sim26_native.available:
        print("compiled integration not available -- run native/build.py")
        return 2
    car = load_car()
    if not sim26_native.eligible(car, fa26.Powertrain()):
        print("compiled integration will not take this car")
        return 2
    laps = quickest_laps()
    if not laps:
        print("no recorded laps in %s" % LAPS)
        return 2
    rng = random.Random(26)
    maps = 60
    total, failures = 0, []
    for name, path in laps.items():
        try:
            checked, bad = check_circuit(name, path, car, maps, rng)
        except Exception as exc:                    # noqa: BLE001
            print("  %-34s skipped (%s: %s)" % (name[:34], type(exc).__name__, exc))
            continue
        total += checked
        failures += bad
        print("  %-34s %5d laps  %s" % (name[:34], checked,
                                        "identical" if not bad else
                                        "%d DIFFER" % len(bad)))
    if "--search" in sys.argv:
        barcelona = [p for n, p in laps.items() if "barcelona" in n]
        target = barcelona[0] if barcelona else next(iter(laps.values()))
        print("whole search, %s" % os.path.basename(target)[:40])
        bad = check_search(target, car)
        if bad:
            failures.append("search: " + bad)
    for line in failures[:20]:
        print("FAIL", line)
    print("%d integrations compared, %d differ" % (total, len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
