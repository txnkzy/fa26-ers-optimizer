"""Loading and differentiating a recorded FA26 lap.

The logger records the energy store and a cumulative per-lap deployment
counter. Deployment power is the derivative of that counter, which makes it an
exact measurement rather than an inference -- but only when differentiated over
a window. Per-sample differencing is dominated by frame-timing jitter and
produces physically impossible values (484 kW against a 350 kW limit).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

STORE_CAPACITY_KJ = 4000.0
DERIVATIVE_WINDOW_S = 0.25


@dataclass
class Lap:
    t: list[float]
    dist: list[float]
    speed: list[float]            # m/s
    gas: list[float]
    brake: list[float]
    rpm: list[float]
    gear: list[int]
    boost: list[float]
    store_kj: list[float]         # energy remaining in the store
    deployed_kj: list[float]      # cumulative deployment this lap
    lat_g: list[float]
    long_g: list[float]
    strat: list[int]
    length_m: float = 0.0

    # Derived, filled by `differentiate`
    deploy_kw: list[float] = field(default_factory=list)
    store_kw: list[float] = field(default_factory=list)   # + = leaving the store
    accel: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.t)

    @property
    def lap_time_s(self) -> float:
        return self.t[-1]

    def totals(self) -> tuple[float, float]:
        """(deployed, harvested) in MJ, robust to where the counter resets."""
        deployed = sum(max(0.0, self.deployed_kj[i] - self.deployed_kj[i - 1])
                       for i in range(1, len(self)))
        harvested = sum(max(0.0, self.store_kj[i] - self.store_kj[i - 1])
                        for i in range(1, len(self)))
        return deployed / 1000.0, harvested / 1000.0


def _smooth(values: list[float], window: int) -> list[float]:
    n = len(values)
    half = window // 2
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out.append(sum(values[lo:hi]) / (hi - lo))
    return out


def load(path: str | Path) -> Lap:
    rows = list(csv.DictReader(Path(path).open(encoding="utf-8")))
    if not rows:
        raise ValueError("empty telemetry file")
    col = lambda k, d=0.0: [float(r.get(k, d) or d) for r in rows]

    lap = Lap(
        t=col("t"),
        dist=col("dist_m"),
        speed=[v / 3.6 for v in col("speed_kmh")],
        gas=col("gas"),
        brake=col("brake"),
        rpm=col("rpm"),
        gear=[int(float(r.get("gear", 0) or 0)) for r in rows],
        boost=col("turbo_boost"),
        store_kj=[c * STORE_CAPACITY_KJ for c in col("kers_charge")],
        deployed_kj=col("kers_current_kj"),
        lat_g=[abs(v) for v in col("lat_acc")],
        long_g=col("long_acc"),
        # The car's own dash renders this as "STRAT " .. mgukDelivery, and
        # setup_ers_strat_maps.lut is zero-based, so value 0 is STRAT 1. It is
        # the live strategy selection, changeable on the wheel at any moment --
        # which is how a strategy is actually chosen, not the PU mode switch.
        strat=[int(float(r.get("mguk_delivery", 0) or 0)) + 1 for r in rows],
    )
    lap.length_m = max(lap.dist)
    differentiate(lap)
    return lap


def differentiate(lap: Lap, window_s: float = DERIVATIVE_WINDOW_S) -> None:
    n = len(lap)
    lap.deploy_kw = [0.0] * n
    lap.store_kw = [0.0] * n

    j = 0
    for i in range(n):
        while j < n - 1 and lap.t[j] - lap.t[i] < window_s:
            j += 1
        dt = lap.t[j] - lap.t[i]
        if dt < window_s * 0.8:
            # Near the end of the lap, reuse the last full window.
            lap.deploy_kw[i] = lap.deploy_kw[i - 1] if i else 0.0
            lap.store_kw[i] = lap.store_kw[i - 1] if i else 0.0
            continue
        # The deployment counter resets at the line; a drop is that reset, not
        # negative deployment.
        if lap.deployed_kj[j] >= lap.deployed_kj[i]:
            lap.deploy_kw[i] = (lap.deployed_kj[j] - lap.deployed_kj[i]) / dt
        lap.store_kw[i] = -(lap.store_kj[j] - lap.store_kj[i]) / dt

    smoothed = _smooth(lap.speed, 15)
    lap.accel = [0.0] * n
    for i in range(1, n - 1):
        dt = lap.t[i + 1] - lap.t[i - 1]
        if dt > 0:
            lap.accel[i] = (smoothed[i + 1] - smoothed[i - 1]) / dt
