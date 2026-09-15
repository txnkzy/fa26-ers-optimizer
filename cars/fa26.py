"""FA26 deployment maps: the layout of a split and how it reaches the setup file.

Twelve maps of twenty-four splits, each split six values. Field names are the
car's own, taken from its setup.ini declarations:

    1 Split Start        metres
    2 Deploy End         metres
    3 Super-clip Start   metres (CLIP_OFF disables)
    4 Split End          metres
    5 Deploy Power       kW, 0-350
    6 Super-clip Power   kW, 0-350

So within a split the car deploys at Deploy Power from Split Start to Deploy
End, coasts, then harvests at Super-clip Power from Super-clip Start to Split
End. Deployment power is continuous, unlike the FA25's three fixed modes.

The four positions are genuinely independent. In 69 of the 71 splits across
VRC's six authored maps `clipStart` equals `deployEnd`, which makes it tempting
to collapse them into three positions -- but Albert Park uses the neutral gap
deliberately, coasting from 1607 to 1708 m before harvesting, and again from
3802 to 3940 m after deploying at full power. Collapsing them would discard a
control the reference maps rely on.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

import setupfile

CAR_ID = "vrc_formula_alpha_2026_csp"

MAPS = 12
SPLITS_PER_MAP = 24
POSITION_MAX = 50000
POWER_MAX = 350
CLIP_OFF = 50000          # Super-clip Start parked beyond the lap disables it


@dataclass
class Split:
    start_m: int
    deploy_end_m: int
    clip_start_m: int
    end_m: int
    deploy_kw: int
    clip_kw: int

    @classmethod
    def empty(cls) -> "Split":
        return cls(0, 0, CLIP_OFF, 0, 0, 0)

    @property
    def is_empty(self) -> bool:
        return self.end_m <= self.start_m and self.deploy_end_m <= self.start_m

    def clamped(self, track_length_m: float) -> "Split":
        """Keep positions inside the lap and in order, powers inside range."""
        hi = int(track_length_m)
        start = max(0, min(hi, int(round(self.start_m))))
        end = max(start, min(hi, int(round(self.end_m))))
        deploy_end = max(start, min(end, int(round(self.deploy_end_m))))
        clip_kw = max(0, min(POWER_MAX, int(round(self.clip_kw))))
        clip_start = int(round(self.clip_start_m))
        if clip_start >= CLIP_OFF or clip_kw <= 0:
            # An active split must keep start <= deployEnd <= clipStart <= end.
            # Parking clipStart out beyond the lap at CLIP_OFF breaks that
            # ordering, and the car rejects the split -- which is why every map
            # written this way was silently ignored and the car fell back to its
            # default deployment, on every track. VRC's own maps never do it:
            # they disable harvesting by setting the power to zero and leaving
            # clipStart at deployEnd, which is what this now does. CLIP_OFF is
            # still the internal marker, and still what an unused slot carries.
            clip_start = deploy_end
            clip_kw = 0
        else:
            clip_start = max(deploy_end, min(end, clip_start))
        return Split(
            start_m=start,
            deploy_end_m=deploy_end,
            clip_start_m=clip_start,
            end_m=end,
            deploy_kw=max(0, min(POWER_MAX, int(round(self.deploy_kw)))),
            clip_kw=clip_kw,
        )


def split_ids(map_index: int, split_index: int) -> list[str]:
    return [
        f"DEPLOYMENT_MAP_{map_index}_SPLIT_{split_index}_{field}"
        for field in range(1, 7)
    ]


def read_map(setup_text: str, mapping: dict[str, int], map_index: int) -> list[Split]:
    """Non-empty splits of one deployment map, in file order."""
    values = setupfile.read_values(setup_text)
    splits = []
    for s in range(1, SPLITS_PER_MAP + 1):
        raw = [values.get(mapping[i], 0) for i in split_ids(map_index, s)]
        split = Split(*raw)
        if not split.is_empty:
            splits.append(split)
    return splits


def write_map(setup_path, mapping: dict[str, int], map_index: int,
              splits: list[Split], track_length_m: float, backup: bool = True):
    """Write one deployment map, blanking the splits it does not use."""
    if len(splits) > SPLITS_PER_MAP:
        raise ValueError(
            f"{len(splits)} splits exceeds the {SPLITS_PER_MAP} the car supports")

    updates: dict[int, int] = {}
    for slot in range(1, SPLITS_PER_MAP + 1):
        split = (splits[slot - 1].clamped(track_length_m)
                 if slot <= len(splits) else Split.empty())
        for item_id, value in zip(split_ids(map_index, slot),
                                  (split.start_m, split.deploy_end_m,
                                   split.clip_start_m, split.end_m,
                                   split.deploy_kw, split.clip_kw)):
            updates[mapping[item_id]] = value

    return setupfile.write_values(setup_path, updates, backup=backup)


# ---------------------------------------------------------------- powertrain

MOTOR_EFFICIENCY = 0.93
FLOOR_KW = 200.0          # regulation minimum at throttle application
MAX_DEPLOY_KW = 350.0
MAX_HARVEST_KW = 350.0

# Measured off-throttle harvest rate. Coasting and braking both sit at about
# 336 kW, and coasting is the single largest source of energy on a lap -- more
# than braking and super-clipping combined -- so a model that only harvests
# under braking comes up around two thirds short.
#
# Confirmed: swept against the nine laps whose map is provably the one driven,
# with the per-lap cap lifted so the harvest model itself is on test, 336 kW
# is the minimum-error point -- +0.11 MJ bias, 0.20 MJ mean absolute. 330 and
# 344 are both worse. This constant was right; what was wrong was the throttle
# taper it was multiplied by.
HARVEST_OFF_THROTTLE_KW = 336.0

# The cutoff was set at 80 kph from three Spa laps, whose slowest corner sits
# below it -- so the sample could not see where the knee really is. Across
# every circuit recorded the p90 off-throttle harvest reads 0 kW at 60 kph,
# 256-283 at 70 and 311-336 at 80, flat from about 85 up. Cutting everything
# off below 80 threw away 0.43 MJ on a Monza lap, and more on the slow
# circuits -- Monaco, Hungaroring, Singapore -- which carry the largest
# recharge allocations to fill.
HARVEST_CUTOFF_KPH = 60.0
HARVEST_RAMP_KPH = 85.0

#: Harvest against throttle, measured with speed held to 150-260 kph so the
#: speed ceiling cannot confound it. The car stops regenerating between 60%
#: and 70% throttle, which is the other side of the manual's statement that
#: "above 60% throttle input, MGU-K positive power is applied smoothly up to
#: full throttle" (p.21). The old linear taper to zero at 99% throttle
#: credited 44/33/21/10% of the ceiling at 0.6/0.7/0.8/0.9 and manufactured
#: 0.67 MJ a lap at Barcelona -- 9% of modelled harvest -- that the car will
#: never bank. Since harvest is what limits how much a lap can deploy, that
#: inflated every plan the optimiser produced.
#: Throttle below which the MGU-K stops applying positive power, so a new
#: deployment demand may be established when it next rises. The manual puts
#: the arbitration knee at 60% throttle (p.21), and the telemetry agrees in
#: both directions: harvest is gone by 0.68 throttle, and on Barcelona lap 07
#: the driver lifts to 0.35 at 4231 m -- never below an "off throttle" test --
#: and the car immediately re-establishes demand and goes to 251 kW.
#: Resetting only on a full lift latched the demand at whatever the map
#: happened to command at the last real lift, which on that lap was zero.
DEMAND_RESET_GAS = 0.60

HARVEST_THROTTLE_CURVE = (
    (0.00, 1.000), (0.10, 0.973), (0.20, 0.927), (0.30, 0.817),
    (0.40, 0.693), (0.50, 0.534), (0.60, 0.083), (0.68, 0.000),
)


#: Delivered deploy power against throttle, measured with speed held to
#: 150-260 kph and the store not near empty, normalised to the p95 at full
#: throttle. The relationship is strongly concave -- 20% throttle already
#: buys 50% of the power -- which is the torque arbitration the manual
#: describes on p.21 blending ICE and MGU-K, not a proportional pedal.
#: Scaling deployment linearly by throttle is therefore wrong in both
#: directions at once: it under-delivers on a real part-throttle sample and
#: over-delivers when fed a 5 m slot's *maximum* throttle, which runs 1.35x
#: the mean. Feeding it the slot mean and this curve puts the part-throttle
#: deploy error back where it belongs.
DEPLOY_THROTTLE_CURVE = (
    (0.00, 0.000), (0.10, 0.393), (0.20, 0.500), (0.30, 0.597),
    (0.50, 0.604), (0.60, 0.708), (0.70, 0.774), (0.80, 0.927),
    (0.90, 0.998), (1.00, 1.000),
)


def deploy_throttle_frac(gas: float) -> float:
    """Fraction of the commanded deploy level delivered at this throttle."""
    pts = DEPLOY_THROTTLE_CURVE
    if gas <= pts[0][0]:
        return 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if gas <= x1:
            return y0 + (y1 - y0) * (gas - x0) / (x1 - x0)
    return 1.0


def harvest_throttle_frac(gas: float) -> float:
    """Fraction of the off-throttle harvest ceiling available at this throttle."""
    pts = HARVEST_THROTTLE_CURVE
    if gas <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if gas <= x1:
            return y0 + (y1 - y0) * (gas - x0) / (x1 - x0)
    return 0.0


def harvest_kw_at(speed_ms: float) -> float:
    """Off-throttle harvest ceiling at a given speed."""
    kph = speed_ms * 3.6
    if kph <= HARVEST_CUTOFF_KPH:
        return 0.0
    if kph >= HARVEST_RAMP_KPH:
        return HARVEST_OFF_THROTTLE_KW
    span = HARVEST_RAMP_KPH - HARVEST_CUTOFF_KPH
    return HARVEST_OFF_THROTTLE_KW * (kph - HARVEST_CUTOFF_KPH) / span

# Speed at which deploy power starts tapering and where it reaches zero, in kph.
# The manual gives a base curve and a raised overtake curve; overtake is always
# active in Practice and Qualifying. Neither knee has been measured yet -- the
# recorded laps top out around 333 kph and the authored maps never command more
# than 250 kW, so the taper is never the binding limit in the data. This is the
# one input still taken from the manual rather than telemetry.
TAPER_BASE = (290.0, 345.0)
TAPER_OVERTAKE = (337.0, 355.0)

#: The event notice names which ERS-K power curve applies, in Article C5.2.8:
#: one for normal running and one with Override active. Only the Base pair is
#: known numerically -- it matches the chart the FIA publishes, flat to 290 kph
#: and zero by 345 for Standard, flat to 337 and zero by 355 for Overtake.
#: Monaco runs a "Rev 1" pair and several events name an "Alt 1" curve for
#: specific sectors; neither has published numbers, so they fall back to Base
#: rather than being invented.
TAPER_CURVES = {
    "Base - Standard": TAPER_BASE,
    "Base - Overtake": TAPER_OVERTAKE,
}


def taper_for(curve: str | None,
              fallback: tuple = TAPER_OVERTAKE) -> tuple[float, float]:
    """Speed taper for a named FIA power curve.

    The fallback is the caller's to choose, and it matters. Ten circuits in
    the table name no curve for normal running -- Bahrain, Baku, Jeddah,
    Austin, Las Vegas, Lusail, Mexico, Singapore, Interlagos, Yas Marina --
    and every circuit that does name one names a Standard variant. Falling
    back to Overtake for all of them handed the race car 350 kW at 320 km/h,
    where Standard allows 175: Override power, in a strategy that never runs
    Override.

    Measured on a Bahrain lap, full throttle above 300 km/h delivered at most
    202 kW. Standard allows 222 there and Overtake 350, so the car is plainly
    on the conservative curve and the model was not.

    (Reading the curve out of the telemetry directly was tried and dropped:
    asked to recover the published curve on circuits that name one, it
    returned 335 km/h for Barcelona's 290 and could not read half of them at
    all. The ceiling it measures is only ever the most the driver happened to
    ask for.)
    """
    if not curve:
        return fallback
    if curve in TAPER_CURVES:
        return TAPER_CURVES[curve]
    return TAPER_BASE if curve.endswith("Standard") else TAPER_OVERTAKE


def taper_is_known(curve: str | None) -> bool:
    return curve in TAPER_CURVES


@dataclass
class Powertrain:
    """How a commanded power becomes power actually delivered.

    There is deliberately no `overtake` flag. One used to sit here and nothing
    ever read it, so `Powertrain(overtake=False)` returned the Overtake curve
    anyway -- a 291.7 kW against 31.8 kW difference at 340 kph, waiting for
    whoever trusted it. The curve is chosen by passing `taper`, from
    `taper_for(...)`, which is the only thing that has ever had an effect.
    """

    floor_kw: float = FLOOR_KW
    taper: tuple[float, float] = TAPER_OVERTAKE
    floor_hold_s: float = 1.0        # how long the 200 kW floor must be held
    #: How fast the regulated power level may fall, kW/s. This is NOT the
    #: setup's ERS_POWER_REDUCTION_RATE. That item is per-event at 50 or
    #: 100 kW/s and is still written to the car, but it does not govern the
    #: delivered-power reduction modelled here: measured from the raw
    #: deployment counter over 243 full-throttle reductions above 210 kph, the
    #: car falls at 100-215 kW/s with a hard floor at 101 -- including at
    #: Monza, whose setups were written with the item at 50. Feeding 50 into
    #: the model made a Monza replay 0.36% slow for no reason the car shows.
    ramp_down_kw_s: float = 100.0

    def taper_limit_kw(self, speed_ms: float) -> float:
        start, end = self.taper
        kph = speed_ms * 3.6
        if kph <= start:
            return MAX_DEPLOY_KW
        if kph >= end:
            return 0.0
        return MAX_DEPLOY_KW * (end - kph) / (end - start)

    def deploy_kw(self, commanded_kw: float, speed_ms: float,
                  full_throttle: bool = True) -> float:
        """Delivered deployment power.

        Measured behaviour from the Barcelona reference map: a split commanding
        150 kW delivered 199.2 kW, and one commanding 0 delivered 158.5 kW,
        because the car holds a 200 kW floor at throttle application and cannot
        cut power instantly. Commanding below the floor therefore does not save
        the energy a naive model would predict.
        """
        limit = self.taper_limit_kw(speed_ms)
        wanted = min(commanded_kw, MAX_DEPLOY_KW)
        if full_throttle and wanted < self.floor_kw:
            wanted = self.floor_kw
        return max(0.0, min(wanted, limit))

    def thrust_n(self, deploy_kw: float, speed_ms: float) -> float:
        return deploy_kw * 1000.0 * MOTOR_EFFICIENCY / max(speed_ms, 1.0)


def map_command_at(splits: list[Split], distance_m: float,
                   length_m: float) -> tuple[float, float]:
    """(deploy_kW, superclip_kW) commanded at a point on the lap."""
    d = distance_m % length_m
    for s in splits:
        if s.start_m <= d < s.end_m:
            if d < s.deploy_end_m:
                return s.deploy_kw, 0.0
            if d < s.clip_start_m:
                # The four positions are independent, and between deployEnd and
                # clipStart the car coasts. Reading clip power across the gap
                # harvested all 101 m of Albert Park's deliberate neutral run
                # at 250 kW -- a control VRC uses and this file documents, which
                # the lookup then threw away.
                return 0.0, 0.0
            return 0.0, s.clip_kw
    return 0.0, 0.0


def map_fit(lap, splits) -> tuple[float, float, float]:
    """(mean error kW, null-map error kW, skill) of a map against a driven lap.

    The error is mean |effective command - delivered| over every full-throttle
    sample, where the effective command is the deploy power in a deploy region
    and zero in a super-clip region.

    The skill term is what makes it a test rather than a number. Scored on its
    own the metric cannot fail: samples inside clip regions used to be dropped,
    so a map commanding nothing anywhere was compared against almost nothing
    and scored ~0. Monza's recharge map -- seventeen splits, every one
    commanding 0 kW -- scored 0.3 against laps driven on three different
    strats, a better "fit" than any genuine match in the corpus. Measuring the
    same lap against an empty map and reporting the improvement removes that:
    a map the car really followed scores +85% or better (VRC's own Barcelona
    and Red Bull Ring maps, on the laps driven with them, score +77 to +94%),
    and a map it did not scores near zero however small its raw error.
    """
    errs, null = [], []
    for i in range(len(lap)):
        if lap.gas[i] < 0.99 or lap.brake[i] > 0.01:
            continue
        deploy, clip = map_command_at(splits, lap.dist[i], lap.length_m)
        effective = deploy if clip <= 0 else 0.0
        errs.append(abs(effective - lap.deploy_kw[i]))
        null.append(abs(lap.deploy_kw[i]))
    if not errs:
        return float("nan"), float("nan"), float("nan")
    fit = sum(errs) / len(errs)
    base = sum(null) / len(null)
    return fit, base, (1.0 - fit / base) if base > 1e-9 else float("nan")


# The wheel's PU-mode switch, in the order setup_pu_modes.lut declares them:
# RACE AD1 AD2 FS FW IN ES Q K2 K2+ SLO SC T4 RS. Q is the qualifying mode and
# ES is energy-saving, which is what a recharge lap is for, so the three
# strategies map onto the three modes a driver would actually reach for.
PU_MODE_QUALIFY = 8       # "Q"
PU_MODE_RACE = 1          # "RACE"
PU_MODE_RECHARGE = 11     # "SLO"

#: Which mode on the wheel selects which strategy. The strategies live in
#: deployment maps 1, 2 and 3, but those numbers are an implementation detail
#: and saying "STRAT 1 is qualifying" next to a mode called RACE just reads as
#: a contradiction. Only the mode names matter to the driver.
PU_MODE_STRATEGY = {
    PU_MODE_QUALIFY: 1,       # Q   -> qualifying
    PU_MODE_RACE: 2,          # RACE -> race
    PU_MODE_RECHARGE: 3,      # SLO -> recharge
}


def _check_encoding(strat_number: int, splits, track_length_m: float) -> None:
    """Refuse to write a map the car will silently reject.

    An active split has to keep start <= deployEnd <= clipStart <= end. Writing
    clipStart out at CLIP_OFF to mean "no harvesting" breaks that, and the car
    does not complain -- it discards the map and runs its default deployment, so
    the tool looks like it is working while nothing it writes reaches the car.
    That went unnoticed across every track for as long as it took to notice a
    zero-deploy map deploying 9.88 MJ.

    Across 76 active splits in VRC's own Barcelona, Belgium and Austria setups,
    not one uses CLIP_OFF or breaks the ordering.
    """
    for index, split in enumerate(splits, start=1):
        s = split.clamped(track_length_m)
        if s.end_m == 0 and s.start_m == 0:
            continue
        if not (s.start_m <= s.deploy_end_m <= s.clip_start_m <= s.end_m):
            raise ValueError(
                f"strat {strat_number} split {index} is malformed and the car "
                f"would ignore the whole map: start={s.start_m} "
                f"deployEnd={s.deploy_end_m} clipStart={s.clip_start_m} "
                f"end={s.end_m}")


def snapshot_dir_for(telemetry_dir) -> "Path":
    """Where map snapshots live, beside the telemetry they explain."""
    d = Path(telemetry_dir) / "maps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot_for(telemetry_csv, telemetry_dir=None):
    """The map snapshot in force when a lap was recorded, or None.

    Telemetry files carry the session stamp the logger wrote them with
    (`<track>__<car>__YYYYMMDD_HHMMSS__lapNN_...`), and each snapshot is named
    with the stamp it was written at, so the right map is the newest snapshot
    at or before the session stamp.
    """
    csv_path = Path(telemetry_csv)
    directory = Path(telemetry_dir) if telemetry_dir else csv_path.parent
    stamp = None
    for part in csv_path.name.split("__"):
        if re.match(r"^\d{8}_\d{6}$", part):
            stamp = part
            break
    if stamp is None:
        return None
    best = None
    for candidate in sorted((directory / "maps").glob("*.ini")):
        head = candidate.name.split("__")[0]
        if re.match(r"^\d{8}_\d{6}$", head) and head <= stamp:
            best = candidate
    return best


def write_strategies(setup_path, mapping: dict[str, int],
                     strategies: "list[tuple[int, list[Split]]]",
                     track_length_m: float, allocation=None,
                     backup: bool = True, active: int = 2,
                     telemetry_dir=None):
    """Write several strategies at once, each into its own deployment map.

    `strategies` is a list of (strat number, splits). Strat n is pointed at
    deployment map n so the wheel switch selects them directly, and unused
    splits in each map are blanked. Optionally the per-lap recharge allocation
    is written too.
    """
    updates: dict[int, int] = {}

    for strat_number, splits in strategies:
        _check_encoding(strat_number, splits, track_length_m)
        if not 1 <= strat_number <= MAPS:
            raise ValueError(f"strat {strat_number} outside 1-{MAPS}")
        if len(splits) > SPLITS_PER_MAP:
            raise ValueError(
                f"strat {strat_number}: {len(splits)} splits exceeds {SPLITS_PER_MAP}")

        updates[mapping[f"STRAT_{strat_number}_DEPLOYMENT_MAP"]] = strat_number

        for slot in range(1, SPLITS_PER_MAP + 1):
            split = (splits[slot - 1].clamped(track_length_m)
                     if slot <= len(splits) else Split.empty())
            for item_id, value in zip(
                split_ids(strat_number, slot),
                (split.start_m, split.deploy_end_m, split.clip_start_m,
                 split.end_m, split.deploy_kw, split.clip_kw),
            ):
                updates[mapping[item_id]] = value

    # Point the wheel's PU-mode switch at the strategies just written.
    #
    # A STRAT is only reachable in the car through a PU mode that selects it,
    # and every one of the fourteen modes ships pointing at STRAT 1. Writing the
    # maps without repointing the modes left the mode switch inert -- RACE and Q
    # both gave STRAT 1 -- so two of the three strategies could not be selected
    # at all. VRC's own setups point Q at a different map for exactly this
    # reason.
    # How a strategy actually gets selected, established on track:
    #
    #   the base STRAT_MAP item picks the deployment map, and that works.
    #   the per-mode overrides (PU_MODE_n_STRAT_MAP + _ACTIVE) do not fire,
    #   whatever they are set to -- four laps on three different wheel modes all
    #   ran whichever map the base pointed at.
    #
    # So the overrides are explicitly disabled rather than left half-configured:
    # with every ACTIVE flag off, the base item is the only thing that decides,
    # and the wheel switch cannot silently override it. "STRAT Map" is an
    # ordinary setup item, so it can be changed in the pit setup screen:
    # 1 qualifying, 2 race, 3 recharge.
    # SoC Adaptation lets the car rewrite the deployment map on the fly to chase
    # a state-of-charge target. That is a good feature and a terrible one to
    # leave on while measuring whether the car follows the map it was given, so
    # every strat is pinned to a flat target with adaptation off.
    for strat_number in range(1, 13):
        for item, value in ((f"STRAT_{strat_number}_SOC_TARGET", 0),
                            (f"STRAT_{strat_number}_SOC_ADAPTATION", 0)):
            if item in mapping:
                updates[mapping[item]] = value

    for mode_index in range(1, 15):
        flag = f"PU_MODE_{mode_index}_STRAT_MAP_ACTIVE"
        if flag in mapping:
            updates[mapping[flag]] = 0

    written = {n for n, _ in strategies}
    if "STRAT_MAP" in mapping and active in written:
        updates[mapping["STRAT_MAP"]] = active
    if "STRAT_MAP_EDIT" in mapping and active in written:
        updates[mapping["STRAT_MAP_EDIT"]] = active

    if allocation is not None:
        import recharge
        updates.update(recharge.updates_for(allocation, mapping))
        # The per-lap recharge limit belongs to the session, not to the strat:
        # the car applies the practice figure in a practice session whichever
        # map is selected. Since testing happens in practice, the practice limit
        # is pointed at the session the active strategy is actually for, so a
        # race map is held to the race allowance rather than the more generous
        # practice one.
        session = {1: "qualify", 2: "race", 3: "race"}.get(active)
        item = "ERS_REGEN_MAX_MJ_P"
        if session and item in mapping:
            updates[mapping[item]] = getattr(allocation, session)

    result = setupfile.write_values(setup_path, updates, backup=backup)

    # Keep a dated copy beside the telemetry. Without one there is no way to
    # know afterwards which map a recorded lap was driven on, because this
    # function overwrites the setup in place: every Monza lap on disk was
    # replayed against a map written after the lap was recorded, and the
    # resulting "+3.1% race / +13.7% recharge" figures compared a simulated
    # map against a lap driven on a different one. Snapshots are small and
    # cost nothing next to a 2 MB telemetry file.
    if telemetry_dir is not None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = snapshot_dir_for(telemetry_dir) / f"{stamp}__{Path(setup_path).name}"
        target.write_bytes(Path(setup_path).read_bytes())

    return result
