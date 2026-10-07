"""Deployment-map optimiser for the FA26.

A whole lap simulates in about 10 ms, so candidate maps are evaluated directly
rather than through a precomputed table and dynamic program. The search is a
hill climb over the four adjustable numbers in each split: where deployment
ends, where harvesting starts, and the two power levels.

Split boundaries themselves are seeded from the driver's throttle trace and left
fixed. VRC's own maps place boundaries at throttle transitions, and moving them
as well makes the search far larger for little gain.
"""

from __future__ import annotations

import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cars"))

import fa26
import sim26

POWER_STEP = 50.0
POWERS = [0.0, 50.0, 100.0, 150.0, 200.0, 250.0, 300.0, 350.0]


#: What a lift in a sweeper costs a map, in seconds per kph. Lifting into a
#: fast corner because the map arrived faster than it can be taken flat is
#: harder to drive and less repeatable: through Sepang T5-T6 the laps taken
#: flat on the limit averaged 8.90 s within 0.08 s of each other, the laps that
#: arrived fast and lifted 8.94-9.25 s with 0.15-0.23 s between laps on the
#: same map. The search treats the lift as a cost it will pay for only when
#: the gain is clearly larger, so energy goes where the car can stay flat.
LIFT_S_PER_KPH = 0.01

#: Lift the model sees in a sweeper even on a map the driver took flat: the
#: speed ceiling comes from several laps and is never perfectly smooth. Map C
#: at Sepang, flat through T5-T6 on every lap, reads as 12 kph.
LIFT_TOLERANCE_KPH = 10.0


def lift_cost_s(result, baseline: dict) -> float:
    """Seconds charged for the lifts a map forces beyond those it cannot avoid.

    `baseline` is the lift in each sweeper with no deployment at all: a corner
    the engine alone arrives at too fast needs that lift whatever the map does.
    """
    over = 0.0
    for corner, kph in result.lifts.items():
        extra = kph - baseline.get(corner, 0.0) - LIFT_TOLERANCE_KPH
        if extra > 0.0:
            over += extra
    return over * LIFT_S_PER_KPH


@dataclass
class Objective:
    """What a strategy is trying to achieve."""

    name: str
    harvest_cap_mj: float
    energy_neutral: bool = False       # must end the lap with what it started
    maximise_store: bool = False       # fill the store; lap time only breaks ties
    one_shot: bool = False             # a push lap: arrive full, leave empty
    #: Starting charges to score against, as fractions of the store, beyond
    #: the one the strategy assumes. A qualifying map is planned for a full
    #: battery because it is meant to follow a recharge lap -- but arrive at
    #: 78% instead, as a real Spa lap did, and a map tuned only for the best
    #: case falls apart faster than one that was not. VRC's own Spa map gives
    #: up 0.72 s between a full store and 2500 kJ where the solver's gave up
    #: 1.06; scoring the mean across charges instead cut that to 0.48 and beat
    #: VRC at every level. Costs one extra simulation per charge per candidate.
    charge_fractions: tuple = ()
    #: The lowest the store may go, as a fraction, before a plan is refused.
    #:
    #: The race search used to set this to a quarter, on the reasoning that a
    #: repeating lap neutral at zero is still neutral and leaves nothing in
    #: hand. Measured across seven circuits it costs **a second a lap**, and
    #: not because a quarter is too much -- 5%, 10% and 15% all land within
    #: 0.13 s of 25%, while no floor at all is 0.99 s quicker than any of them.
    #: The cost is the constraint existing, not its size, because this is a
    #: rejection rather than a preference: a map that touches the floor once is
    #: discarded whole, and a 4 MJ battery cannot cycle 8 MJ of allowance in a
    #: lap without going low. The floor was capping how much energy could move.
    #:
    #: Its own justification does not survive either. The worry was that the
    #: first lap off a full battery would be quick and every lap after it slow,
    #: which is true -- 0.17 to 0.98 s, measured -- but the settling lap already
    #: makes the reported time the repeating one, and the repeating lap with no
    #: floor is still a second quicker than the floored map. What was left is a
    #: reserve for attacking, and this tool hands the driver three maps: the
    #: reserve is STRAT 1, not a quarter of the battery held back every lap.
    #:
    #: What it does cost is a lap arrived at with half the planned charge:
    #: +0.08 to +0.66 s, once, before the store settles again.
    min_store_fraction: float = 0.0
    #: With `energy_neutral`, what the lap must end with relative to what it
    #: started with, in kJ: negative spends that much of the battery a lap,
    #: positive banks it. The attack, defend, push and save maps, each matched
    #: to the car's own SoC Target for its strat.
    store_delta_kj: float = 0.0

    def score(self, result, start_store_kj: float) -> float | None:
        """Lower is better. None means the plan is not admissible."""
        lift_s = getattr(result, "lift_s", 0.0)
        if result.harvested_mj > self.harvest_cap_mj + 1e-6:
            return None
        if self.min_store_fraction > 0.0 and                 result.store_min_kj < STORE_CAPACITY_KJ * self.min_store_fraction - 1e-6:
            return None
        if self.energy_neutral and (result.store_end_kj < result.store_start_kj
                                    + self.store_delta_kj - 1e-6):
            # Neutrality must hold across the measured lap, not against the
            # charge the driver happened to be carrying. Anchoring it to the
            # recorded value pinned a Monza race lap at 99% full, where there
            # was no room to harvest and therefore nothing to spend: 2.08 MJ
            # deployed with 1.49 MJ of harvest refused. The settling lap is
            # free to find a lower operating level; what matters is that the
            # lap repeats.
            return None
        if self.maximise_store:
            # A recharge lap exists to hand the next lap a full store, so the
            # store dominates and lap time only separates plans that bank the
            # same amount. Weighting time against energy instead just invents an
            # exchange rate that has no basis.
            return -result.store_end_kj * 1000.0 + result.lap_time_s + lift_s
        return (result.robust_time_s or result.lap_time_s) + lift_s


def seed_splits(lap, profile, min_length_m: float = 60.0) -> list[fa26.Split]:
    """Tile the lap at throttle transitions.

    Full-throttle stretches become deployment splits; everything else becomes a
    harvesting split, which is what the car does there anyway.
    """
    n = len(profile.cap)
    ds = profile.ds
    #: What the split *does* is decided by full throttle -- that is where the
    #: car will accept a super-clip command.
    flat = [profile.gas[i] >= 0.99 for i in range(n)]
    #: Where the split *starts* is decided by the arbitration knee. A boundary
    #: placed at the 0.99 crossing sits well above it, so the demand is already
    #: latched by the time the split begins and the car refuses the new level:
    #: every seed boundary was an illegal power increase, and once candidates
    #: are held to what the car can execute that capped the whole map at zero.
    #: Cutting at the knee is also what the manual advises -- "set the start of
    #: the split to some meters before the exit into it, or before you would be
    #: getting back on throttle" (p.9).
    #:
    #: Placing the boundary at the *earliest* pickup across a session rather
    #: than the typical one was tried and does not pay, which was a surprise.
    #: The asymmetry that motivated it is real -- shifting VRC's Barcelona map
    #: 20 m early is free while 20 m late costs 0.68 s, because the car refuses
    #: the whole power step -- and a map built from one lap really does have
    #: 3-5 steps refused on every other lap of its own session. But moving
    #: every boundary early also moves the power delivery early, into where it
    #: is worth less, and that costs more than the refusals do. Holding
    #: everything else equal, on achieved lap time averaged over a session:
    #: median throttle 100.839 / 78.224, earliest-fifth 101.085 / 78.047,
    #: near-unanimous 102.133 / 79.845 (Madrid / Barcelona). The middle is a
    #: wash and the conservative end is clearly worse, so the typical pickup
    #: stands. `Profile.consensus` is still built, and is worth reporting.
    on = [profile.gas[i] >= fa26.DEMAND_RESET_GAS for i in range(n)]

    bounds = [0]
    for i in range(1, n):
        if on[i] != on[i - 1]:
            bounds.append(i)
    bounds.append(n)

    # Merge short runs until the tiling fits the car's 24 slots, rather than
    # tiling at a fixed granularity and cutting off whatever does not fit.
    # `splits[:24]` dropped the tail of the lap outright: Madrid wanted 29
    # splits, so the last 778 m -- 15% of the circuit, including the run to the
    # line -- carried no command at all, and the solver's own qualifying map
    # came out 2.6 s slower than the lap the driver had actually driven on the
    # same track. Raising the merge length instead keeps the whole lap covered
    # and spends the slots on the longest features.
    limit = min_length_m
    while True:
        merged = [bounds[0]]
        for b in bounds[1:]:
            if (b - merged[-1]) * ds >= limit:
                merged.append(b)
        if len(merged) > 1:
            merged[-1] = n
        else:
            merged = [0, n]
        if len(merged) - 1 <= fa26.SPLITS_PER_MAP:
            break
        limit *= 1.15

    splits = []
    for a, b in zip(merged, merged[1:]):
        start, end = int(a * ds), int(b * ds)
        full = flat[min(a, n - 1)]
        if full:
            splits.append(fa26.Split(start, end, end, end, 200, 0))
        else:
            splits.append(fa26.Split(start, start, start, end, 0, 0))
    return splits


MAX_FULL_SPLIT_M = 500


