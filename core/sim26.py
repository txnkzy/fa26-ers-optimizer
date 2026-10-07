"""Whole-lap simulation for the FA26.

FA26 deployment maps tile the lap contiguously rather than placing isolated
zones, so the lap is integrated in one pass over a distance grid instead of
being cut into independent segments.

Quasi-steady-state: a backward pass sets the highest speed permitted at each
point by the driver's cornering speeds and by braking; a forward pass then
accelerates into that ceiling under engine plus ERS thrust, less super-clip.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cars"))

import fa26

#: Below this speed the car may cut ERS-K power instantly rather than ramping
#: it down. Track Speed Threshold zones can raise it, which is not modelled.
INSTANT_CUT_KPH = 210.0

#: How far back along the lap to walk when establishing the state the car
#: arrives at the start/finish line in. Long enough to cover the run onto any
#: main straight, short enough to cost nothing.
PRIME_M = 600.0

#: A sweeper is a corner taken on the throttle, without braking, at this speed
#: or more and under at least this share of the car's lateral grip. Arriving
#: at one faster than it can be taken flat means a lift in the middle of a
#: high-load corner, which the driver has to judge afresh every lap.
SWEEPER_MIN_KPH = 150.0
SWEEPER_MIN_LOAD = 0.5
#: Sweeper points closer than this are one corner.
SWEEPER_JOIN_M = 75.0


def _geometry(profile, longitudinal, ds, reset_zones, speed_zones):
    """Everything about a lap that depends only on position, computed once.

    A solve simulates the same lap a few thousand times with a different map
    each time. The speed ceiling, the throttle classification, the grip left
    under lateral load, the drag coefficient and the two throttle curves are
    all functions of track position alone, so recomputing them inside every
    evaluation was nearly all of the run time: a Monza race solve spent 63 of
    its 64 seconds inside `simulate`, and almost none of that on the map being
    tested. Cached on the profile, keyed by step size and zones, and checked
    against the calibration object's identity so a refit cannot be handed a
    stale ceiling.
    """
    cache = getattr(profile, "_sim_geometry", None)
    if cache is None:
        cache = profile._sim_geometry = {}
    key = (ds, reset_zones, speed_zones)
    hit = cache.get(key)
    if hit is not None and hit[0] is longitudinal:
        return hit[1]

    length = profile.length_m
    n = max(2, int(length / ds))
    step = length / n

    # Backward pass: the fastest the car may be at each point and still slow to
    # the next constraint under the calibrated braking limit. Run twice around
    # the lap so the wrap at the start line is consistent.
    ceiling = [min(profile.cap_at(i * step), 400.0) for i in range(n)]
    for _ in range(2):
        for i in range(n - 1, -1, -1):
            nxt = ceiling[(i + 1) % n]
            reach = (nxt * nxt + 2.0 * longitudinal.braking_decel(nxt) * step) ** 0.5
            if reach < ceiling[i]:
                ceiling[i] = reach

    # Which point each ceiling is there for: itself where the driver's speed
    # binds, or the corner a braking run is slowing the car for.
    raw = [min(profile.cap_at(i * step), 400.0) for i in range(n)]
    origin = list(range(n))
    for _ in range(2):
        for i in range(n - 1, -1, -1):
            if ceiling[i] < raw[i] - 1e-9:
                origin[i] = origin[(i + 1) % n]

    slot = profile._slot
    gas = [0.0] * n
    deploy_frac = [0.0] * n
    harvest_frac = [0.0] * n
    full_throttle = [False] * n
    on_throttle = [False] * n
    grip = [0.0] * n
    drag_k = [0.0] * n
    in_reset = [False] * n
    cut_kph = [INSTANT_CUT_KPH] * n
    for i in range(n):
        d = i * step
        gas[i] = profile.gas[slot(d)]
        mean = profile.gas_mean[slot(d)]
        deploy_frac[i] = fa26.deploy_throttle_frac(mean)
        harvest_frac[i] = fa26.harvest_throttle_frac(mean)
        full_throttle[i] = profile.full_throttle_at(d)
        on_throttle[i] = profile.on_throttle_at(d)
        grip[i] = profile.grip_left(d)
        drag_k[i] = longitudinal.drag.k_at(d)
        in_reset[i] = any(a <= d < b for a, b in reset_zones)
        for lo, hi, kph in speed_zones:
            if lo <= d < hi:
                cut_kph[i] = kph
                break

    # Sweepers, each a run of neighbouring points named by where it begins,
    # and for every point the sweeper its ceiling belongs to, or -1.
    lat_max = getattr(profile, "lat_max", 0.0) or 0.0
    sweeper = [-1] * n
    last, gap = -1, int(SWEEPER_JOIN_M / step)
    for i in range(n):
        d = i * step
        if (SWEEPER_MIN_KPH <= raw[i] * 3.6 and raw[i] < 400.0
                and on_throttle[i] and profile.brake_mean[slot(d)] < 0.05
                and lat_max > 0.0
                and profile.lat[slot(d)] >= SWEEPER_MIN_LOAD * lat_max):
            # One corner, not several: the load dips for a few metres
            # through a long sweeper, and an S joins up too.
            sweeper[i] = sweeper[last] if last >= 0 and i - last <= gap else i
            last = i
    if sweeper[0] >= 0 and sweeper[-1] >= 0:       # one corner across the line
        for i in range(n):
            if sweeper[i] != 0:
                break
            sweeper[i] = sweeper[-1]
    lifting = [-1] * n
    for i in range(n):
        d = i * step
        if on_throttle[i] and profile.brake_mean[slot(d)] < 0.05:
            lifting[i] = sweeper[origin[i]]

    data = (n, step, ceiling, gas, deploy_frac, harvest_frac, full_throttle,
            on_throttle, grip, drag_k, in_reset, cut_kph, lifting)
    cache[key] = (longitudinal, data)
    return data


@dataclass
class Carry:
    """The state the car passes a point in, so a run can be continued from it.

    Entry speed and entry charge are not independent: deploying out of the last
    corner buys speed at the line and spends the charge that would have gone
    down the straight. Modelling them as two separate inputs is what made the
    old flip study mispredict by a factor of eighteen at Albert Park and in the
    wrong direction at Monza. Carrying this across the line instead makes them
    one decision, because they are one.

    The three power fields are the demand latch. The regulation caps the level
    the map may *ask* for, and that cap only lifts when the driver comes off
    the throttle, so a run continued without them would let the second half ask
    for power the first half had already forfeited.
    """

    speed_ms: float
    store_kj: float
    demand_kw: float = 0.0
    prev_level_kw: float = 0.0
    throttle_time_s: float = 0.0


@dataclass
class Result:
    lap_time_s: float
    deployed_mj: float
    harvested_mj: float
    store_end_kj: float
    store_min_kj: float
    harvest_capped: bool
    store_start_kj: float = 0.0    # charge as the measured lap began
    #: Lap time averaged over several plausible starting charges, where the
    #: search was told to care about that. Zero means it was not.
    robust_time_s: float = 0.0
    refused_mj: float = 0.0        # harvest the full store had no room for
    speed: list[float] = field(default_factory=list)
    dist: list[float] = field(default_factory=list)
    #: The state the car finishes in, for a run that continues into another.
    exit: "Carry | None" = None
    #: Speed the driver has to lift off in each sweeper, in kph, keyed by
    #: where the sweeper begins in metres. See SWEEPER_MIN_KPH.
    lifts: dict = field(default_factory=dict)


def run_to_line(before, after, switch_m: float, car, longitudinal, profile,
                powertrain, start_store_kj: float, harvest_cap_kj: float,
                approach_m: float = PRIME_M, **kw) -> "Carry":
    """The state the car crosses the line in, having switched maps on the way.

    `switch_m` metres before the line the driver moves from `before` to
    `after`, and the run is integrated through the change rather than assumed.
    What comes back is one state, not two independent ones: deploying early
    raises the speed at the line and lowers the charge, and the caller sees
    both together because they are the same decision.

    The approach belongs to the previous lap, whose own allowance is not
    knowable from here, so harvesting over it is priced at the cap given.
    """
    length = profile.length_m
    approach_m = min(approach_m, length)
    begin = length - approach_m
    change = length - min(switch_m, approach_m)
    state = Carry(speed_ms=profile.measured_speed_at(begin),
                  store_kj=start_store_kj)
    for splits, window in ((before, (begin, change)), (after, (change, length))):
        if window[1] - window[0] < 1.0:
            continue
        state = simulate(splits, car, longitudinal, profile, powertrain,
                         start_store_kj=state.store_kj,
                         harvest_cap_kj=harvest_cap_kj, entry=state,
                         window=window, **kw).exit
    return state


def _commands_along(splits, n, step, length):
    """Every step's (deploy, clip) command, built in one pass over the splits.

    `fa26.map_command_at` answers the same question for one point and scans the
    split list to do it. Asked for every step of every simulation that is
    12.3 million scans in a single Spa race search, for a list that is already
    in the order the steps visit it.

    The answer is the same one, and the assertion that it is runs in the tests
    rather than here.
    """
    out = [(0.0, 0.0)] * n
    ordered = sorted(splits, key=lambda s: s.start_m)
    index = 0
    for i in range(n):
        d = (i * step) % length
        while index < len(ordered) and ordered[index].end_m <= d:
            index += 1
        if index >= len(ordered):
            continue
        split = ordered[index]
        if split.start_m > d:
            continue                        # in a gap between splits
        if d < split.deploy_end_m:
            out[i] = (split.deploy_kw, 0.0)
        elif d < split.clip_start_m:
            out[i] = (0.0, 0.0)             # the deliberate neutral run
        else:
            out[i] = (0.0, split.clip_kw)
    return out

def simulate(splits, car, longitudinal, profile, powertrain,
             start_store_kj: float, harvest_cap_kj: float,
             store_capacity_kj: float = 4000.0, ds: float = 2.5,
             settle: bool = True, reset_zones: "tuple" = (),
             speed_zones: "tuple" = (), entry: "Carry | None" = None,
             window: "tuple[float, float] | None" = None,
             trace: "list | None" = None) -> Result:
    """Integrate one lap.

    `trace`, when given a list, receives one row per measured step: distance,
    time, speed, power delivered and harvested, charge, and what limited the
    car there ("ceiling", "grip" or "power"). For diagnosis; a solve never
    asks for it.

    `settle` runs a throwaway lap first and measures the second, which is right
    for a lap that repeats: the store ends where it began and the plan is
    sustainable. It is wrong for a one-shot qualifying lap. Because the store
    carries from the settling lap into the measured one, settling forces
    deploy to equal harvest -- a Madrid lap came back 6.77 MJ deployed against
    6.76 harvested, ending on the same charge it started, with 1.69 MJ of
    harvest refused by a full battery. A push lap should arrive full and leave
    empty, so it is simulated from the given charge in a single pass.
   
    `entry` and `window` run part of a lap from a known state rather than the
    whole of it from the driver's. Together they are how the run down to the
    line is priced: the last few hundred metres are integrated under whichever
    map is live there, and the speed and charge that come out are what the
    measured lap starts from. `start_store_kj` is ignored when `entry` is
    given, and settling is meaningless for a partial run, so it is not done.
    """
    length = profile.length_m
    (n, step, ceiling, gas_at, deploy_frac, harvest_frac, full_throttle_at,
     on_throttle_at, grip_at, drag_k_at, in_reset_at, cut_kph_at, lifting_at) = _geometry(
        profile, longitudinal, ds, tuple(map(tuple, reset_zones)),
        tuple(map(tuple, speed_zones)))

    # The map is fixed for the whole run, so its commands are read once rather
    # than once per lap of the settle-and-measure pair.
    # The steps are visited in order and the splits are in order with them,
    # so both are walked once together. Reading the split list afresh for
    # every step was 12.3 million linear scans in a single Spa race search.
    commands = _commands_along(splits, n, step, length)

    # Bind the hot lookups once. The inner loop runs n times per lap and twice
    # per evaluation when settling, so attribute resolution on car, powertrain
    # and the fa26 module is a measurable share of a solve.
    mass = car.total_mass_kg
    traction_force = longitudinal.traction_ms2 * mass
    engine_force = car.best_tractive_force
    wastegate = car.boost_wastegate
    deploy_limit = powertrain.deploy_kw
    thrust_n = powertrain.thrust_n
    floor_hold_s = powertrain.floor_hold_s
    ramp_down = powertrain.ramp_down_kw_s
    harvest_ceiling = fa26.harvest_kw_at
    max_harvest = fa26.MAX_HARVEST_KW
    motor_eff = fa26.MOTOR_EFFICIENCY
    reset_gas = fa26.DEMAND_RESET_GAS

    # Start where the driver was, then let one settling lap remove the effect of
    # that guess before the measured lap begins.
    v = min(profile.measured_speed_at(0.0), ceiling[0])
    prev_deploy_kw = 0.0
    #: The power level the map asked for when the throttle was picked up. The
    #: regulation caps the demand, not the delivery, so this is tracked apart
    #: from prev_deploy_kw.
    demand_kw = 0.0
    #: The regulated power level, before the throttle modulates it. The
    #: reduction rate applies here, not to delivered power.
    prev_level_kw = 0.0
    throttle_time = 0.0

    # A lap does not begin with the car freshly on the throttle. Where the
    # start/finish line sits on a straight the driver crosses it flat, having
    # been flat for some time, so the demand is already latched at whatever the
    # last split of the map asked for -- and a first split asking for more is
    # refused. Starting from a clean slate instead let the model grant the
    # opening split anything: at Suzuka it awarded the full 350 kW across the
    # line where the car actually held 100, and because a qualifying solve runs
    # without a settling lap, the search never saw the refusal and wrote the
    # 350 in. Walking the tail of the lap first establishes the state the car
    # really arrives in. Nothing here touches the store or any total; it only
    # decides what the car is already committed to.
    lo, hi = 0, n
    if window is not None:
        lo = int(window[0] / step)
        hi = max(lo + 1, int(window[1] / step))
    for i in (() if entry is not None else range(max(0, n - int(PRIME_M / step)), n)):
        d = i * step
        if not on_throttle_at[i]:
            throttle_time = 0.0
            demand_kw = 0.0
            prev_level_kw = 0.0
            continue
        # The clock runs from the knee, as in the main loop below.
        if gas_at[i] < fa26.DEMAND_RESET_GAS:
            throttle_time = 0.0
        else:
            throttle_time += step / max(profile.measured_speed_at(d), 1.0)
        cmd, clip = commands[i]
        if throttle_time <= powertrain.floor_hold_s or in_reset_at[i]                 or gas_at[i] < fa26.DEMAND_RESET_GAS:
            demand_kw = cmd
        else:
            demand_kw = min(demand_kw, cmd)
        prev_level_kw = powertrain.deploy_kw(
            demand_kw, profile.measured_speed_at(d),
            full_throttle=(throttle_time <= powertrain.floor_hold_s
                           and clip <= 0.0), distance_m=d)
    # Without an `entry` the lap arrives on its own map -- the driver was on it
    # when they got back on the power for the final corner, either because it
    # is a race stint or because they switched before the corner, which is
    # what a map that uses its opening asks them to do. Arriving any other
    # way is `run_to_line`'s job, and whether the opening is granted on
    # arrival is decided at the line in the main loop.
    if entry is not None:
        v = min(entry.speed_ms, ceiling[lo % n])
        demand_kw = entry.demand_kw
        prev_level_kw = entry.prev_level_kw
        throttle_time = entry.throttle_time_s
        start_store_kj = entry.store_kj
        settle = False
    store = start_store_kj
    store_start = store
    deployed = harvested = 0.0
    store_min = store
    capped = False
    time_s = 0.0
    speeds, dists = [], []

    refused = 0.0
    lifts = {}
    # One settling lap, not a run to convergence. Iterating until a settling
    # lap ends where it began was tried and is worse: on VRC's own maps the
    # model deploys about 0.5 MJ a lap more than it harvests, so the fixed
    # point it converges onto is an empty store, and every reference lap then
    # came back store-starved -- mean lap error 1.18% against 0.30%, and a
    # limit cycle rather than a fixed point, because a flat store throttles
    # deployment and lets harvest overtake it again. The overshoot is the thing
    # to fix; settling harder only makes the model act on it.
    for lap_index in ((0, 1) if settle else (1,)):
        measuring = lap_index == 1
        if measuring:
            time_s = 0.0
            deployed = harvested = 0.0
            store_min = store
            store_start = store
            refused = 0.0
            capped = False
            lifts = {}
        for i in range(lo, hi):
            d = (i % n) * step
            cmd_deploy, cmd_clip = commands[i % n]
            full_throttle = full_throttle_at[i % n]
            gas = gas_at[i % n]
            on_throttle = on_throttle_at[i % n]

            # Estimate the step duration first: the store cannot supply more
            # than it holds, and that limit is an energy over a time, so it
            # needs dt. Using the entry speed is close enough at this step size
            # and is corrected by the exact dt below.
            dt_guess = step / (v if v > 1.0 else 1.0)

            # A split's super-clip section is harvesting, not deploying, so the
            # 200 kW floor does not apply there. Applying it anyway had the car
            # deploying and harvesting in the same step, which drained the store
            # across the lap while the totals still looked balanced.
            clip_kw = 0.0
            if full_throttle and cmd_clip > 0.0 and harvested < harvest_cap_kj:
                clip_kw = cmd_clip if cmd_clip < max_harvest else max_harvest
            # The crossing from deployment into super-clipping is already
            # rate-limited by the deployment ramp below, and the two net out, so
            # clipping needs no separate ramp. Giving it one charged the rate
            # twice and cost a Barcelona replay more than half its harvest --
            # 3.47 MJ against a measured 7.85.

            # The 200 kW floor is a transient obligation, not a permanent one:
            # it must be held for about a second after the throttle opens, and
            # after that power may fall, but only at the ramp rate. Treating it
            # as permanent made the car deploy everywhere the map asked for
            # nothing.
            #
            # The clock starts at the knee, not at the first touch of the
            # pedal. Below the knee the demand re-arms on every step anyway,
            # and the car's floor is timed from the knee: measured, the floor
            # sits at 197 kW half a second after the throttle crosses 60% and
            # is ramping down by 1.0-1.5 s. Counting from 20% instead ran the
            # window out during the squeeze, and at Austin refused a 250 kW
            # split that began 0.29 s after the knee -- the car delivered it,
            # 1.8 MJ through the long right, and the model delivered 0.12.
            throttle_time = throttle_time + dt_guess if gas >= reset_gas else 0.0

            # Deployment follows the throttle, not just full throttle. Gating it
            # on full throttle made every corner exit invisible to the search:
            # commands there changed nothing, so the optimiser left them at zero
            # and the car came out of slow corners with no power -- which is
            # where deployment is worth most, since force is power over speed.
            if not on_throttle:
                demand_kw = 0.0
                prev_level_kw = 0.0

            deploy_kw = 0.0
            if on_throttle and store > 0.0:
                # "The maximum power demand cannot increase under part or full
                # throttle, except when Boost Mode is activated" (manual p.25),
                # or inside a track Power Reset zone. What the regulation binds
                # is the *demand* -- the level the map asks for -- not the power
                # the car happens to be delivering at that instant.
                #
                # Latching delivered power instead made the rule a ratchet:
                # delivery is already cut by throttle scaling, the speed taper,
                # the store limit and the expiry of the 200 kW floor, so the
                # moment it touched zero it could never rise again until the
                # driver lifted. On a Monza lap that killed 393 of 964
                # full-throttle steps -- 39% of full-throttle time, 7.04 MJ of
                # live commands discarded, in dead stretches up to 530 m -- and
                # it left Barcelona untouched, which is why it survived
                # validation there.
                #
                # The demand is read while the throttle is being picked up and
                # the floor is held, which is also why the manual advises
                # starting a split before the point you get back on the power.
                just_applied = throttle_time <= floor_hold_s
                in_reset = in_reset_at[i % n]
                if just_applied or in_reset or gas < reset_gas:
                    demand_kw = cmd_deploy
                elif i % n == 0:
                    # The start/finish line. The car arrives with the demand
                    # it established on the last corner of the previous lap,
                    # on whichever map was live then, and the opening split
                    # is granted only if it asks for strictly less. Equal is
                    # refused, and so is more -- not held at the carried
                    # level as `min` would have it. Every Austin lap with
                    # charge to spend: carried 300 or 350 against an opening
                    # of 200 or 199 deployed 1.36-1.44 MJ, three of three;
                    # 190 against 190 and 0 against 200 deployed nothing.
                    demand_kw = cmd_deploy if cmd_deploy < demand_kw else 0.0
                else:
                    demand_kw = demand_kw if demand_kw < cmd_deploy else cmd_deploy

                # The regulated quantity is the power *level* the PU may
                # demand -- floor and speed taper included. The driver's
                # throttle then modulates delivery within it, and the reduction
                # rate has nothing to say about that: lifting is not a demand
                # reduction. Rate-limiting after the throttle scaling instead
                # held power up as the driver came off the pedal and
                # over-deployed by 0.57 MJ a lap.
                # The floor is an obligation the car takes on when the map
                # asks for nothing. Where the map explicitly commands
                # super-clip, it is asking to harvest at full throttle, and the
                # car obeys: across Red Bull Ring's five clip splits the car
                # deploys essentially nothing, while applying the floor there
                # put 0.54 MJ a lap into regions meant to be generating -- and
                # netted it straight back off the commanded harvest.
                floor_applies = just_applied and cmd_clip <= 0.0
                level = deploy_limit(demand_kw, v, full_throttle=floor_applies,
                                     distance_m=d)

                # Article C5.12.7: a track may declare sectors with a
                # higher threshold than the usual 210 kph, inside which power
                # may still be cut instantly at speed.
                # The ramp applies at any speed outside a declared C5.12.7
                # sector. Measured after the floor expires, a car well below
                # 210 kph still comes down at about 100 kW/s -- 197, 150,
                # 101, then nothing, a second at a time -- where the model had
                # it drop at once and lost 100-200 kJ on every slow-corner
                # exit. A declared sector keeps the instant cut the
                # regulation allows below its own threshold.
                if level < prev_level_kw and (v * 3.6 >= cut_kph_at[i % n]
                                              or cut_kph_at[i % n] <= INSTANT_CUT_KPH):
                    # Nor can the level cut instantly. The manual works the
                    # example on p.9: at 300 km/h, over 300 kW down to zero
                    # takes about 250 m -- 3.0 s, the whole reduction at
                    # 100 kW/s. Gating the ramp on power above 100 kW as well
                    # as speed let the last 100 kW vanish at once and arrived
                    # 83 m early.
                    floor_level = prev_level_kw - ramp_down * dt_guess
                    if floor_level > level:
                        level = floor_level
                prev_level_kw = level

                # Slot mean, and the measured concave curve -- see
                # fa26.DEPLOY_THROTTLE_CURVE. `gas` is the slot maximum, which
                # answers "was the driver ever flat here", not "how much power
                # did the car deliver across these 2.5 m".
                target = level * deploy_frac[i % n]
                drawable = store / (dt_guess if dt_guess > 1e-6 else 1e-6)
                deploy_kw = target if target < drawable else drawable
            prev_deploy_kw = deploy_kw

            # The MGU-K cannot motor and generate at once, so what the car
            # actually does is the net of the two commands. Treating a clip
            # region as suppressing deployment outright was wrong: the car holds
            # its 200 kW floor at throttle application whatever the map says, so
            # a 50 kW clip against that floor is still 150 kW of deployment. A
            # Monza recharge map built on that assumption commanded 0 kW deploy
            # and 50 kW clip, and delivered a big deploy down the back straight
            # -- the driver had to lift to get any regen at all.
            net_kw = deploy_kw - clip_kw
            if net_kw >= 0.0:
                deploy_kw, clip_kw = net_kw, 0.0
            else:
                deploy_kw, clip_kw = 0.0, -net_kw

            drive = engine_force(v, wastegate)
            drive += thrust_n(deploy_kw, v)
            drive -= clip_kw * 1000.0 / (v if v > 1.0 else 1.0) / motor_eff
            # One friction budget: what the tyre is using sideways is not
            # available for driving.
            grip_limit = traction_force * grip_at[i % n]
            if grip_limit < drive:
                drive = grip_limit

            accel = (drive - drag_k_at[i % n] * v * v) / mass
            square = v * v + 2.0 * accel * step
            v_free = (square if square > 1.0 else 1.0) ** 0.5
            limit = ceiling[(i + 1) % n]
            v_next = v_free if v_free < limit else limit
            kept = v if v < v_free else v_free
            if limit < kept:
                # Held below the speed it would otherwise keep, on the
                # throttle and off the brake: the driver is lifting. Where
                # that is for a sweeper, the speed given up is the lift.
                corner = lifting_at[(i + 1) % n]
                if corner >= 0:
                    lifts[corner] = lifts.get(corner, 0.0) + (kept - limit) * 3.6
            if v_next < 1.0:
                v_next = 1.0
            mean_v = (v + v_next) * 0.5
            dt = step / (mean_v if mean_v > 1.0 else 1.0)

            # Harvest whenever the driver is off the throttle, not only under
            # braking: coasting regen is the largest source on a lap. Between
            # closed and full throttle it tapers away.
            harvest_kw = clip_kw
            if not full_throttle:
                # The taper takes the slot MEAN throttle, not the slot maximum. `gas` is
                # the maximum, which is the right question for "was the driver ever
                # flat here" and the wrong one for "how much did the car regenerate
                # across these five metres": over a Monza lap the maximum runs 1.35x
                # the mean in partial-throttle slots, and using it lost 0.5 MJ a lap.
                harvest_kw = harvest_ceiling(v) * harvest_frac[i % n]

            gained = harvest_kw * dt
            if harvested + gained > harvest_cap_kj:
                gained = harvest_cap_kj - harvested
                if gained < 0.0:
                    gained = 0.0
                capped = True
            room = store_capacity_kj - store
            if room < 0.0:
                room = 0.0
            if gained > room:
                refused += gained - room
                gained = room

            drawn = deploy_kw * dt
            used = drawn if drawn < store else store
            store = store - used + gained
            if store > store_capacity_kj:
                store = store_capacity_kj
            harvested += gained
            deployed += used
            if store < store_min:
                store_min = store

            if trace is not None and measuring:
                trace.append((d, time_s, v, used / dt if dt > 0 else 0.0,
                              gained / dt if dt > 0 else 0.0, store,
                              "ceiling" if v_free >= limit else
                              "grip" if drive >= grip_limit else "power"))
            v = v_next
            time_s += dt
            if measuring:
                speeds.append(v)
                dists.append(d)

    leaving = Carry(speed_ms=v, store_kj=store, demand_kw=demand_kw,
                    prev_level_kw=prev_level_kw, throttle_time_s=throttle_time)
    return Result(exit=leaving, lap_time_s=time_s, deployed_mj=deployed / 1000.0,
                  harvested_mj=harvested / 1000.0, store_end_kj=store,
                  store_min_kj=store_min, store_start_kj=store_start,
                  harvest_capped=capped,
                  refused_mj=refused / 1000.0,
                  speed=speeds, dist=dists,
                  lifts={c * step: kph for c, kph in lifts.items()})
