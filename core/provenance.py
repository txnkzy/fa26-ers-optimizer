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


#: Suffix of the file the logger writes beside each lap, holding the
#: deployment maps that were live while it was recorded.
SIDECAR_SUFFIX = ".map"


@dataclass
class Attribution:
    """Which map a lap ran, and how sure that is."""

    splits: list          #: the map itself, empty when unattributable
    strat: int            #: the strategy slot the wheel was on, 1-based
    skill: float          #: `fa26.map_fit` improvement over an empty map
    source: str           #: the snapshot it came from, for the record
    reason: str = ""      #: why it failed, when `splits` is empty
    #: True when the logger recorded the map with the lap, rather than it
    #: having been matched to one afterwards.
    recorded: bool = False

    def __bool__(self) -> bool:
        return bool(self.splits)


def read_sidecar(lap_path) -> dict | None:
    """The maps the logger wrote beside this lap, or None if it wrote none.

    The file is the ground truth this module used to have to reconstruct. It
    carries every strategy slot the wheel sat on during the lap and the map
    each of them held, read out of the setup as the lap finished -- so nothing
    here has to guess which setup was loaded or which strategy was live.
    """
    path = Path(lap_path).with_suffix(SIDECAR_SUFFIX)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    out: dict = {"maps": {}, "strats": [], "dominant": 0, "logger": 0,
                 "length_m": 0.0, "path": path, "setup_file": "", "probe": "",
                 "invalid_samples": 0, "boundary": ""}
    section = None
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].upper()
            if section.startswith("MAP_"):
                out["maps"].setdefault(int(section[4:]), [])
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().upper(), value.strip()
        if section == "LAP":
            if key == "STRATS":
                out["strats"] = [int(v) for v in value.split(",") if v.strip()]
            elif key == "DOMINANT_STRAT":
                out["dominant"] = int(float(value))
            elif key == "LOGGER":
                out["logger"] = int(float(value))
            elif key == "SETUP_FILE":
                out["setup_file"] = value
            elif key == "SETUP_PROBE":
                out["probe"] = value
            elif key == "INVALID_SAMPLES":
                # The logger records invalidated laps rather than discarding
                # them -- it was throwing away the push lap after a recharge
                # lap where a corner had been cut. The count is kept so the
                # decision to exclude one can still be made, visibly, here.
                out["invalid_samples"] = int(float(value))
            elif key == "LAP_BOUNDARY":
                # "game" when the lap was cut at the game's own line, "spline"
                # on a CSP build without a lap counter, where a track whose
                # spline starts away from the line gives laps the timing
                # screen never showed.
                out["boundary"] = value
            elif key == "TRACK_LENGTH_M":
                out["length_m"] = float(value)
        elif section and section.startswith("MAP_") and key.startswith("SPLIT_"):
            numbers = [int(float(v)) for v in value.split(",")]
            if len(numbers) == 6:
                out["maps"][int(section[4:])].append(fa26.Split(*numbers))
    # A sidecar is worth having if it names the setup even when it carries no
    # map of its own -- naming the file is the harder half, and on this build
    # it is the only half the game will give up.
    return out if (out["maps"] or out["setup_file"]) else None


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


def _rewritten_since(setup_file, lap_path) -> bool:
    """Whether a setup file has been written since this lap was saved.

    False when either time cannot be read, which leaves the fit score to
    decide as it always did.
    """
    try:
        return Path(setup_file).stat().st_mtime > Path(lap_path).stat().st_mtime
    except (OSError, TypeError):
        return False


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

    # Written down at the time beats worked out afterwards, every time. When
    # the logger recorded the map with the lap there is nothing to decide:
    # this is the map, and no snapshot is consulted at all.
    recorded = read_sidecar(getattr(lap, "path", None) or "")
    if recorded and recorded["maps"].get(strat):
        splits = recorded["maps"][strat]
        _fit, _null, skill = fa26.map_fit(lap, splits)
        return Attribution(splits, strat, skill if skill == skill else 1.0,
                           recorded["path"].name, "", recorded=True)

    # Failing the map itself, the name of the setup the car loaded. That is
    # the thing the old rule was guessing at, and the logger knows it for
    # certain -- `ac.onSetupFile` hands it over as the game loads it.
    #
    # It is still checked rather than trusted, because a setup file is not
    # immutable: this tool overwrites them, and a lap driven before a write
    # names a file whose contents have since changed. `map_fit` settles that
    # in one pass -- a map the car really followed explains the power it
    # delivered, and one that has been overwritten since does not.
    #
    # Mostly. A file written since the lap was saved is not a record of it at
    # all, and a fit score can pass one by chance: three Baku laps of 21/09
    # named the server's shared setup, which the server rewrote on 23/09, and
    # the new contents cleared the bar at 0.35-0.37 against a floor of 0.35.
    # Such a lap goes to the snapshots instead, which hold what this tool
    # wrote and when.
    if (recorded and recorded["setup_file"] and item_map
            and not _rewritten_since(recorded["setup_file"],
                                     getattr(lap, "path", None))):
        named = Path(recorded["setup_file"])
        try:
            splits = fa26.strat_map(
                named.read_text(encoding="utf-8", errors="replace"),
                item_map, strat)
        except Exception:
            splits = []
        if splits:
            _fit, _null, skill = fa26.map_fit(lap, splits)
            if skill != skill or skill >= MIN_SKILL:
                return Attribution(splits, strat,
                                   skill if skill == skill else 1.0,
                                   named.name, "", recorded=True)

    best = Attribution([], strat, float("-inf"), "", "")
    for snap in snapshots:
        try:
            text = Path(snap).read_text(encoding="utf-8", errors="replace")
            splits = fa26.strat_map(text, item_map, strat)
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
    found = [attribute(lap, snaps, item_map) for lap in laps]
    if not snaps and not any(f.recorded for f in found):
        return []
    known = [f for f in found if f]
    strats = {f.strat for f in found if f.strat}

    out = []
    if known and all(f.recorded for f in known) and len(known) == len(found):
        return []                  # every lap brought its own map; nothing to say
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