def subdivide(splits, profile, budget: int = fa26.SPLITS_PER_MAP):
    """Cut long full-throttle splits so each covers a narrower speed band.

    A split carries one deploy power and one harvest power, and force is power
    over speed. Spread a single value from 120 to 320 kph and it shoves hard at
    the bottom of the straight and barely pushes at the top -- and there is no
    way to express what the car actually wants, which is to deploy at the slow
    end and harvest at the fast end of the same straight.

    Spa seeded 17 splits of the 24 available with one of them 1840 m long,
    covering Eau Rouge's exit and the whole Kemmel straight together. The spare
    capacity is free, so spend it on the longest full-throttle stretches first.
    """
    out = list(splits)
    unsplittable: set[tuple[int, int]] = set()
    while len(out) < budget:
        longest, index = 0, None
        for i, sp in enumerate(out):
            span = sp.end_m - sp.start_m
            if (sp.start_m, sp.end_m) in unsplittable:
                continue
            if span > max(longest, MAX_FULL_SPLIT_M) and                     profile.full_throttle_at(sp.start_m + profile.ds):
                longest, index = span, i
        if index is None:
            break
        sp = out[index]
        cut = _speed_midpoint(sp, profile)
        if cut is None:
            # Skip this one and keep going; an earlier version broke out of the
            # loop here, which left the longest splits uncut whenever any split
            # happened to be unsplittable.
            unsplittable.add((sp.start_m, sp.end_m))
            continue
        out[index:index + 1] = _cut_solved(sp, cut)
    return out


def _cut_solved(split: fa26.Split, cut: int) -> list[fa26.Split]:
    """Split one solved split in two without changing what it commands.

    Deployment applies over [start, deploy_end) and harvesting over
    [clip_start, end), so each half keeps the part of each region that falls
    inside it. Subdividing faithfully matters because the second search stage
    starts from the first stage's answer: if the cut changed behaviour, the
    refinement would begin from something worse than what it was handed.
    """
    has_clip = split.clip_start_m < fa26.CLIP_OFF and split.clip_kw > 0
    first = fa26.Split(
        split.start_m, min(split.deploy_end_m, cut),
        split.clip_start_m if has_clip and split.clip_start_m < cut else fa26.CLIP_OFF,
        cut, split.deploy_kw,
        split.clip_kw if has_clip and split.clip_start_m < cut else 0)
    second = fa26.Split(
        cut, max(split.deploy_end_m, cut),
        max(split.clip_start_m, cut) if has_clip else fa26.CLIP_OFF,
        split.end_m, split.deploy_kw, split.clip_kw if has_clip else 0)
    return [first, second]


def _speed_midpoint(split: fa26.Split, profile) -> int | None:
    """Where the split's measured speed passes halfway between its ends.

    Cutting by speed rather than by distance keeps each piece to a band over
    which one power level means roughly one level of force.
    """
    ds = int(profile.ds)
    band = [profile.measured_speed_at(d)
            for d in range(split.start_m, split.end_m, ds)]
    if len(band) < 4:
        return None
    lo, hi = min(band), max(band)
    # The end of a split often sits at a braking point, so the closing speed is
    # not the top of its range. Cut against the range itself.
    if hi - lo < 5.0:
        return None
    target = (lo + hi) * 0.5
    d = split.start_m + ds
    while d < split.end_m - ds:
        if profile.measured_speed_at(d) >= target:
            break
        d += ds
    cut = int(d)
    if cut - split.start_m < 100 or split.end_m - cut < 100:
        return None
    return cut


def _with_deploy(split: fa26.Split, power: float) -> fa26.Split:
    """Set a split's deployment power, carving out room for it if there is none.

    Splits seeded from a part-throttle region start with a zero-length deploy
    region, so raising the power alone changed nothing and the optimiser could
    never switch deployment on there. That left dead zones at corner exits --
    exactly where deployment is worth most, because force is power over speed.
    """
    power = int(power)
    if power <= 0:
        return fa26.Split(split.start_m, split.start_m, split.clip_start_m,
                          split.end_m, 0, split.clip_kw)
    deploy_end = split.deploy_end_m
    if deploy_end <= split.start_m:
        limit = split.clip_start_m if split.clip_start_m < fa26.CLIP_OFF else split.end_m
        deploy_end = max(split.start_m, limit)
        if deploy_end <= split.start_m:
            deploy_end = split.end_m
    return fa26.Split(split.start_m, deploy_end,
                      max(deploy_end, split.clip_start_m) if split.clip_start_m < fa26.CLIP_OFF
                      else fa26.CLIP_OFF,
                      split.end_m, power, split.clip_kw)


def _with_clip(split: fa26.Split, power: float) -> fa26.Split:
    """Set a split's harvesting power, carving out room for it if there is none.

    Turning harvesting on for a split that deploys all the way to its end would
    otherwise create a zero-length clip region that harvests nothing, so the
    optimiser could never add harvesting in a single move.
    """
    power = int(power)
    if power <= 0:
        return fa26.Split(split.start_m, split.deploy_end_m, fa26.CLIP_OFF,
                          split.end_m, split.deploy_kw, 0)
    clip_start = split.clip_start_m
    deploy_end = split.deploy_end_m
    if clip_start >= fa26.CLIP_OFF or clip_start >= split.end_m:
        clip_start = split.start_m + (split.end_m - split.start_m) // 2
        deploy_end = min(deploy_end, clip_start)
    return fa26.Split(split.start_m, deploy_end, clip_start, split.end_m,
                      split.deploy_kw, power)


MIN_CLIP_M = 25


def sanitise(split: fa26.Split, profile) -> fa26.Split:
    """Pull a split's harvest region back inside full-throttle territory.

    Super-clipping is harvesting *while on the throttle*. A clip region that
    reaches back over a braking zone or a corner exit tells the car to pull up
    to 350 kW out of the driveline at 80 kph, which behaves -- and feels -- like
    the engine cutting out.

    Nothing stopped the search producing those. Deployment and harvesting were
    both gated on full throttle inside the simulator, so a command anywhere else
    was a free no-op: it scored exactly the same as leaving the region clean, and
    the climb had no reason to prefer either. The car obeys it regardless.
    """
    if split.clip_kw <= 0 or split.clip_start_m >= fa26.CLIP_OFF:
        return split
    ds = profile.ds
    floor = split.end_m
    while floor - ds >= split.start_m and profile.full_throttle_at(floor - ds):
        floor -= ds
    clip_start = max(int(floor), split.clip_start_m, split.deploy_end_m)
    if split.end_m - clip_start < MIN_CLIP_M:
        return fa26.Split(split.start_m, split.deploy_end_m, fa26.CLIP_OFF,
                          split.end_m, split.deploy_kw, 0)
    if clip_start == split.clip_start_m:
        return split
    return fa26.Split(split.start_m, split.deploy_end_m, clip_start,
                      split.end_m, split.deploy_kw, split.clip_kw)


def ensure_live_exit(split: fa26.Split, profile) -> fa26.Split:
    """Never hand the car a zero-length deploy region where it is on throttle.

    A split whose deploy region is empty commands nothing at all, which is what
    left Zandvoort's Turn 1 with no power. The car holds its 200 kW floor at
    throttle application regardless of the map, and the simulator already
    assumes it does, so commanding the floor here changes no energy total -- it
    just stops the written map claiming something the car will not do.
    """
    if split.deploy_end_m > split.start_m and split.deploy_kw > 0:
        return split
    ds = profile.ds
    # Only where the throttle is genuinely picked up inside this split. The
    # floor is an obligation the car takes on *at throttle application*, so
    # commanding it in a split that opens with the driver already above the
    # arbitration knee is not free -- it is a power increase under throttle,
    # which the car will refuse and which then sits in the written map as a
    # promise it never keeps. Four of these survived in a Monza qualifying map
    # because they were injected into the seed before anything checked them.
    if profile.gas[profile._slot(split.start_m)] >= fa26.DEMAND_RESET_GAS:
        return split
    d = split.start_m
    while d < split.end_m and profile.gas[profile._slot(d)] < 0.2:
        d += ds
    if d >= split.end_m:
        return split                      # nothing on throttle here
    limit = (split.clip_start_m if split.clip_start_m < fa26.CLIP_OFF
             else split.end_m)
    run = d
    while run < limit and profile.gas[profile._slot(run)] >= 0.2:
        run += ds
    deploy_end = min(int(run), limit)
    if deploy_end <= split.start_m:
        return split
    return fa26.Split(split.start_m, deploy_end, split.clip_start_m,
                      split.end_m, int(max(split.deploy_kw, fa26.FLOOR_KW)),
                      split.clip_kw)


def build_recharge(splits, car, longitudinal, profile, powertrain,
                   start_store_kj: float, harvest_cap_kj: float,
                   target_kj: float = 3950.0, ds: float = 2.5,
                   reset_zones=(), speed_zones=()):
    """The gentlest full-lap harvest that still fills the store by the line.

    Clipping at full power everywhere does fill the battery, but it also takes
    350 kW out of the driveline on every straight: Madrid came back as a 1339 s
    lap with 453 MJ of harvest refused by an already-full store. Once the store
    is full, extra clip power is pure lap time for nothing. So the lowest power
    that still arrives full is the right one, and it is cheap to find by trying
    each level in turn.
    """
    best = None
    # Below the floor the clip cannot even cancel the car's own deployment, so
    # start at it. A recharge lap is driven once, not repeated, so it is
    # simulated one-shot like a qualifying lap.
    for power in [p for p in POWERS if p >= fa26.FLOOR_KW]:
        candidate = recharge_map(splits, profile, power)
        result = sim26.simulate(candidate, car, longitudinal, profile,
                                powertrain, start_store_kj=start_store_kj,
                                harvest_cap_kj=harvest_cap_kj, ds=ds,
                                settle=False, reset_zones=reset_zones,
                                speed_zones=speed_zones)
        best = (candidate, result)
        if result.store_end_kj >= target_kj:
            break
    return best


