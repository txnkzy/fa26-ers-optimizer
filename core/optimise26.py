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

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cars"))

import fa26
import sim26

POWER_STEP = 50.0
POWERS = [0.0, 50.0, 100.0, 150.0, 200.0, 250.0, 300.0, 350.0]


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
    #: A repeating lap has to be neutral, but neutral at zero is still neutral,
    #: and that is where the search settled: deploy exactly equalled harvest
    #: with the store pinned empty. The first lap off a full battery was quick
    #: and every lap after it was slow. A race lap has to keep a usable buffer,
    #: so it must stay above this fraction of the store all lap.
    min_store_fraction: float = 0.0

    def score(self, result, start_store_kj: float) -> float | None:
        """Lower is better. None means the plan is not admissible."""
        if result.harvested_mj > self.harvest_cap_mj + 1e-6:
            return None
        if self.min_store_fraction > 0.0 and                 result.store_min_kj < STORE_CAPACITY_KJ * self.min_store_fraction - 1e-6:
            return None
        if self.energy_neutral and                 result.store_end_kj < result.store_start_kj - 1e-6:
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
            return -result.store_end_kj * 1000.0 + result.lap_time_s
        return result.robust_time_s or result.lap_time_s


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
    return [ensure_live_exit(sanitise(c, profile), profile) for c in out]


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


class Evaluator:
    """Simulates candidate maps, remembering ones it has already seen."""

    def __init__(self, car, longitudinal, profile, powertrain, ds,
                 start_store_kj, harvest_cap_kj, settle, reset_zones=(),
                 speed_zones=(), charges=()):
        self._fixed = (car, longitudinal, profile, powertrain, ds,
                       start_store_kj, harvest_cap_kj, settle)
        self.reset_zones = tuple(tuple(z) for z in reset_zones)
        self.speed_zones = tuple(tuple(z) for z in speed_zones)
        self.charges = tuple(charges)
        self._cache = {}
        self.evaluations = 0

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

    def _simulate(self, splits):
        car, longitudinal, profile, powertrain, ds, start, cap, settle = self._fixed
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


def optimise(splits, car, longitudinal, profile, powertrain, objective: Objective,
             start_store_kj: float, max_sweeps: int = 8, ds: float = 2.5,
             extra_seeds=(), reset_zones=(), speed_zones=(), on_progress=None):
    """Solve coarse, subdivide the answer, then refine. Returns (splits, result, evals).

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
    evaluator = Evaluator(car, longitudinal, profile, powertrain, ds,
                          start_store_kj, cap_kj, settle, reset_zones,
                          speed_zones, charges)
    starts = [splits] + [list(extra) for extra in extra_seeds]
    # One climb per starting point, plus the refining climb over the
    # subdivided answer.
    climbs = len(starts) + 1
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
    if coarse is None:
        coarse, result = _climb(
            splits, evaluator, profile, objective, start_store_kj, max_sweeps)

    fine = subdivide(coarse, profile)
    if len(fine) > len(coarse):
        refined, refined_result = _climb(
            fine, evaluator, profile, objective, start_store_kj, max_sweeps,
            on_sweep=report)
        score = objective.score(result, start_store_kj)
        refined_score = objective.score(refined_result, start_store_kj)
        if refined_score is not None and (score is None or refined_score < score):
            coarse, result = refined, refined_result
    evaluations = evaluator.evaluations
    return coarse, result, evaluations


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
