"""Establishing which deployment map a recorded lap was actually driven on.

Comparing a lap time against a simulated map only means anything if the map
being simulated is the one the car was running. Two things were being assumed
here, and both were wrong often enough to poison every accuracy figure this
project has quoted.

**Which strategy.** The lap file records the live strategy on the wheel, in
`mguk_delivery`, and the driver changes it constantly -- of 61 laps whose map
could be identified at all, 22 were driven on STRAT 2 and 3 on STRAT 3, while
every one of them was being replayed against STRAT 1. A Bahrain session driven
on the race map, scored against the qualifying map, is not a measurement of
anything.

**Which snapshot.** The rule was "the newest map snapshot written before this
lap". That is right only if the setup reached the car immediately and nothing
else was written in between, and neither holds: Assetto Corsa reads a setup
when the session loads, so a map written mid-session does not apply until a
restart, and a user switching between setups leaves snapshots interleaved in
time order that have nothing to do with each other.

So neither is assumed. `fa26.map_fit` already scores a map against a lap by
how well its commands explain the delivered power, and reports the improvement
over an empty map -- a map the car really followed scores well above half,
and a map it did not scores near zero however small its raw error. That
function existed, was documented, and had no callers. This module is the
caller: it picks the snapshot whose map best explains the lap, and refuses the
lap outright when nothing explains it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cars"))

import fa26
import session as lapsession

#: `map_fit` skill below which a map is not credibly the one the lap ran. A
#: genuine match scores 0.45 or better across this corpus and VRC's own setups
#: score 0.77 to 0.94; a mismatch scores at or below zero, usually far below.
#: The gap is wide enough that the exact cut barely matters.
MIN_SKILL = 0.35

#: A lap may not spend more than this fraction of its samples on a strategy
#: other than the dominant one. A switch mid-lap means no single map describes
#: it, and the lap cannot be attributed at all.
MAX_OFF_STRAT = 0.02

#: Below this the lap deployed essentially nothing, whatever the map says. The
#: logger has written such files -- two Austin laps share a time to the
#: millisecond and one of them carries an empty deployment counter.
MIN_DEPLOYED_MJ = 0.05


@dataclass
class Attribution:
    """Which map a lap ran, and how sure that is."""

    splits: list          #: the map itself, empty when unattributable
    strat: int            #: the strategy slot the wheel was on, 1-based
    skill: float          #: `fa26.map_fit` improvement over an empty map
    source: str           #: the snapshot it came from, for the record
    reason: str = ""      #: why it failed, when `splits` is empty

    def __bool__(self) -> bool:
        return bool(self.splits)


def driven_strat(lap) -> int | None:
    """The strategy slot this lap was driven on, or None if it changed.

    The channel is the live selection on the wheel, so a lap where the driver
    reached for a different map part-way through has no single answer. A few
    samples of another value at the very start are the previous lap's tail
    bleeding across the line, and are tolerated.
    """
    values = getattr(lap, "strat", None)
    if not values:
        return None
    counts: dict[int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    best = max(counts, key=lambda k: counts[k])
    if (len(values) - counts[best]) / len(values) > MAX_OFF_STRAT:
        return None
    if not 1 <= best <= fa26.MAPS:
        return None
    return best


def attribute(lap, snapshots, item_map, strat: int | None = None) -> Attribution:
    """The map among these snapshots that best explains this lap.

    Every snapshot is scored, not only those written before the lap: a
    snapshot's timestamp says when the tool wrote the file, which is not when
    the car read it, and ordering on it alone has attributed laps to maps they
    never ran. Behaviour decides instead, and the timestamp is not consulted.
    """
    if lap.totals()[0] < MIN_DEPLOYED_MJ:
        return Attribution([], 0, 0.0, "", "the lap recorded no deployment")

    if strat is None:
        strat = driven_strat(lap)
    if strat is None:
        return Attribution([], 0, 0.0, "",
                           "the strategy was changed during the lap")

    best = Attribution([], strat, float("-inf"), "", "")
    for snap in snapshots:
        try:
            text = Path(snap).read_text(encoding="utf-8", errors="replace")
            splits = fa26.read_map(text, item_map, strat)
        except Exception:
            continue
        if not splits:
            continue
        _, _, skill = fa26.map_fit(lap, splits)
        if skill != skill:                      # NaN: nothing to score against
            continue
        if skill > best.skill:
            best = Attribution(splits, strat, skill, Path(snap).name)

    if not best.splits:
        return Attribution([], strat, 0.0, "",
                           "no map snapshot was recorded for this circuit")
    if best.skill < MIN_SKILL:
        return Attribution([], strat, best.skill, best.source,
                           "no recorded map explains this lap's deployment")
    return best


def for_lap(lap, path, telemetry_dir, item_map, circuit: str) -> Attribution:
    """Attribution for one lap file, from the snapshots beside it."""
    return attribute(lap, lapsession.snapshots(telemetry_dir, circuit), item_map)


def describe(laps, paths, telemetry_dir, item_map, circuit: str) -> list[str]:
    """What is known about the maps a set of laps was driven on, in words.

    The old check for this compared timestamps: it asked whether the setup had
    been written after the laps were saved, and warned if so. That answers a
    question about files, not about the car -- Assetto Corsa reads a setup when
    the session loads, so a write can land before a lap and still not have
    reached it, and a lap can predate a write and have been driven on exactly
    the map it produced.

    What the deployment actually did settles it, so that is what is asked here.
    """
    snaps = lapsession.snapshots(telemetry_dir, circuit)
    if not snaps:
        return []
    found = [attribute(lap, snaps, item_map) for lap in laps]
    known = [f for f in found if f]
    strats = {f.strat for f in found if f.strat}

    out = []
    if not known:
        out.append("None of these laps match any deployment map this tool has "
                   "recorded, so a lap time from them is not a comparison "
                   "with anything it can plan. Save a map, restart the "
                   "session, then drive.")
    elif len(known) < len(found):
        out.append("%d of these %d laps match a map this tool recorded; the "
                   "rest were driven on something else."
                   % (len(known), len(found)))
    if len(strats) > 1:
        out.append("These laps were not all driven on the same strategy — %s. "
                   "The lap time to beat is whichever of them you mean."
                   % ", ".join("STRAT %d" % s for s in sorted(strats)))
    return out
