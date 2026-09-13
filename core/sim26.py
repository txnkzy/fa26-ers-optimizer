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

    data = (n, step, ceiling, gas, deploy_frac, harvest_frac, full_throttle,
            on_throttle, grip, drag_k, in_reset, cut_kph)
    cache[key] = (longitudinal, data)
    return data


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


def simulate(splits, car, longitudinal, profile, powertrain,
             start_store_kj: float, harvest_cap_kj: float,
             store_capacity_kj: float = 4000.0, ds: float = 2.5,
             settle: bool = True, reset_zones: "tuple" = (),
             speed_zones: "tuple" = ()) -> Result:
    """Integrate one lap.

    `settle` runs a throwaway lap first and measures the second, which is right
    for a lap that repeats: the store ends where it began and the plan is
    sustainable. It is wrong for a one-shot qualifying lap. Because the store
    carries from the settling lap into the measured one, settling forces
    deploy to equal harvest -- a Madrid lap came back 6.77 MJ deployed against
    6.76 harvested, ending on the same charge it started, with 1.69 MJ of
    harvest refused by a full battery. A push lap should arrive full and leave
    empty, so it is simulated from the given charge in a single pass.
    """
    length = profile.length_m
    (n, step, ceiling, gas_at, deploy_frac, harvest_frac, full_throttle_at,
     on_throttle_at, grip_at, drag_k_at, in_reset_at, cut_kph_at) = _geometry(
        profile, longitudinal, ds, tuple(map(tuple, reset_zones)),
        tuple(map(tuple, speed_zones)))

    # The map is fixed for the whole run, so its commands are read once rather
    # than once per lap of the settle-and-measure pair.
    commands = [fa26.map_command_at(splits, i * step, length) for i in range(n)]

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
    for i in range(max(0, n - int(PRIME_M / step)), n):
        d = i * step
        if not on_throttle_at[i]:
            throttle_time = 0.0
            demand_kw = 0.0
            prev_level_kw = 0.0
            continue
        throttle_time += step / max(profile.measured_speed_at(d), 1.0)
        cmd, clip = commands[i]
        if throttle_time <= powertrain.floor_hold_s or in_reset_at[i]                 or gas_at[i] < fa26.DEMAND_RESET_GAS:
            demand_kw = cmd
        else:
            demand_kw = min(demand_kw, cmd)
        prev_level_kw = powertrain.deploy_kw(
            demand_kw, profile.measured_speed_at(d),
            full_throttle=(throttle_time <= powertrain.floor_hold_s
                           and clip <= 0.0))
    store = start_store_kj
    store_start = store
    deployed = harvested = 0.0
    store_min = store
    capped = False
    time_s = 0.0
    speeds, dists = [], []

    refused = 0.0
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
        for i in range(n):
            d = i * step
            cmd_deploy, cmd_clip = commands[i]
            full_throttle = full_throttle_at[i]
            gas = gas_at[i]
            on_throttle = on_throttle_at[i]

            # Estimate the step duration first: the store cannot supply more
            # than it holds, and that limit is an energy over a time, so it
            # needs dt. Using the entry speed is close enough at this step size
            # and is corrected by the exact dt below.
            dt_guess = step / max(v, 1.0)

            # A split's super-clip section is harvesting, not deploying, so the
            # 200 kW floor does not apply there. Applying it anyway had the car
            # deploying and harvesting in the same step, which drained the store
            # across the lap while the totals still looked balanced.
            clip_kw = 0.0
            if full_throttle and cmd_clip > 0.0 and harvested < harvest_cap_kj:
                clip_kw = min(cmd_clip, max_harvest)
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
            throttle_time = throttle_time + dt_guess if on_throttle else 0.0

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
                in_reset = in_reset_at[i]
                if just_applied or in_reset or gas < reset_gas:
                    demand_kw = cmd_deploy
                else:
                    demand_kw = min(demand_kw, cmd_deploy)

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
                level = deploy_limit(demand_kw, v, full_throttle=floor_applies)

                # Article C5.12.7: a track may declare sectors with a
                # higher threshold than the usual 210 kph, inside which power
                # may still be cut instantly at speed.
                if level < prev_level_kw and v * 3.6 >= cut_kph_at[i]:
                    # Nor can the level cut instantly. The manual works the
                    # example on p.9: at 300 km/h, over 300 kW down to zero
                    # takes about 250 m -- 3.0 s, the whole reduction at
                    # 100 kW/s. Gating the ramp on power above 100 kW as well
                    # as speed let the last 100 kW vanish at once and arrived
                    # 83 m early.
                    level = max(level, prev_level_kw - ramp_down * dt_guess)
                prev_level_kw = level

                # Slot mean, and the measured concave curve -- see
                # fa26.DEPLOY_THROTTLE_CURVE. `gas` is the slot maximum, which
                # answers "was the driver ever flat here", not "how much power
                # did the car deliver across these 2.5 m".
                target = level * deploy_frac[i]
                deploy_kw = min(target, store / max(dt_guess, 1e-6))
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
            drive -= clip_kw * 1000.0 / max(v, 1.0) / motor_eff
            # One friction budget: what the tyre is using sideways is not
            # available for driving.
            drive = min(drive, traction_force * grip_at[i])

            accel = (drive - drag_k_at[i] * v * v) / mass
            v_free = max(v * v + 2.0 * accel * step, 1.0) ** 0.5
            v_next = max(1.0, min(v_free, ceiling[(i + 1) % n]))
            dt = step / max((v + v_next) * 0.5, 1.0)

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
                harvest_kw = harvest_ceiling(v) * harvest_frac[i]

            gained = harvest_kw * dt
            if harvested + gained > harvest_cap_kj:
                gained = max(0.0, harvest_cap_kj - harvested)
                capped = True
            room = max(0.0, store_capacity_kj - store)
            if gained > room:
                refused += gained - room
                gained = room

            used = min(deploy_kw * dt, store)
            store = min(store_capacity_kj, store - used + gained)
            harvested += gained
            deployed += used
            store_min = min(store_min, store)

            v = v_next
            time_s += dt
            if measuring:
                speeds.append(v)
                dists.append(d)

    return Result(lap_time_s=time_s, deployed_mj=deployed / 1000.0,
                  harvested_mj=harvested / 1000.0, store_end_kj=store,
                  store_min_kj=store_min, store_start_kj=store_start,
                  harvest_capped=capped,
                  refused_mj=refused / 1000.0,
                  speed=speeds, dist=dists)