FLOOR_CANCEL_M = 260


def recharge_map(splits, profile, clip_kw: float = fa26.FLOOR_KW):
    """Harvest across the lap by cancelling the floor where it actually occurs.

    Commanding zero deployment does not stop the car deploying: it holds 200 kW
    for about a second at every throttle application and then ramps down at
    100 kW/s, which over a lap's worth of corner exits is several MJ of
    deployment the map never asked for. A recharge map that clips at 50 kW
    against that floor still nets +150 kW out of the corner, which is the big
    deploy felt down Monza's back straight.

    Clipping hard everywhere cancels it but is brutal -- 200 kW across every
    straight cost 16 s a lap. The floor is transient, so the clip only has to
    cover the first few hundred metres after the throttle opens. Each
    full-throttle split is therefore cut into a short cancelling window and a
    free-coasting remainder, longest splits first while slots remain.
    """
    """A map that harvests everywhere it legally can, to refill the store.

    A recharge lap has one job: hand the next lap a full battery. Searching for
    it as an optimisation kept producing maps that still deployed, because any
    deployment that costs less time than it costs charge scores well on a
    combined objective. This is built rather than searched -- super-clip at full
    power across every full-throttle stretch, and command nothing beyond the
    floor the car holds at throttle application anyway.
    """
    ds = int(profile.ds)
    out = []
    for sp in splits:
        if not profile.full_throttle_at(sp.start_m + ds):
            out.append(fa26.Split(sp.start_m, sp.start_m, fa26.CLIP_OFF,
                                  sp.end_m, 0, 0))
        else:
            out.append(fa26.Split(sp.start_m, sp.start_m, sp.start_m,
                                  sp.end_m, 0, int(clip_kw)))

    # Split off the cancelling window where there is room in the map for it.
    order = sorted(range(len(out)), key=lambda i: out[i].start_m - out[i].end_m)
    for i in order:
        if len(out) >= fa26.SPLITS_PER_MAP:
            break
        sp = out[i]
        if sp.clip_kw <= 0 or sp.end_m - sp.start_m <= FLOOR_CANCEL_M + MIN_CLIP_M:
            continue
        cut = sp.start_m + FLOOR_CANCEL_M
        out[i] = fa26.Split(sp.start_m, sp.start_m, sp.start_m, cut, 0, sp.clip_kw)
        out.insert(i + 1, fa26.Split(cut, cut, fa26.CLIP_OFF, sp.end_m, 0, 0))
    return out


def _nearest_power_index(power: float) -> int:
    levels = POWERS[1:]
    return min(range(len(levels)), key=lambda k: abs(levels[k] - power))


def _level_clip(split: fa26.Split, direction: int) -> fa26.Split | None:
    """Trade super-clip level against width at roughly constant energy.

    The retarding force of harvesting is power over speed, so concentrating a
    fixed amount of it into a short burst slows the car, which raises the force
    still further -- the cost compounds. Spreading the same energy thinner is
    usually faster.

    No single move in the search could express that. Changing the power and
    changing the window were separate moves, and the intermediate step -- less
    power over the same short window -- harvests less and gets rejected, so the
    climb never reached the flatter plan on the far side. That trap is what left
    350 kW bursts in the finished maps.
    """
    if split.clip_kw <= 0 or split.clip_start_m >= fa26.CLIP_OFF:
        return None
    levels = POWERS[1:]
    j = _nearest_power_index(split.clip_kw) + direction
    if not 0 <= j < len(levels):
        return None
    power = levels[j]
    length = split.end_m - split.clip_start_m
    scaled = int(round(length * split.clip_kw / power))
    floor = max(split.start_m, split.deploy_end_m)
    clip_start = max(floor, min(split.end_m - scaled, split.end_m - 1))
    if clip_start == split.clip_start_m and power == split.clip_kw:
        return None
    return fa26.Split(split.start_m, split.deploy_end_m, clip_start,
                      split.end_m, split.deploy_kw, int(power))


def _level_deploy(split: fa26.Split, direction: int) -> fa26.Split | None:
    """The same trade for deployment.

    Deployment wants the opposite of harvesting -- force is power over speed
    there too, so it is worth most where the car is slowest -- but which way a
    given split should go depends on its speed range, so both directions are
    offered and the simulation decides.
    """
    if split.deploy_kw <= 0:
        return None
    levels = POWERS[1:]
    j = _nearest_power_index(split.deploy_kw) + direction
    if not 0 <= j < len(levels):
        return None
    power = levels[j]
    length = split.deploy_end_m - split.start_m
    scaled = int(round(length * split.deploy_kw / power))
    limit = (split.clip_start_m if split.clip_start_m < fa26.CLIP_OFF
             else split.end_m)
    deploy_end = min(max(split.start_m + scaled, split.start_m + 1), limit)
    if deploy_end == split.deploy_end_m and power == split.deploy_kw:
        return None
    return fa26.Split(split.start_m, deploy_end, split.clip_start_m,
                      split.end_m, int(power), split.clip_kw)


def _variants(split: fa26.Split, profile) -> list[fa26.Split]:
    """Neighbouring settings for one split."""
    span = max(split.end_m - split.start_m, 1)
    step = max(int(span * 0.15), 10)
    out = []

    for p in POWERS:
        if p != split.deploy_kw:
            out.append(_with_deploy(split, p))
    for p in POWERS:
        if p != split.clip_kw:
            out.append(_with_clip(split, p))
    for d in (-step, step):
        de = min(max(split.deploy_end_m + d, split.start_m), split.end_m)
        cs = max(de, min(split.clip_start_m, split.end_m)) \
            if split.clip_start_m < fa26.CLIP_OFF else fa26.CLIP_OFF
        out.append(fa26.Split(split.start_m, de, cs, split.end_m,
                              split.deploy_kw, split.clip_kw))
    if split.clip_start_m < fa26.CLIP_OFF:
        for d in (-step, step):
            cs = min(max(split.clip_start_m + d, split.deploy_end_m), split.end_m)
            out.append(fa26.Split(split.start_m, split.deploy_end_m, cs, split.end_m,
                                  split.deploy_kw, split.clip_kw))

    # Level-for-width trades, which no combination of the moves above reaches in
    # one step.
    for direction in (-1, 1):
        for candidate in (_level_clip(split, direction),
                          _level_deploy(split, direction)):
            if candidate is not None:
                out.append(candidate)

    quiet = _floor_off(split)
    if quiet is not None:
        out.append(quiet)
    return [ensure_live_exit(sanitise(c, profile), profile) for c in out]


#: Enough harvesting to hold `cmd_clip > 0` through the throttle application.
#: The suppression is the point, not the energy: on a qualifying lap the
#: harvest cap is already saturated, so 50 kW and 350 kW give the same lap to
#: the millisecond, and the smaller figure costs less traction at the corner
#: exit it sits on.
FLOOR_OFF_CLIP_KW = 50


def _floor_off(split: fa26.Split) -> fa26.Split | None:
    """The same zone, harvesting from its first metre: no deployment at all.

    The only way a map can decline the 200 kW floor. The car holds that floor
    for a second after every throttle application whatever the map commands,
    and the demand latch then pins the zone there, so setting a short zone's
    deploy power to 0 changes nothing -- measured: three of eleven Albert Park
    zones give an identical lap time at 0 kW and at 350. The simulator gates
    the floor on `cmd_clip <= 0`, so a clip live at the throttle application
    is the one thing that turns it off.

    The shape is not new -- `build_recharge` has always written it for STRAT 3
    -- but the search could not reach it. `_with_clip` relocates `clip_start`
    only when there is no clip region at all, and then to the split's
    midpoint; from there the start is several 15% nudges away, each scored on
    its own, and the intermediate steps do not pay. So a hill climb turns back
    before arriving.

    Offered as a single move, it is quicker at 8 of 10 circuits and slower at
    none. Madrid gained 0.802 s in the model and 0.660 s on track, against a
    lap that gave some of it back with a mistake in the last sector.
    """
    if split.clip_kw > 0 and split.clip_start_m <= split.start_m:
        return None
    if split.end_m - split.start_m < MIN_CLIP_M:
        return None
    return fa26.Split(split.start_m, split.start_m, split.start_m,
                      split.end_m, 0, FLOOR_OFF_CLIP_KW)


def _more_harvest(split: fa26.Split) -> fa26.Split | None:
    """Raise this split's harvesting by one power step."""
    nxt = next((p for p in POWERS if p > split.clip_kw), None)
    if nxt is None:
        return None
    return _with_clip(split, nxt)


def _more_deploy(split: fa26.Split) -> fa26.Split | None:
    """Raise this split's deployment by one power step, extending it if empty."""
    nxt = next((p for p in POWERS if p > split.deploy_kw), None)
    if nxt is None:
        return None
    return _with_deploy(split, nxt)


# ------------------------------------------------------------------ evaluation
#
# Two things make the hill climb slow: it re-simulates maps it has already
# tried -- a quarter to a third of all calls on a Monza solve -- and it runs one
# lap at a time on one core while twenty sit idle.
#
# Both are fixed without changing a single decision the search makes. The cache
# is exact by construction: identical splits under identical conditions give an
# identical lap. The pool is exact because acceptance is still applied strictly
# in the original candidate order (see `_first_improvement`), so the map that
# wins is the map that would have won sequentially.
#
# An earlier attempt at caching reused *partial* simulation state between
# different maps; it was slower and disagreed with the direct simulation in 6 of
# 324 cases, and was reverted. This is a plain memo on identical inputs, which
# is a different thing.

