"""Track-position profiles taken from a recorded lap.

The slot size is 2.5 m, not 5. Swept against the nine laps whose map is
provably the one driven, the model does not converge to zero error as the step
shrinks -- it converges to about -1.3%, the standing optimism of driving the
recorded line perfectly and braking at the calibrated limit. At 5 m,
discretisation error happened to cancel most of that (-0.79%), and at 7.5-10 m
it over-cancelled into a false +0.2%. That cancellation is a property of one
track and one map shape, so it cannot be relied on anywhere else; 2.5 m is
converged to within 0.05% of the 1 m answer and costs almost nothing.

The ceiling margin is 1.00 for the same reason. This module says the driver's
measured speed "stands as a ceiling" where they were not at full throttle,
because deployment cannot change a grip-limited corner -- and then handed back
2% of it, which is most of a second a lap the car was never going to find.
Removing it is what actually closes the residual: at 2.5 m the nine reference
laps go from -1.28% bias to -0.04%, and the worst lap from 1.83% to 1.02%.
Where the driver *was* flat the cap is NO_CAP, so deployment is still free to
make the car faster there, which is the only place it legitimately can.

Corner speeds, throttle application and the speed ceiling all come from the
driver's own lap rather than from track geometry. Where the driver was not at
full throttle the limit is grip and line, which deployment cannot change, so the
measured speed stands as a ceiling. Where they were at full throttle with no
brake the car was power-limited, and more deployment legitimately goes faster.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cars"))

NO_CAP = 1e6

#: Shape of the combined-grip envelope, (1 - share**n)**(1/n). n = 2 is a true
#: friction circle; real tyres hold more longitudinal grip under lateral load
#: than that, so the envelope is squarer. Fitted against replays of VRC's own
#: authored maps on the laps driven with them -- see docs/FA26_FINDINGS.md.
GRIP_EXPONENT = 4.0


def _smooth(values: list[float], window: int) -> list[float]:
    n = len(values)
    half = window // 2
    return [sum(values[max(0, i - half):min(n, i + half + 1)])
            / (min(n, i + half + 1) - max(0, i - half)) for i in range(n)]


class Profile:
    """Speed ceiling, throttle state and commanded-power context by position."""

    def __init__(self, lap, ds: float = 2.5, margin: float = 1.00,
                 lat_fraction: float = 0.7) -> None:
        laps = list(lap) if isinstance(lap, (list, tuple)) else [lap]
        if len(laps) > 1:
            self._build_many(laps, ds, margin, lat_fraction)
            return
        lap = laps[0]
        self.ds = ds
        self.length_m = lap.length_m
        n = int(lap.length_m / ds) + 1
        self.cap = [NO_CAP] * n
        self.gas = [0.0] * n
        #: Mean throttle in each slot. `gas` is the slot maximum, which is right
        #: for spotting a full-throttle stretch but far too generous for asking
        #: "is the driver on the throttle here" -- a braking zone with one
        #: sample of throttle in it reads as on-throttle all the way through.
        self.gas_mean = [0.0] * n
        self.brake_mean = [0.0] * n
        self.speed = [0.0] * n
        #: Lateral load carried at each point, and the most the car showed all
        #: lap. Together they say how much of the tyre is already spoken for.
        self.lat = [0.0] * n
        self.lat_max = 0.0

        speed = _smooth(lap.speed, 15)

        # A corner taken flat out is full throttle yet still grip-limited, so
        # lateral load has to be part of the test or Eau-Rouge-like corners look
        # like straights and the model invents speed through them.
        lat_limit = None
        if any(lap.lat_g):
            ordered = sorted(lap.lat_g)
            lat_limit = ordered[int(len(ordered) * 0.98)] * lat_fraction

        counts = [0] * n
        for i in range(len(lap)):
            slot = int(lap.dist[i] / ds)
            if not 0 <= slot < n:
                continue
            cornering = lat_limit is not None and lap.lat_g[i] >= lat_limit
            power_limited = lap.gas[i] >= 0.99 and lap.brake[i] < 0.01 and not cornering
            value = NO_CAP if power_limited else speed[i] * margin
            self.cap[slot] = min(self.cap[slot], value)
            self.gas[slot] = max(self.gas[slot], lap.gas[i])
            self.gas_mean[slot] += lap.gas[i]
            self.brake_mean[slot] += lap.brake[i]
            self.lat[slot] = max(self.lat[slot], lap.lat_g[i])
            self.speed[slot] += speed[i]
            counts[slot] += 1
        for i in range(n):
            if counts[i]:
                self.speed[i] /= counts[i]
                self.gas_mean[i] /= counts[i]
                self.brake_mean[i] /= counts[i]
            elif i:
                self.speed[i] = self.speed[i - 1]
                self.lat[i] = self.lat[i - 1]
                self.gas_mean[i] = self.gas_mean[i - 1]
                self.brake_mean[i] = self.brake_mean[i - 1]
        if any(lap.lat_g):
            ordered = sorted(lap.lat_g)
            self.lat_max = max(ordered[int(len(ordered) * 0.99)], 1e-6)

    def _build_many(self, laps, ds, margin, lat_fraction) -> None:
        """One profile from a session, with the rules chosen per quantity.

        These do not all want the same aggregate, which is the whole point:

        * The speed ceiling is the driver's line, and wants the **median**.
          Taking the minimum -- the obvious generalisation of what a single
          lap already does across its own samples -- is a disaster, because
          across laps it picks the slowest lap at every point and compounds
          into a lap nobody drove: 3.15% error against 0.50% for the median,
          measured by leave-one-out on the Barcelona session.
        * Throttle and brake state average, so a slot reads as the driver
          typically had it rather than as one lap happened to.
        * `consensus` is new, and is what a split boundary should be placed
          against: the fraction of laps where the driver was above the
          arbitration knee. Boundaries land almost exclusively in slots where
          the laps disagree -- 19 of 21 at Barcelona, 22 of 23 at Madrid --
          and the cost is sharply asymmetric, since a boundary 20 m early is
          free while one 20 m late has the car refuse the whole power step.
        """
        import fa26 as _fa26
        parts = [Profile(l, ds=ds, margin=margin, lat_fraction=lat_fraction)
                 for l in laps]
        n = min(len(p.cap) for p in parts)
        self.ds = ds
        self.length_m = statistics.median([p.length_m for p in parts])
        self.cap = [NO_CAP] * n
        self.gas = [0.0] * n
        self.gas_mean = [0.0] * n
        self.brake_mean = [0.0] * n
        self.speed = [0.0] * n
        self.lat = [0.0] * n
        #: Fraction of laps with the throttle above the arbitration knee here.
        self.consensus = [0.0] * n
        self.lap_count = len(parts)
        knee = getattr(_fa26, "DEMAND_RESET_GAS", 0.6)
        for i in range(n):
            capped = [p.cap[i] for p in parts if p.cap[i] < NO_CAP]
            # Power-limited on most laps means power-limited: deployment can
            # legitimately go faster there, and a cap from the minority of laps
            # where the driver lifted would forbid it.
            self.cap[i] = (NO_CAP if len(capped) * 2 <= len(parts)
                           else statistics.median(capped))
            self.gas[i] = statistics.median([p.gas[i] for p in parts])
            self.gas_mean[i] = statistics.fmean([p.gas_mean[i] for p in parts])
            self.brake_mean[i] = statistics.fmean([p.brake_mean[i] for p in parts])
            self.speed[i] = statistics.fmean([p.speed[i] for p in parts])
            self.lat[i] = statistics.fmean([p.lat[i] for p in parts])
            self.consensus[i] = sum(
                1 for p in parts if p.gas_mean[i] >= knee) / len(parts)
        self.lat_max = statistics.fmean([p.lat_max for p in parts])

    def _slot(self, distance_m: float) -> int:
        return min(int((distance_m % self.length_m) / self.ds), len(self.cap) - 1)

    def cap_at(self, distance_m: float) -> float:
        return self.cap[self._slot(distance_m)]

    def full_throttle_at(self, distance_m: float) -> bool:
        """Whether the driver is genuinely flat here.

        The throttle test alone used the slot maximum, so a braking zone
        containing one sample of throttle counted as flat for its whole length,
        and super-clipping -- which the car only allows at full throttle -- was
        being planned into braking zones. Monza's qualifying map was harvesting
        through the Turn 1 braking zone on exactly that error.

        Requiring a mean throttle instead went too far the other way and cost a
        Monza race replay two thirds of its harvest, because a 5 m slot's mean
        is noisy. Testing for the brake is both the physically right question
        and far more stable.
        """
        slot = self._slot(distance_m)
        return self.gas[slot] >= 0.99 and self.brake_mean[slot] < 0.05

    def measured_speed_at(self, distance_m: float) -> float:
        return self.speed[self._slot(distance_m)]

    def on_throttle_at(self, distance_m: float) -> bool:
        """Whether the driver is actually on the throttle here."""
        return self.gas_mean[self._slot(distance_m)] >= 0.2

    def grip_left(self, distance_m: float) -> float:
        """Fraction of longitudinal grip still available under lateral load.

        A tyre has one friction budget. Spending it sideways leaves less for
        driving, which the simulation used to ignore entirely: it allowed the
        full straight-line traction limit at any lateral load, so the search
        happily commanded 350 kW through the middle of Curva Grande where the
        car has no grip left to take it. The usual friction-ellipse form is
        close enough and needs nothing that is not already measured.
        """
        if self.lat_max <= 0.0:
            return 1.0
        share = min(1.0, self.lat[self._slot(distance_m)] / self.lat_max)
        n = GRIP_EXPONENT
        return max(0.0, 1.0 - share ** n) ** (1.0 / n)
