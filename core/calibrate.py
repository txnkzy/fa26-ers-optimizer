"""Fitting the car's longitudinal behaviour from a recorded lap.

Deployment power is known exactly for the FA26, so the force balance can be
solved for drag directly rather than fitted around an unknown:

    F_drag = F_engine + F_ers - m*a

The 2026 car has two aero states -- normal and straight-line -- and which one
applies is set by track zones, not by the driver. There is no telemetry channel
for it (`drs_active` reads zero throughout), so the zones are recovered from the
drag residual instead: binned by track position it separates into two clear
levels rather than a spread.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

MOTOR_EFFICIENCY = 0.93


@dataclass
class DragProfile:
    """Drag coefficient k = F_drag / v^2, indexed by position around the lap.

    An earlier attempt classified this into two aero states, normal and
    straight-line. The data does not support that: binned by position the values
    spread continuously rather than separating into two levels, because the
    residual also carries induced drag in corners, which rises with lateral
    load, and gear-shift torque interruptions. All three are position-dependent
    and repeat lap to lap on the same line, so indexing by position captures
    them together without having to separate them.

    The cost is that the profile belongs to the driver's line on that track, in
    the same way the speed ceiling does.
    """

    bin_m: float
    k: dict[int, float]
    k_default: float
    length_m: float

    def k_at(self, distance_m: float) -> float:
        b = int((distance_m % self.length_m) / self.bin_m)
        return self.k.get(b, self.k_default)

    def drag(self, distance_m: float, speed_ms: float) -> float:
        return self.k_at(distance_m) * speed_ms * speed_ms

    @property
    def low_drag_regions(self) -> list[tuple[float, float]]:
        """Contiguous runs well below the lap median -- indicative of low-drag
        aero zones, reported for inspection rather than used by the model."""
        if not self.k:
            return []
        cut = statistics.median(self.k.values()) * 0.8
        runs, start, prev = [], None, None
        for b in sorted(self.k):
            if self.k[b] <= cut:
                if start is None or (prev is not None and b - prev > 1):
                    if start is not None:
                        runs.append((start * self.bin_m, (prev + 1) * self.bin_m))
                    start = b
                prev = b
            elif start is not None:
                runs.append((start * self.bin_m, (prev + 1) * self.bin_m))
                start, prev = None, None
        if start is not None:
            runs.append((start * self.bin_m, (prev + 1) * self.bin_m))
        return [r for r in runs if r[1] - r[0] >= self.bin_m * 2]


def as_laps(lap_or_laps) -> list:
    """Accept a single Lap or a sequence of them, uniformly."""
    return list(lap_or_laps) if isinstance(lap_or_laps, (list, tuple)) else [lap_or_laps]


def fit_drag_profile(lap_or_laps, car, bin_m: float = 50.0,
                     min_samples: int = 6) -> DragProfile:
    """Drag by position, pooled over however many laps are given.

    A bin needs `min_samples` full-throttle samples before its median is
    trusted, and one lap rarely supplies that everywhere: on a twisty circuit
    most of the track misses the threshold and falls back to a single lap-wide
    median. Monaco fitted 33% of its bins from one lap and 48% from six;
    Madrid 50% against 63%. Since drag by position is a property of the car on
    that line rather than of any one lap, pooling a session is the cheapest
    accuracy there is.
    """
    laps = as_laps(lap_or_laps)
    samples = []
    for lap in laps:
        samples += drag_samples(lap, car)
    if not samples:
        raise ValueError("no usable full-throttle samples for the drag fit")

    bins: dict[int, list[float]] = {}
    for d, _v, k in samples:
        bins.setdefault(int(d // bin_m), []).append(k)
    k = {b: statistics.median(v) for b, v in bins.items() if len(v) >= min_samples}
    default = statistics.median([x[2] for x in samples])
    return DragProfile(bin_m=bin_m, k=k, k_default=default,
                       length_m=max(l.length_m for l in laps))


@dataclass
class Longitudinal:
    drag: DragProfile
    brake_c0: float
    brake_c1: float
    traction_ms2: float
    n_drag: int
    n_brake: int
    #: The speed band the drag fit actually saw, in m/s. A map that makes the
    #: car faster than the laps it was fitted to is extrapolating: Monza's fit
    #: saw p95 294 kph while a proper map runs it at 333, which cost 0.66
    #: points of accuracy. Callers use this to say when a second lap and a
    #: re-solve are worth it.
    observed_speed_p95: float = 0.0
    observed_speed_max: float = 0.0

    def extrapolating_kph(self, speeds) -> float:
        """How far past the calibrated band a simulated lap runs, in kph."""
        if not speeds or self.observed_speed_p95 <= 0.0:
            return 0.0
        return max(0.0, (max(speeds) - self.observed_speed_p95) * 3.6)

    def braking_decel(self, speed_ms: float) -> float:
        return max(1.0, self.brake_c0 + self.brake_c1 * speed_ms * speed_ms)


def _engine_force(car, rpm: float, boost: float, gear: int, mass: float) -> float | None:
    from carphysics import lut_lookup  # FA25 module, car-agnostic helpers
    if not 1 <= gear <= len(car.gears):
        return None
    ratio = car.gears[gear - 1] * car.final_drive
    torque = lut_lookup(car.torque_curve, rpm) * (1.0 + boost)
    return torque * ratio * car.mech_efficiency / car.wheel_radius_m


def drag_samples(lap, car) -> list[tuple[float, float, float]]:
    """(distance, speed, k) for clean full-throttle samples.

    Both directions of MGU-K power have to be in the balance. Super-clipping is
    regeneration *at full throttle*, and it is present in 28% of the samples
    this filter admits at Monza -- p90 202 kW, which at 280 kph is about
    4,800 N of retarding force at the wheels. Leaving it out attributed every
    newton of it to aerodynamics: the fitted k in Monza's super-clip zones came
    out at 1.22-1.30 against a true 0.52-0.54, and the simulation then dragged
    the car down in exactly the places it should have been accelerating. The
    lap-time residual on a measured-power replay was +2.31%; with harvest in
    the balance it is -0.13%.
    """
    mass = car.total_mass_kg
    out = []
    for i in range(1, len(lap) - 1):
        if lap.gas[i] < 0.99 or lap.brake[i] > 0.01 or lap.speed[i] < 55.0:
            continue
        f_eng = _engine_force(car, lap.rpm[i], lap.boost[i], lap.gear[i], mass)
        if f_eng is None:
            continue
        f_ers = lap.deploy_kw[i] * 1000.0 / lap.speed[i] * MOTOR_EFFICIENCY
        # store_kw is + leaving the store, so deploy - store_kw is what the
        # MGU-K put back in: the harvest rate, exactly, with no inference.
        harvest_kw = max(0.0, lap.deploy_kw[i] - lap.store_kw[i])
        f_regen = harvest_kw * 1000.0 / lap.speed[i] / MOTOR_EFFICIENCY
        f_drag = f_eng + f_ers - f_regen - mass * lap.accel[i]
        out.append((lap.dist[i], lap.speed[i], f_drag / (lap.speed[i] ** 2)))
    return out


def fit_aero(lap, car, bin_m: float = 100.0, min_samples: int = 12) -> Aero:
    samples = drag_samples(lap, car)
    if not samples:
        raise ValueError("no usable full-throttle samples for the drag fit")

    bins: dict[int, list[float]] = {}
    for d, _v, k in samples:
        bins.setdefault(int(d // bin_m), []).append(k)
    medians = {b: statistics.median(v) for b, v in bins.items() if len(v) >= min_samples}
    if not medians:
        raise ValueError("not enough samples per bin to separate the aero states")

    values = sorted(medians.values())
    # Split at the midpoint of the largest gap: with two aero states the
    # distribution is bimodal, and this finds the divide without assuming where.
    gaps = [(values[i + 1] - values[i], i) for i in range(len(values) - 1)]
    if not gaps:
        threshold = values[0] + 1.0
    else:
        _, idx = max(gaps)
        threshold = (values[idx] + values[idx + 1]) / 2.0

    low = [v for v in values if v <= threshold]
    high = [v for v in values if v > threshold]
    if not high:                       # single aero state on this track
        high = low

    zones: list[tuple[float, float]] = []
    for b in sorted(medians):
        if medians[b] <= threshold:
            lo, hi = b * bin_m, (b + 1) * bin_m
            if zones and lo - zones[-1][1] <= bin_m:
                zones[-1] = (zones[-1][0], hi)
            else:
                zones.append((lo, hi))

    return Aero(k_normal=statistics.median(high), k_low=statistics.median(low),
                zones=zones, bin_m=bin_m)


def fit_longitudinal(lap_or_laps, car) -> Longitudinal:
    """Fit drag, braking and traction, pooling every lap given.

    Passing one lap reproduces the single-lap fit exactly; passing a session
    averages down the run-to-run scatter, which is not small -- the braking
    coefficient moves by 10-25% between laps of the same session and the drag
    median by up to 10%.
    """
    laps = as_laps(lap_or_laps)
    drag = fit_drag_profile(laps, car)

    brake_rows = []
    for lap in laps:
        for i in range(len(lap)):
            if lap.brake[i] > 0.35 and lap.speed[i] > 15.0 and lap.accel[i] < 0:
                brake_rows.append((1.0, lap.speed[i] ** 2, -lap.accel[i]))
    c0, c1 = _lstsq2(brake_rows) if len(brake_rows) > 10 else (15.0, 0.003)

    # Traction is measured at corner exits, which are the only place the car
    # is both slow and at full throttle -- and they all carry lateral load. The
    # observed acceleration there is therefore already reduced by the friction
    # ellipse, so dividing it back out recovers the straight-line limit that
    # the simulation then re-applies the ellipse to. Fitting the reduced figure
    # and applying the ellipse on top counts the same loss twice, which made a
    # Barcelona replay 2.8% pessimistic against 1.1% optimistic before.
    lat_all = sorted(g for lap in laps for g in lap.lat_g)
    lat_max = lat_all[int(len(lat_all) * 0.99)] if lat_all else 0.0

    def unloaded(lap, index: int) -> float:
        if lat_max <= 0.0:
            return lap.accel[index]
        share = min(1.0, lap.lat_g[index] / lat_max)
        # Very high lateral load leaves so little longitudinal grip that
        # dividing by it amplifies noise, so those samples are left alone.
        left = max(0.3, (1.0 - share * share) ** 0.5)
        return lap.accel[index] / left

    grip = sorted(
        unloaded(lap, i) for lap in laps for i in range(len(lap))
        if lap.gas[i] >= 0.99 and lap.brake[i] < 0.01
        and lap.accel[i] > 0 and 10.0 < lap.speed[i] < 50.0
    )
    traction = grip[int(len(grip) * 0.97)] if len(grip) >= 20 else 20.0

    seen = sorted(s[1] for lap in laps for s in drag_samples(lap, car))
    return Longitudinal(drag=drag, brake_c0=c0, brake_c1=c1, traction_ms2=traction,
                        n_drag=len(seen), n_brake=len(brake_rows),
                        observed_speed_p95=seen[int(len(seen) * 0.95)] if seen else 0.0,
                        observed_speed_max=seen[-1] if seen else 0.0)


def _lstsq2(rows: list[tuple[float, float, float]]) -> tuple[float, float]:
    s11 = s12 = s22 = s1y = s2y = 0.0
    for x1, x2, y in rows:
        s11 += x1 * x1; s12 += x1 * x2; s22 += x2 * x2
        s1y += x1 * y; s2y += x2 * y
    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-9:
        return 0.0, (s2y / s22 if s22 else 0.0)
    return (s1y * s22 - s2y * s12) / det, (s11 * s2y - s12 * s1y) / det