def illegal_increases(splits, profile, reset_zones=()):
    """Split boundaries where the map raises deploy power on throttle.

    "The maximum power demand cannot increase under part or full throttle,
    except when Boost Mode is activated" (manual p.25), or inside a track
    Power Reset zone. A map that asks for one anyway is not a lap the car can
    drive: it will hold the earlier, lower level and the plan silently does not
    happen.

    The simulator models the rule, which means such a move is scored as a
    no-op -- it costs the candidate nothing, so the hill climb has no gradient
    against it and leaves the garbage in the written map. That is the same
    failure `sanitise` describes for clip regions, so it gets the same
    treatment: the move is rejected outright rather than priced.
    """
    bad = []
    for split in splits:
        d = split.start_m
        # The split that opens the lap has to be checked against the one that
        # closes it, not skipped. Where the start/finish line sits on a
        # straight the driver crosses it flat, so the demand carries over from
        # the previous lap and a map that steps up there is refused exactly
        # like any other mid-throttle increase. At Suzuka this cost the real
        # thing 250 kW: the map asked 200 before the line and 350 after, and
        # the car held 100 kW for the whole 317 m -- 17 kph down on VRC's map,
        # which commands 250 on both sides and is obeyed.
        prev_d = (profile.length_m - profile.ds) if d <= 0 else (d - profile.ds)
        before, _ = fa26.map_command_at(splits, max(0.0, prev_d), profile.length_m)
        after, clip_after = fa26.map_command_at(splits, d + 1.0, profile.length_m)
        if clip_after > 0 or after <= before:
            continue
        # Below the arbitration knee the MGU-K is not deploying, so the next
        # application is free to establish a new level.
        if profile.gas[profile._slot(max(0.0, prev_d))] < fa26.DEMAND_RESET_GAS:
            continue
        if any(a <= d < b for a, b in reset_zones):
            continue
        bad.append((int(d), before, after))
    return bad


def is_executable(splits, profile, reset_zones=()):
    return not illegal_increases(splits, profile, reset_zones)


def make_executable(splits, profile, reset_zones=()):
    """Project a map onto what the car can actually execute.

    Rejecting illegal candidates outright was the first attempt and it strands
    the climb: the seed itself can contain an increase, every neighbour
    inherits it, nothing is ever accepted, and the illegal seed is returned as
    the answer -- four such increases survived a Monza qualifying solve exactly
    that way. Lowering the offending command to the level the car will actually
    hold keeps the search space connected, and makes the map that was scored
    the same map that gets written.

    One forward pass suffices: each split is capped by what its predecessor
    effectively commands, so the level is non-increasing within a throttle
    application and lowering split i can only tighten i+1, which is visited
    after it.
    """
    out = list(splits)
    ds = profile.ds
    for index, split in enumerate(out):
        d = split.start_m
        if split.deploy_kw <= 0:
            continue
        # Wrap at the line -- see `illegal_increases`.
        prev = max(0.0, (profile.length_m - ds) if d <= 0 else (d - ds))
        before, _ = fa26.map_command_at(out, prev, profile.length_m)
        after, clip_after = fa26.map_command_at(out, d + 1.0, profile.length_m)
        if clip_after > 0 or after <= before:
            continue
        if profile.gas[profile._slot(prev)] < fa26.DEMAND_RESET_GAS:
            continue
        if any(a <= d < b for a, b in reset_zones):
            continue
        out[index] = fa26.Split(split.start_m, split.deploy_end_m,
                                split.clip_start_m, split.end_m,
                                int(before), split.clip_kw)
    return out


def _key(splits):
    return tuple((s.start_m, s.deploy_end_m, s.clip_start_m, s.end_m,
                  s.deploy_kw, s.clip_kw) for s in splits)


@dataclass
class Approach:
    """How a push lap is entered: off `lead`, switched `switches` metres out.

    A qualifying map was scored from a full battery and the switch advice was
    worked out afterwards -- and the advice was often "before the final
    corner", which spends a third of the battery on the run to the line. At
    Sepang that made every lap start at 53-56%, and a map planned for full
    spent early and was empty from T14. Scored this way, the charge and the
    speed at the line are consequences of the map, as they are on track.
    """

    lead: list
    switches: tuple = (0.0,)
    store_kj: float = 4000.0            # a full store: the recharge lap's job


class Evaluator:
    """Simulates candidate maps, remembering ones it has already seen."""

    def __init__(self, car, longitudinal, profile, powertrain, ds,
                 start_store_kj, harvest_cap_kj, settle, reset_zones=(),
                 speed_zones=(), charges=(), approach: "Approach | None" = None):
        self._fixed = (car, longitudinal, profile, powertrain, ds,
                       start_store_kj, harvest_cap_kj, settle)
        self.reset_zones = tuple(tuple(z) for z in reset_zones)
        self.speed_zones = tuple(tuple(z) for z in speed_zones)
        self.charges = tuple(charges)
        self.approach = approach
        self._cache = {}
        self.evaluations = 0
        self._no_ers = None
        #: Whether lifting in sweepers is charged. See `optimise`.
        self.charge_lifts = True

    def use_switches(self, switches) -> None:
        """Score from now on with only these switch points."""
        if self.approach is None or tuple(switches) == self.approach.switches:
            return
        self.approach = Approach(self.approach.lead, tuple(switches),
                                 self.approach.store_kj)
        self._cache = {}

    def one(self, splits):
        return self.many([splits])[0]

    def many(self, candidates):
        keys = [_key(c) for c in candidates]
        # A batch can hold the same map twice -- two different moves can land on
        # it -- so simulate each distinct one once.
        todo, seen = [], set()
        for key, candidate in zip(keys, candidates):
            if key not in self._cache and key not in seen:
                seen.add(key)
                todo.append((key, candidate))
        for key, candidate in todo:
            self.evaluations += 1
            self._cache[key] = self._simulate(candidate)
        return [self._cache[k] for k in keys]

    def _baseline(self) -> dict:
        """The lifts no map can avoid: the lap with no deployment at all."""
        if self._no_ers is None:
            car, longitudinal, profile, powertrain, ds, start, cap, _settle = self._fixed
            self._no_ers = sim26.simulate(
                [], car, longitudinal, profile, powertrain, start_store_kj=start,
                harvest_cap_kj=cap, ds=ds, settle=False,
                reset_zones=self.reset_zones, speed_zones=self.speed_zones).lifts
        return self._no_ers

    def charge_for_lifts(self, on: bool) -> None:
        if on != self.charge_lifts:
            self.charge_lifts = on
            self._cache = {}

    def _simulate(self, splits):
        result = self._simulate_lap(splits)
        result.lift_s = self._lift_s(result)
        return result

    def _lift_s(self, result) -> float:
        return lift_cost_s(result, self._baseline()) if self.charge_lifts else 0.0

    def _simulate_lap(self, splits):
        car, longitudinal, profile, powertrain, ds, start, cap, settle = self._fixed
        if self.approach is not None:
            return self._as_driven(splits)
        run = lambda store: sim26.simulate(
            splits, car, longitudinal, profile, powertrain,
            start_store_kj=store, harvest_cap_kj=cap, ds=ds, settle=settle,
            reset_zones=self.reset_zones, speed_zones=self.speed_zones)
        result = run(start)
        if self.charges:
            # The reported lap time stays the one at the planned charge; only
            # what the search optimises changes.
            times = [result.lap_time_s] + [run(c).lap_time_s for c in self.charges]
            result.robust_time_s = sum(times) / len(times)
        return result

    def _entered(self, splits, switch_m: float, lead_store_kj: float):
        """One push lap, run down to the line from the lead map first."""
        car, longitudinal, profile, powertrain, ds, _start, cap, _settle = self._fixed
        zones = dict(reset_zones=self.reset_zones, speed_zones=self.speed_zones)
        state = sim26.run_to_line(
            self.approach.lead, splits, switch_m, car, longitudinal, profile,
            powertrain, start_store_kj=lead_store_kj, harvest_cap_kj=cap,
            approach_m=max(sim26.PRIME_M, switch_m + 50.0), ds=ds, **zones)
        result = sim26.simulate(splits, car, longitudinal, profile, powertrain,
                                start_store_kj=state.store_kj, harvest_cap_kj=cap,
                                ds=ds, settle=False, entry=state, **zones)
        result.switch_m = switch_m
        return result

    def _as_driven(self, splits):
        """The lap as driven: the better of the switch points on offer.

        The robustness charges, when asked for, are what the lead lap arrives
        with rather than what the push lap starts with -- a recharge lap that
        did not quite fill the store -- and are taken at the chosen switch.
        """
        best = None
        for switch_m in self.approach.switches:
            result = self._entered(splits, switch_m, self.approach.store_kj)
            result.lift_s = self._lift_s(result)
            if best is None or (result.lap_time_s + result.lift_s
                                < best.lap_time_s + best.lift_s):
                best = result
        if self.charges:
            times = [best.lap_time_s] + [
                self._entered(splits, best.switch_m, c).lap_time_s
                for c in self.charges]
            best.robust_time_s = sum(times) / len(times)
        return best


