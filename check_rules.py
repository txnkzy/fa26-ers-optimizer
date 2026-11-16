"""Hold the search's last steps to what the car can execute. Local only.

    python check_rules.py

`check_app.py` solves whatever session is newest, which is a lap someone
drove. The maps that broke `open_the_lap` came from laps the AI drove: it is
flat the instant it touches the throttle, so every long straight is cut into
several splits, and the lap's last split begins mid-straight. Raising that
split alone, to let the opening deploy, asked for more power under throttle
-- which the car refuses -- on every map of a Mexico solve, and nothing here
would have noticed.

So this builds that shape on purpose, on every circuit with a recorded lap:
a run to the line covered by two splits, the driver on the throttle across
the boundary between them. Whatever `open_the_lap` returns has to be a map
the car will run.

It reads laps from Documents\\Assetto Corsa\\fa26_baseline. Nothing is written.
"""

from __future__ import annotations

import glob
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "cars"))

import fa26
import lap as laplib
import optimise26
import profile as prof

LAPS = Path(os.path.expanduser(r"~\Documents\Assetto Corsa\fa26_baseline"))

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + name + ("  " + detail if detail else ""))
    if not ok:
        fails.append(name)


def quickest_laps() -> dict:
    best = {}
    for path in glob.glob(str(LAPS / "*.csv")):
        m = re.search(r"^(.*?)__.*lap\d+_(\d+)m([\d.]+)s\.csv$",
                      os.path.basename(path))
        if not m:
            continue
        seconds = int(m.group(2)) * 60 + float(m.group(3))
        if seconds > 180:
            continue
        if m.group(1) not in best or seconds < best[m.group(1)][0]:
            best[m.group(1)] = (seconds, path)
    return {k: v[1] for k, v in sorted(best.items())}


def run_to_the_line(seed, profile):
    """(opening, last, cut): a lap-opening split the driver is on the throttle
    in, the last split of the lap they are flat through, and a point inside
    it they are still on the throttle at -- or None if the lap has no such."""
    knee = fa26.DEMAND_RESET_GAS
    on = lambda d: profile.gas[profile._slot(d)] >= knee
    opening = seed[0]
    if opening.start_m > optimise26.OPENING_M or not on(opening.start_m + profile.ds):
        return None
    for index in range(len(seed) - 1, 0, -1):
        split = seed[index]
        if split.end_m - split.start_m < 200:
            continue
        cut = int((split.start_m + split.end_m) / 2)
        if on(cut) and on(cut - profile.ds) and on(split.end_m - profile.ds):
            return 0, index, cut
        return None
    return None


def build(seed, opening, last, cut, power, first_half_ends):
    """Nothing commanded, but for the opening and the run to the line in two."""
    blank = [fa26.Split(s.start_m, s.start_m, fa26.CLIP_OFF, s.end_m, 0, 0)
             for s in seed]
    o, l = seed[opening], seed[last]
    blank[opening] = fa26.Split(o.start_m, o.end_m, o.end_m, o.end_m, power, 0)
    blank[last:last + 1] = [
        fa26.Split(l.start_m, first_half_ends, cut, cut, power, 0),
        fa26.Split(cut, l.end_m, l.end_m, l.end_m, power, 0)]
    return blank


def main() -> int:
    laps = quickest_laps()
    if not laps:
        print("no recorded laps in %s" % LAPS)
        return 2
    tried = 0
    for name, path in laps.items():
        try:
            lap = laplib.load(path)
            profile = prof.Profile(lap)
            seed = optimise26.seed_splits(lap, profile)
        except Exception as exc:                    # noqa: BLE001
            print("  skip %s (%s)" % (name, type(exc).__name__))
            continue
        shape = run_to_the_line(seed, profile)
        if shape is None:
            continue
        opening, last, cut = shape
        tried += 1

        # The driver flat across the boundary, both halves deploying.
        base = build(seed, opening, last, cut, 100, cut)
        if not optimise26.is_executable(base, profile):
            print("  skip %s (the shape is not drivable to begin with)" % name)
            continue
        opened = optimise26.open_the_lap(base, profile)
        bad = optimise26.illegal_increases(opened, profile)
        check("%s: an opened lap is one the car will run" % name[:24], not bad,
              "refused at %s" % (bad,) if bad else
              "run to the line at %s kW, opening at %s"
              % (sorted({s.deploy_kw for s in opened[last:last + 2]}),
                 opened[opening].deploy_kw))
        halves = opened[last:last + 2]
        check("%s: the raise is carried back along the straight" % name[:24],
              opened is base or halves[0].deploy_kw == halves[1].deploy_kw,
              "%s then %s kW" % (halves[0].deploy_kw, halves[1].deploy_kw))

        # The first half stops deploying short of the boundary: it cannot be
        # raised into holding the level, so the lap is left as it was.
        short = build(seed, opening, last, cut, 100,
                      seed[last].start_m + (cut - seed[last].start_m) // 2)
        left = optimise26.open_the_lap(short, profile)
        check("%s: a lap that cannot be opened is left alone" % name[:24],
              not optimise26.illegal_increases(left, profile)
              or bool(optimise26.illegal_increases(short, profile)))
    check("at least one circuit offered the shape", tried > 0, "%d did" % tried)
    print("FAILED: " + ", ".join(fails) if fails else "ALL PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