#: The blanket family, as (how much of the lap from the line, what power).
#: This replaces two hand-named starting maps. `_all_out` was (1.00, 350) and
#: `_front_loaded` was (0.45, 350); both were added one at a time, each after a
#: circuit was found where the climb could not reach the shape, and each with a
#: guard deciding when it was worth the extra climb. Naming shapes as they are
#: discovered does not converge -- there is no reason to think 1.00 and 0.45
#: are the only two that matter, and nothing said which others to try.
#:
#: A grid does converge, because it is cheap to widen. Every member is screened
#: with a single simulation and only the best few are climbed, so going from
#: two shapes to twelve costs less than the two guarded climbs it replaces.
BLANKET_COVERAGE = (0.25, 0.45, 0.70, 1.00)
BLANKET_LEVELS = (fa26.FLOOR_KW, 250.0, fa26.POWER_MAX)

#: Refused recovery worth one more climb from a map that spends earlier. Set
#: to match the figure the app reports as wasted: anything worth telling the
#: user about is worth a second look.
REFUSAL_WORTH_CHASING_MJ = 0.1

#: How much of the lap that second look spends flat out. Enough to empty a
#: full store before the first heavy braking of the second half, so the
#: harvesting that follows has somewhere to go.
FRONT_LOAD_FRACTION = 0.45

#: How many screened starting points to actually climb, over and above the
#: throttle-trace seed and any authored maps found for the track. Three is
#: where the gain flattens: the screen ranks the family well enough that the
#: fourth has never been the one that wins.
SCREEN_KEEP = 2


def _blanket(splits, profile, coverage: float, power: float) -> "list[fa26.Split]":
    """The same zones, deploying at `power` over the first `coverage` of the lap.

    The two shapes a climb cannot reach on its own, generalised. Raising one
    zone while the store is full and recovery is being refused buys almost
    nothing, so each single step looks like noise and the climb stays put --
    spending the store only pays when it is done across a stretch at once.
    Where that stretch should end, and how hard to spend over it, is what the
    coverage and the level say.
    """
    if not splits:
        return []
    cutoff = profile.length_m * coverage
    out = []
    for split in splits:
        if split.start_m <= cutoff:
            out.append(fa26.Split(split.start_m, split.end_m, split.end_m,
                                  split.end_m, power, 0))
        else:
            out.append(fa26.Split(split.start_m, split.deploy_end_m,
                                  split.clip_start_m, split.end_m,
                                  split.deploy_kw, split.clip_kw))
    return out


def _clip_blanket(splits, profile, coverage: float) -> "list[fa26.Split]":
    """Deploy hard early, then super-clip through everything after.

    Super-clipping is harvesting at full throttle: giving up speed on a
    straight to refill the store for somewhere it is worth more. The climb can
    reach it one zone at a time in principle -- `_level_clip` is among the
    single-split moves -- but not in practice on an energy-neutral map, where
    one zone of harvest with nothing to spend it on is pure loss and is
    rejected before the deployment that would pay for it is ever tried. The
    paired move exists for exactly that and only pairs one zone with one
    other, so a map wanting several zones of clip to fund one long deployment
    is out of its reach.
    """
    if not splits:
        return []
    cutoff = profile.length_m * coverage
    out = []
    for split in splits:
        if split.start_m <= cutoff:
            out.append(fa26.Split(split.start_m, split.end_m, split.end_m,
                                  split.end_m, fa26.POWER_MAX, 0))
        else:
            out.append(fa26.Split(split.start_m, split.start_m,
                                  split.start_m, split.end_m, 0, fa26.POWER_MAX))
    return out


def _family(splits, profile) -> "list[list[fa26.Split]]":
    """Every systematic starting map, before screening."""
    out = []
    for coverage in BLANKET_COVERAGE:
        for power in BLANKET_LEVELS:
            out.append(_blanket(splits, profile, coverage, power))
    for coverage in BLANKET_COVERAGE[:-1]:
        out.append(_clip_blanket(splits, profile, coverage))
    return out


def _always_climb(splits, profile, objective: Objective) -> "list[list[fa26.Split]]":
    """Starting maps that are climbed whatever the screen thinks of them.

    A screen is one simulation, and one simulation cannot tell which map will
    climb best -- only which is best as it stands. For a one-shot lap that
    distinction has a known answer: spending the whole store is not a
    heuristic but the job, and a map that ends on the charge it started with
    has failed however good its lap time looks. Leaving that start to the
    screen cost Austin 0.13 s and Suzuka 0.15 s against the guarded climb it
    replaced, so it is not left to the screen.
    """
    if not objective.one_shot:
        return []
    return [_blanket(splits, profile, 1.0, fa26.POWER_MAX)]


#: Largest step for the coarse climbs, in metres. The final refinement runs at
#: `ds`, which is what the reported lap time is measured at; the climbs before
#: it are only ranking candidate maps against each other, and they do not need
#: to do that to the nearest 2.5 m. Doubling the step halves every simulation,
#: and the search is nothing but simulations.
#: How near the line a split has to start to count as the lap's opening.
OPENING_M = 60.0

#: How much more the last split of the lap must ask for than the opening, so
#: the opening is unambiguously a reduction and the car grants it.
OPENING_MARGIN_KW = 50

COARSE_DS = 5.0

#: A coarse answer is never the answer. Between 3.5 m and 3.634 m at Monaco
#: the map falls off a cliff -- 73.555 s and 11.95 MJ to 74.672 s and 9.52 --
#: because at some step size a zone boundary the map needs stops being
#: representable. The cliff is sharp, it is circuit-specific, and no rule
#: tying the step to lap length dodges it: Monza, Spa, Suzuka and Austin are
#: all untroubled at 5 m and Monaco is ruined by 3.634. So the coarse pass
#: ranks starting points and gets close, and whatever it picks is then climbed
#: again at `ds` before anything is subdivided. Coarse is an accelerator, not
#: an oracle.
#:
#: And it is only used where there is something to accelerate. A short circuit
#: solves quickly anyway -- Monaco takes under a minute at full resolution --
#: so it is not worth any risk at all there, and the re-climb does not fully
#: recover it: 73.716 s against 73.555 for a saving of twenty seconds.
COARSE_MIN_LENGTH_M = 4500.0


def screen(splits, car, longitudinal, profile, powertrain, objective: Objective,
           start_store_kj: float, ds: float = COARSE_DS, reset_zones=(),
           speed_zones=()):
    """The best of the starting maps, with no climbing at all.

    The screening step of `optimise`, exposed on its own. It is one simulation
    per candidate and no search, which makes it cheap enough to run between
    laps while somebody is still driving -- and because the real search climbs
    from these same maps and keeps the best finish, what this returns is a
    floor on what a full run would find rather than a different answer.
    """
    evaluator = Evaluator(
        car, longitudinal, profile, powertrain, ds, start_store_kj,
        objective.harvest_cap_mj * 1000, not objective.one_shot,
        reset_zones, speed_zones,
        tuple(f * STORE_CAPACITY_KJ for f in objective.charge_fractions))
    candidates = [splits] + _family(splits, profile)
    prepared = [make_executable(
        [ensure_live_exit(sanitise(s, profile), profile) for s in candidate],
        profile, evaluator.reset_zones) for candidate in candidates]
    best = best_score = None
    for candidate_result in evaluator.many(prepared):
        score = objective.score(candidate_result, start_store_kj)
        if score is not None and (best_score is None or score < best_score):
            best, best_score = candidate_result, score
    return best


def optimise(splits, car, longitudinal, profile, powertrain, objective: Objective,
             start_store_kj: float, max_sweeps: int = 8, ds: float = 2.5,
             extra_seeds=(), reset_zones=(), speed_zones=(), on_progress=None,
             coarse_ds: float = COARSE_DS, approach: "Approach | None" = None):
    """The search below run twice, keeping the better. Same return as it.

    Lifting in sweepers (`lift_cost_s`) has to be charged somewhere, and
    neither place works alone. Charged throughout, the cost walls off the
    paths the climbs take through maps that lift on the way to ones that do
    not: Bathurst qualifying came out 0.27 s slower than a map that never
    lifted at all, and Spa's race 0.33 s for 0.02 s of lifting avoided.
    Charged only in the refinement, it is safe but timid: taking the lift out
    of Sepang T5 means moving the energy to another part of the lap, which a
    local refinement cannot do, and the race map kept its 18 kph lift. So both
    are run and scored the same way, lifts included -- never worse than the
    search that ignores lifts by more than its lifts cost, and able to make
    the bigger moves when they pay. The two run side by side; see
    `optimise_many`.
    """
    task = dict(splits=splits, car=car, longitudinal=longitudinal,
                profile=profile, powertrain=powertrain, objective=objective,
                start_store_kj=start_store_kj, max_sweeps=max_sweeps, ds=ds,
                extra_seeds=extra_seeds, reset_zones=reset_zones,
                speed_zones=speed_zones, coarse_ds=coarse_ds,
                approach=approach)
    return optimise_many([task], on_progress=on_progress)[0]


#: Searches run at once, at most: every core the machine offers.
MAX_WORKERS = os.cpu_count() or 1

#: Restarts from the finished answer, shaken, per map -- see `optimise_many`.
SHAKES = 4

#: Share of a map's splits each shake disturbs.
SHAKE_SHARE = 0.35

_PROGRESS = None                    # shared fractions, one per job, in a worker


def _worker_init(progress) -> None:
    global _PROGRESS
    _PROGRESS = progress


def _job(args):
    index, task, blind, restart = args

    def report(fraction: float) -> None:
        _PROGRESS[index] = fraction

    return _optimise_once(**task, on_progress=report, blind_coarse=blind,
                          restart=restart)


def _shaken(splits, rng, profile, reset_zones):
    """The map with about a third of its splits disturbed: power a step or
    two either way, the end of deployment moved by up to a fifth."""
    out = []
    for split in splits:
        if rng.random() < SHAKE_SHARE:
            span = max(split.end_m - split.start_m, 1)
            level = min(range(len(POWERS)),
                        key=lambda k: abs(POWERS[k] - split.deploy_kw))
            level = max(0, min(len(POWERS) - 1,
                               level + rng.choice((-2, -1, 1, 2))))
            end = int(min(split.end_m, max(
                split.start_m,
                split.deploy_end_m + rng.uniform(-0.2, 0.2) * span)))
            clip_start = (max(end, min(split.clip_start_m, split.end_m))
                          if split.clip_kw else split.end_m)
            split = fa26.Split(split.start_m, end, clip_start, split.end_m,
                               int(POWERS[level]), split.clip_kw)
        out.append(split)
    return make_executable(
        [ensure_live_exit(sanitise(s, profile), profile) for s in out],
        profile, tuple(tuple(z) for z in reset_zones))


def _soften(splits, evaluator, profile, objective: Objective,
            start_store_kj: float):
    """A restart's map, with every deployment a step lower until it is allowed.

    A climb handed a map its objective refuses strips it to nothing and starts
    again, which is right for a guess and wrong for a map derived from the
    race answer: the save map starts from a race map that ends the lap level
    rather than ahead, and stripping it threw the race map's shape away.
    Stepping the power down keeps the shape and finds the nearest map that
    banks enough.
    """
    current = make_executable(
        [ensure_live_exit(sanitise(s, profile), profile) for s in splits],
        profile, evaluator.reset_zones)
    for _ in range(len(POWERS)):
        if objective.score(evaluator.one(current), start_store_kj) is not None:
            return current
        lower = [_less_deploy(s) or s for s in current]
        if lower == current:
            break
        current = make_executable(lower, profile, evaluator.reset_zones)
    return splits


def optimise_many(tasks, on_progress=None, workers: int | None = None,
                  derived=()):
    """Several `optimise` searches at once, each on its own core.

    `tasks` are `optimise`'s arguments as dicts, less `on_progress`; the
    answer is one `(splits, result, evaluations)` per task, in order.

    The search was single-threaded: qualifying, then race, each now searched
    twice, one after the other on one core of an eight-core machine. The four
    are independent -- the recharge map they depend on is built before any of
    them -- so they run side by side and the wait is the longest of them
    rather than the sum. Processes rather than threads, because the work is
    pure Python and threads would queue on the interpreter lock.

    Then the answer is shaken. A hill climb stops at the first map no single
    change improves, and some better maps are several changes away: Monza's
    race map is 0.46 s quicker with a super-clip zone after the first chicane
    that pays for 1.3 MJ more deployment, and no one step reaches it. Against
    restarts on sixteen circuits the search was within 0.05 s on 25 maps of
    32 and short by 0.08-0.46 s on the other seven, and most of that was
    found by disturbing the finished map at random and climbing again --
    which is what this does, `SHAKES` times per map on spare cores. The
    answer only changes for one that scores better, so it cannot get worse,
    and the shakes are seeded, so a solve gives the same answer twice.

    `on_progress` gets the fraction done and may raise to abandon the lot,
    as the stop button does; the workers are killed then, and nothing they
    were doing touched the disk.

    `derived` are `(parent, task)` pairs: maps worked out from another
    task's answer rather than searched from scratch -- the attack, defend,
    push and save maps, which are the race map with a different energy
    budget. Each is climbed twice from its parent's answer, once as it is and
    once shaken, alongside the shakes and on the same cores, so they add
    little to the wait. Their answers follow the tasks' in the result.

    Anything that stops the pool starting -- a machine that refuses new
    processes -- falls back to running everything here, one at a time.
    """
    first = [(2 * t + k, task, blind, False) for t, task in enumerate(tasks)
             for k, blind in enumerate((True, False))]
    size = len(tasks) * max(2, SHAKES) + 3 * len(derived)
    if workers is None:
        workers = min(size, MAX_WORKERS)
    with _Runner(workers, size, on_progress) as runner:
        runs = runner.run(first, 0.0, 0.7)
        best = [_better(task, runs[2 * t:2 * t + 2])
                for t, task in enumerate(tasks)]
        second = []
        for t, task in enumerate(tasks):
            rng = random.Random(t)
            for k in range(SHAKES):
                start = _shaken(best[t][0], rng, task["profile"],
                                task["reset_zones"])
                second.append((t * SHAKES + k,
                               dict(task, splits=start, extra_seeds=()),
                               k % 2 == 0, True))
        base = len(tasks) * SHAKES
        for d, (parent, task) in enumerate(derived):
            rng = random.Random(100 + d)
            seed = best[parent][0]
            for k, start in enumerate((
                    seed, _shaken(seed, rng, task["profile"],
                                  task["reset_zones"]))):
                second.append((base + 2 * d + k,
                               dict(task, splits=start, extra_seeds=()),
                               k == 0, True))
        shaken = runner.run(second, 0.7, 0.9) if second else []
        # Each derived map against every other one, under its own rules. A
        # neutral map is a legal defend map -- it simply leaves the extra
        # 0.4 MJ unspent -- yet Sepang's defend climb finished 0.12 s behind
        # it. Where another map scores better, it is climbed once more, so
        # it can spend what its new budget allows.
        base = len(tasks) * SHAKES
        own = [_better(task, shaken[base + 2 * d:base + 2 * d + 2])
               for d, (_parent, task) in enumerate(derived)]
        third, borrowed = [], {}
        for d, (_parent, task) in enumerate(derived):
            judge = _judge(task)
            mine = task["objective"].score(judge.one(own[d][0]),
                                           task["start_store_kj"])
            for e, other in enumerate(own):
                if e == d:
                    continue
                result = judge.one(other[0])
                theirs = task["objective"].score(result, task["start_store_kj"])
                if theirs is not None and (mine is None or theirs < mine - 1e-4):
                    # Kept as a candidate in its own right: a climb can
                    # finish behind where it started, and did here.
                    mine, borrowed[d] = theirs, (other[0], result, 0)
            if d in borrowed:
                third.append((len(third), dict(task, splits=borrowed[d][0],
                                               extra_seeds=()), False, True))
        polished = runner.run(third, 0.9, 1.0) if third else []
    out = []
    for t, task in enumerate(tasks):
        mine = shaken[t * SHAKES:(t + 1) * SHAKES]
        pick = _better(task, [best[t]] + mine)
        out.append((pick[0], pick[1],
                    sum(r[2] for r in runs[2 * t:2 * t + 2] + mine)))
    extra = dict(zip(sorted(borrowed), polished))
    for d, (_parent, task) in enumerate(derived):
        mine = shaken[base + 2 * d:base + 2 * d + 2] + (
            [borrowed[d], extra[d]] if d in extra else [])
        pick = _better(task, mine)
        out.append((pick[0], pick[1], sum(r[2] for r in mine)))
    return out


def _judge(task):
    """An evaluator scoring maps exactly as a finished search scores them."""
    objective = task["objective"]
    return Evaluator(
        task["car"], task["longitudinal"], task["profile"], task["powertrain"],
        task.get("ds", 2.5), task["start_store_kj"],
        objective.harvest_cap_mj * 1000, not objective.one_shot,
        task["reset_zones"], task["speed_zones"],
        tuple(f * STORE_CAPACITY_KJ for f in objective.charge_fractions),
        approach=task.get("approach"))


def _better(task, runs):
    """The best-scoring of these runs, the first if none is admissible."""
    scored = [(task["objective"].score(r[1], task["start_store_kj"]), r)
              for r in runs]
    admissible = [x for x in scored if x[0] is not None]
    return min(admissible, key=lambda x: x[0])[1] if admissible else runs[0]


class _Runner:
    """A process pool kept for both stages, or this process if none starts."""

    def __init__(self, workers, size, on_progress):
        self.on_progress = on_progress
        self.pool = self.progress = None
        if workers > 1:
            self._start(workers, size)

    def _start(self, workers, size):
        import multiprocessing as mp
        context = mp.get_context("spawn")
        # A worker started this way first re-runs the script that started
        # the program, unless it carries the usual `if __name__ ==
        # "__main__"` guard. The app does; `check_app.py` and every one-off
        # script does not, and there each worker set about a solve of its
        # own. The workers need nothing from that script -- only this
        # module, found on the path they are handed -- so it is kept from
        # them while they start. The built program never takes this path;
        # `freeze_support` routes its workers.
        main = sys.modules.get("__main__")
        hidden = getattr(main, "__file__", None)
        if hidden is not None and getattr(main, "__spec__", None) is None:
            del main.__file__
        else:
            hidden = None
        try:
            self.progress = context.Array("d", size, lock=False)
            self.pool = context.Pool(workers, initializer=_worker_init,
                                     initargs=(self.progress,))
        except Exception:
            self.pool = self.progress = None
        finally:
            if hidden is not None:
                main.__file__ = hidden

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.pool is not None:
            self.pool.terminate()
            self.pool.join()
        return False

    def _report(self, lo, hi, fraction):
        if self.on_progress is not None:
            self.on_progress(lo + (hi - lo) * fraction)

    def run(self, jobs, lo, hi):
        if self.pool is None:
            out = []
            for n, (_index, task, blind, restart) in enumerate(jobs):
                out.append(_optimise_once(
                    **task, blind_coarse=blind, restart=restart,
                    on_progress=lambda f, n=n: self._report(
                        lo, hi, (n + f) / len(jobs))))
            return out
        for i in range(len(self.progress)):
            self.progress[i] = 0.0
        pending = self.pool.map_async(_job, jobs, chunksize=1)
        while not pending.ready():
            pending.wait(0.25)
            done = sum(self.progress[j[0]] for j in jobs) / len(jobs)
            self._report(lo, hi, done)
        return pending.get()


def _optimise_once(splits, car, longitudinal, profile, powertrain,
                   objective: Objective, start_store_kj: float,
                   max_sweeps: int = 8, ds: float = 2.5, extra_seeds=(),
                   reset_zones=(), speed_zones=(), on_progress=None,
                   coarse_ds: float = COARSE_DS,
                   approach: "Approach | None" = None,
                   blind_coarse: bool = True, restart: bool = False):
    """Solve coarse, subdivide the answer, then refine. Returns (splits, result, evals).

    With an `approach`, every map is scored as it will be driven -- entered off
    the lead map, at the better of its switch points -- and the result carries
    `switch_m`. Scoring two switch points costs two laps a candidate, so the
    point is chosen on the starting maps, held for the coarse search, and
    chosen again on the coarse answer before the fine one.

    A hill climb only ever reaches what its starting point can deform into.
    Seeding from throttle transitions makes a long flat-out stretch one split
    with one deploy window anchored at its start, so a shape like VRC's Spa map
    -- deploy, coast, deploy again across Eau Rouge and Kemmel -- is unreachable
    no matter how many sweeps run. Their map simulated 0.85 s faster than the
    one this solver produced. Any authored map found for the track is therefore
    used as an additional starting point, and the best finish wins.

    Seeding the fine map directly was worse than the coarse one at Spa -- 110.38
    against 110.26 -- because the extra freedom enlarged the search faster than
    the sweep budget could cover it. Subdividing the *solved* map instead starts
    the second stage from the first stage's answer, so the refinement can only
    improve on it, and the extra splits are spent where a single power level was
    being stretched across too wide a speed range.

    `on_progress` is called with a fraction from 0 to 1 as the search runs. A
    sweep is the natural unit: the work is one climb per starting point plus
    one over the subdivided answer, each of at most `max_sweeps` sweeps, and a
    climb that converges early simply skips the rest of its share. It is a
    measurement rather than an estimate, so the bar it drives can only move
    forwards.

    The callback may also raise, which abandons the search. That is how the
    front end's stop button works: nothing here writes to disk and every
    working structure is local to this call, so unwinding part way through
    leaves nothing behind to clean up.
    """
    cap_kj = objective.harvest_cap_mj * 1000
    settle = not objective.one_shot
    charges = tuple(f * STORE_CAPACITY_KJ for f in objective.charge_fractions)
    # Only where there is something to accelerate and something to lose by
    # not: the qualifying search on a long circuit is the one that takes
    # minutes. A race search is already tens of seconds -- it settles faster
    # because the energy-neutral constraint rejects most moves outright -- and
    # ranking its starts on a 5 m grid picked the wrong one at Spa, 0.339 s
    # worse, for a saving that circuit did not need.
    if profile.length_m < COARSE_MIN_LENGTH_M or not objective.one_shot:
        coarse_ds = ds
    fine = Evaluator(car, longitudinal, profile, powertrain, ds,
                     start_store_kj, cap_kj, settle, reset_zones,
                     speed_zones, charges, approach=approach)
    evaluator = (fine if coarse_ds <= ds else
                 Evaluator(car, longitudinal, profile, powertrain, coarse_ds,
                           start_store_kj, cap_kj, settle, reset_zones,
                           speed_zones, charges, approach=approach))
    authored = [list(extra) for extra in extra_seeds]
    # `blind_coarse` leaves lifting uncharged until there is an answer to
    # refine; see `optimise` for why it is run both ways.
    evaluator.charge_for_lifts(not blind_coarse)
    fine.charge_for_lifts(not blind_coarse)
    if restart:
        splits = _soften(splits, evaluator, profile, objective, start_store_kj)

    def choose_switch(evaluator_, candidates) -> None:
        """Hold the switch point the best of these candidates wants."""
        if approach is None or len(approach.switches) < 2:
            return
        evaluator_.use_switches(approach.switches)
        ready = [make_executable(
            [ensure_live_exit(sanitise(s, profile), profile) for s in c],
            profile, evaluator_.reset_zones) for c in candidates]
        results = evaluator_.many(ready)
        best = min(results, key=lambda r: (r.robust_time_s or r.lap_time_s)
                   + getattr(r, "lift_s", 0.0))
        evaluator_.use_switches((best.switch_m,))

    choose_switch(evaluator, [splits] + authored)

    # Screen the systematic family with one simulation each and climb only the
    # best of it. A climb is tens to hundreds of simulations, so twelve screens
    # cost less than a tenth of one climb -- which is what makes it affordable
    # to enumerate shapes rather than name them as they are found.
    screened = []
    # A restart climbs only the map it was handed; see `optimise_many`.
    family = [] if restart else _family(splits, profile)
    if family:
        prepared = [make_executable(
            [ensure_live_exit(sanitise(s, profile), profile) for s in candidate],
            profile, evaluator.reset_zones) for candidate in family]
        scored = []
        for candidate, candidate_result in zip(prepared, evaluator.many(prepared)):
            score = objective.score(candidate_result, start_store_kj)
            if score is not None:
                scored.append((score, candidate))
        scored.sort(key=lambda pair: pair[0])
        screened = [candidate for _score, candidate in scored[:SCREEN_KEEP]]

    fixed = [make_executable(
        [ensure_live_exit(sanitise(s, profile), profile) for s in candidate],
        profile, evaluator.reset_zones)
        for candidate in ([] if restart else
                          _always_climb(splits, profile, objective))]
    seen = {_key(c) for c in screened}
    starts = ([splits] + authored + screened
              + [c for c in fixed if _key(c) not in seen])
    # One climb per starting point, plus the refining climb over the
    # subdivided answer.
    climbs = len(starts) + 3
    finished = [0]

    def report(sweep: int, sweeps: int) -> None:
        if on_progress is not None:
            on_progress(min(1.0, (finished[0] + sweep / sweeps) / climbs))

    def done() -> None:
        finished[0] += 1
        report(0, 1)

    coarse, result = None, None
    best_score = None
    for start in starts:
        candidate, candidate_result = _climb(
            start, evaluator, profile, objective, start_store_kj, max_sweeps,
            on_sweep=report)
        done()
        score = objective.score(candidate_result, start_store_kj)
        if score is not None and (best_score is None or score < best_score):
            coarse, result, best_score = candidate, candidate_result, score

    # One guarded climb survives the enumeration, because what it responds to
    # is not visible until something has been climbed. A full store refusing
    # recovery says the lap should have spent earlier, and the screen cannot
    # see that: it ranks maps by the lap they give, and a map that throws
    # recovery away still gives a lap. Monaco is the case -- short, heavy on
    # brakes, generous allowance, store full long before the end -- and
    # leaving it to the screen cost 0.27 s there while every other circuit was
    # level or better.
    if result is not None and result.refused_mj > REFUSAL_WORTH_CHASING_MJ:
        candidate, candidate_result = _climb(
            _blanket(splits, profile, FRONT_LOAD_FRACTION, fa26.POWER_MAX),
            evaluator, profile, objective, start_store_kj, max_sweeps,
            on_sweep=report)
        done()
        score = objective.score(candidate_result, start_store_kj)
        if score is not None and (best_score is None or score < best_score):
            coarse, result, best_score = candidate, candidate_result, score

    if coarse is None:
        coarse, result = _climb(
            splits, evaluator, profile, objective, start_store_kj, max_sweeps)

    fine.charge_for_lifts(True)
    # The switch point the coarse answer wants, before it is refined.
    if coarse is not None:
        choose_switch(fine, [coarse])

    # Everything above ranked maps against each other at the coarse step, and
    # a map that wins on a 5 m grid is a lead, not a result. It is climbed
    # again at `ds` from where the coarse pass left it -- which is quick,
    # because it starts close -- and only then subdivided.
    if coarse_ds > ds and coarse is not None:
        coarse, result = _climb(coarse, fine, profile, objective,
                                start_store_kj, max_sweeps, on_sweep=report)
        done()
    else:
        result = fine.one(coarse)
    subdivided = subdivide(coarse, profile)
    if len(subdivided) > len(coarse):
        refined, refined_result = _climb(
            subdivided, fine, profile, objective, start_store_kj, max_sweeps,
            on_sweep=report)
        score = objective.score(result, start_store_kj)
        refined_score = objective.score(refined_result, start_store_kj)
        if refined_score is not None and (score is None or refined_score < score):
            coarse, result = refined, refined_result
    if coarse is not None:
        coarse, result = _rebalance(coarse, fine, profile, objective,
                                    start_store_kj)
    coarse = open_the_lap(coarse, profile)
    if approach is not None:
        # The finished map at every switch point, so the advice given with it
        # is the one it was planned for.
        fine.use_switches(approach.switches)
    if objective.name or approach is not None:
        result = fine.one(coarse)
    return coarse, result, evaluator.evaluations + fine.evaluations


#: Rounds of rebalancing at most. Each takes the single best exchange on offer,
#: so this bounds how far the map can drift from what the climb produced.
REBALANCE_ROUNDS = 8


def _less_deploy(split: fa26.Split) -> fa26.Split | None:
    """Lower this split's deployment by one power step."""
    lower = [p for p in POWERS if p < split.deploy_kw]
    if not lower:
        return None
    return _with_deploy(split, lower[-1])


def _rebalance(splits, evaluator, profile, objective: Objective,
               start_store_kj: float):
    """Move power from where it earns least to where it earns most.

    A lap that ends on an empty battery is energy-limited: what it can spend is
    fixed by the store and the harvest, so raising one split starves a later one
    and lowering one only strands the energy. Neither pays on its own, and a
    climb made of single-split moves can stop there. On the 1.1.0 model it
    did: nine exchanges of 50 kW between two splits made the model's own Austin
    lap quicker, the best by 0.098 s. Most of that was the model rather than the
    search -- with power delivered where the car delivers it, the climb levels
    its own rates at Austin, Albert Park, Monza and Spa, and leaves one exchange
    worth 0.011 s at Suzuka. This is the guarantee that none is left: at an
    optimum the last kilojoule spent in each place returns the same time, and
    while it does not, an exchange pays. It costs a few seconds a solve.

    The climb's paired moves already trade harvesting for deployment, which is
    what an energy-neutral map needs. This is the other trade: deployment for
    deployment. Every exchange from the current map is simulated at once, the
    best one is kept, and that repeats until none improves the score.
    """
    best = list(splits)
    best_result = evaluator.one(best)
    best_score = objective.score(best_result, start_store_kj)
    if best_score is None:
        return best, best_result
    for _round in range(REBALANCE_ROUNDS):
        trials = []
        for j, up_from in enumerate(best):
            up = _more_deploy(up_from)
            if up is None:
                continue
            up = ensure_live_exit(up, profile)
            for i, down_from in enumerate(best):
                if i == j or down_from.deploy_kw <= 0:
                    continue
                down = _less_deploy(down_from)
                if down is None:
                    continue
                trial = list(best)
                trial[i] = ensure_live_exit(sanitise(down, profile), profile)
                trial[j] = up
                trials.append(make_executable(trial, profile, evaluator.reset_zones))
        if not trials:
            break
        chosen = None
        for trial, result in zip(trials, evaluator.many(trials)):
            score = objective.score(result, start_store_kj)
            if score is not None and score < best_score - 1e-4:
                best_score, chosen = score, (trial, result)
        if chosen is None:
            break
        best, best_result = chosen
    return best, best_result


def open_the_lap(splits, profile):
    """Let the opening split deploy, by asking the last one for more.

    A lap begins with the car already at full throttle, so whatever the last
    split of the lap established is the demand it carries across the line, and
    demand cannot be raised under throttle. Measured on six maps at Austin, the
    opening delivers if and only if the last split of the lap commands *more*
    than it:

        opening 300, final 300 -> 1 kJ          opening 200, final 200 -> 0 kJ
        opening 200, final 200 -> 0 kJ          opening 200, final 200 -> 0 kJ
        opening 190, final 190 -> 0 kJ          opening 199, final 350 -> 1364 kJ

    Equal is not enough, which is why a map of uniform power can never use its
    own opening -- and why every map this tool has written so far has thrown
    away the first seven seconds of the lap. That stretch is the third largest
    energy sink on the circuit.

    The mechanism behind the strictness is not pinned down, and only one of the
    six maps is a positive case, so this raises the last split rather than
    trusting a margin of one kilowatt: enough to be unambiguous, and the end of
    a lap is the run to the line, where the power is worth having anyway.
    """
    live = [s for s in splits if s.deploy_kw > 0]
    if len(live) < 2:
        return splits
    opening, final = live[0], live[-1]
    if opening.start_m > OPENING_M or final.deploy_kw > opening.deploy_kw:
        return splits
    want = min(fa26.POWER_MAX, opening.deploy_kw + OPENING_MARGIN_KW)
    if want <= final.deploy_kw:
        return splits
    return [fa26.Split(s.start_m, s.deploy_end_m, s.clip_start_m, s.end_m,
                       want, s.clip_kw) if s is final else s
            for s in splits]


def _climb(splits, evaluator, profile, objective: Objective,
           start_store_kj: float, max_sweeps: int = 8, on_sweep=None):
    """Hill climb the map. Returns (splits, result).

    The acceptance rule is unchanged from the sequential version: walk the
    candidates in order and take every one that improves on the running best.
    Only the simulation is batched, so the map that comes out is the map that
    came out before.
    """
    best = [ensure_live_exit(sanitise(fa26.Split(*vars(s).values()), profile), profile)
            for s in splits]
    best = make_executable(best, profile, evaluator.reset_zones)

    best_result = evaluator.one(best)
    best_score = objective.score(best_result, start_store_kj)
    if best_score is None:
        # Seed is inadmissible: strip deployment back until it is.
        for i, s in enumerate(best):
            best[i] = fa26.Split(s.start_m, s.start_m, fa26.CLIP_OFF, s.end_m, 0, 0)
        best_result = evaluator.one(best)
        best_score = objective.score(best_result, start_store_kj)
        if best_score is None:
            return best, best_result

    n_splits = len(best)
    # Every accepted map has to be one the car can actually drive. Scoring
    # alone will not enforce it: the simulator obeys the no-increase rule, so
    # an illegal move simulates as a no-op and the climb is indifferent to it.
    # Within a sweep the two loops each walk the splits once, so position in
    # them is a usable finer measure than the sweep boundary alone -- five
    # updates over sixteen seconds left the bar looking stuck.
    steps = max(1, 2 * n_splits)
    for sweep in range(max_sweeps):
        improved = False

        def tick(step: int) -> None:
            if on_sweep is not None:
                on_sweep(sweep + step / steps, max_sweeps)

        # Single-split moves. Every trial replaces position i outright, so an
        # acceptance earlier in the sweep cannot change a later trial: the whole
        # variant set for a split can be simulated at once.
        for i in range(n_splits):
            variants = _variants(best[i], profile)
            trials = []
            for candidate_split in variants:
                trial = list(best)
                trial[i] = candidate_split
                trials.append(make_executable(trial, profile, evaluator.reset_zones))
            for trial, result in zip(trials, evaluator.many(trials)):
                score = objective.score(result, start_store_kj)
                if score is not None and score < best_score - 1e-4:
                    best, best_score, best_result = trial, score, result
                    improved = True
            tick(i + 1)

        # Paired moves. An energy-neutral strategy cannot add deployment without
        # also adding the harvesting to pay for it: on its own the first breaks
        # the constraint and the second only costs time, so single-split moves
        # stall with the car deploying almost nothing.
        #
        # This loop stays sequential. Unlike the single-split sweep, an
        # acceptance here changes what the following candidates are built from,
        # and a batched version that rebuilt them after each acceptance did not
        # reproduce the original search: three of nine reference solves came out
        # with different maps, one of them slower. It was also no faster,
        # because rebuilding the pair list on every acceptance cost more than
        # the batching saved. The memo still applies through `evaluator`.
        for j in range(n_splits):
            deploy_more = _more_deploy(best[j])
            if deploy_more is None:
                continue
            for i in range(n_splits):
                if i == j:
                    continue
                harvest_more = _more_harvest(best[i])
                if harvest_more is None:
                    continue
                harvest_more = ensure_live_exit(sanitise(harvest_more, profile), profile)
                trial = list(best)
                trial[i], trial[j] = harvest_more, ensure_live_exit(deploy_more, profile)
                trial = make_executable(trial, profile, evaluator.reset_zones)
                result = evaluator.one(trial)
                score = objective.score(result, start_store_kj)
                if score is not None and score < best_score - 1e-4:
                    best, best_score, best_result = trial, score, result
                    improved = True
            tick(n_splits + j + 1)

        if on_sweep is not None:
            # A converged climb gives up the rest of its sweeps, so report it
            # as complete rather than leaving the bar stuck part way.
            on_sweep(max_sweeps if not improved else sweep + 1, max_sweeps)
        if not improved:
            break
    return best, best_result


STORE_CAPACITY_KJ = 4000.0

#: Where a race lap should live: enough in hand for a defensive burst, not so
#: much that the car is carrying energy it never spends.
RACE_WORKING_FRACTION = 0.5


def starting_store_kj(lap, strategy: str) -> float:
    """Energy in the store as a lap of this type would begin.

    Asking the driver for a starting percentage was a guess that changed the
    answer, so it is derived from how the strategies are meant to be used:

    * a recharge lap is driven when the store is low, so it starts where the
      recorded lap started;
    * a qualifying lap follows a recharge lap, so it starts full;
    * a race lap repeats, so it starts where the recorded lap crossed the line,
      which is the only measurement of a real racing state available.
    """
    measured = lap.store_kj[0]
    if strategy == "quali":
        return STORE_CAPACITY_KJ
    if strategy == "recharge":
        return min(measured, STORE_CAPACITY_KJ * 0.5)
    # A race lap repeats, so it should start from a healthy working level rather
    # than from whatever charge the recorded lap happened to carry -- a lap
    # recorded on an empty battery would otherwise plan for an empty battery.
    return max(measured, STORE_CAPACITY_KJ * RACE_WORKING_FRACTION)


def sensitivity(splits, car, longitudinal, profile, powertrain, objective,
                levels=(0.3, 0.5, 0.7, 0.9), ds: float = 2.5):
    """How a finished plan behaves if the lap starts with more or less energy.

    The starting level is never exactly what was planned for, so this reports
    the spread rather than pretending one number is definitive.
    """
    out = []
    for fraction in levels:
        start = STORE_CAPACITY_KJ * fraction
        result = sim26.simulate(splits, car, longitudinal, profile, powertrain,
                                start_store_kj=start,
                                harvest_cap_kj=objective.harvest_cap_mj * 1000,
                                ds=ds, settle=not objective.one_shot)
        out.append((fraction, result))
    return out
