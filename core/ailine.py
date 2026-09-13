"""Reading a track's AI racing line, for a cold start with no laps recorded.

Before the first lap there is nothing position-dependent to model with: no
speed ceiling, no drag, no throttle trace. The car's own deployment map is
empty by default, so those first laps are driven on the automatic fallback and
nothing else -- and calibrating on them then running a map that deploys
properly takes the car past the speeds the fit ever saw (Monza: fitted to
p95 294 kph, final map runs at 333, worth about 0.6 s).

Assetto Corsa ships `content/tracks/<track>/ai/fast_lane.ai` for every track,
and it carries per-point speed, throttle and brake along the racing line --
structurally the same inputs a recorded lap provides. It is the AI's line
rather than the driver's, so it is a starting point and not a substitute: good
enough to author a first map worth driving, which is then replaced by one built
from real laps.

Format, version 7, little-endian, confirmed against four circuits:

    int   version, count, lapTime, sectorCount
    count x { float x, y, z; float length; int id }      the ideal line
    int   count (repeated)
    count x { float speed, gas, brake, obsoleteLatG,     72 bytes each
              radius, sideLeft, sideRight, camber,
              direction, normal[3], length,
              forwardVector[3], tag, grade }

Some files carry further blocks after that; they are not needed and ignored.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

HEADER = struct.Struct("<iiii")
POINT = struct.Struct("<ffffi")      # x, y, z, length, id
EXTRA_BYTES = 72
EXTRA_HEAD = struct.Struct("<ffff")  # speed, gas, brake, obsoleteLatG


#: An AI line is only as good as whoever authored it. Several stock tracks
#: ship lines topping out near 100 kph -- Monza, Barcelona and Red Bull Ring
#: among them -- which would silently produce a map for a car that does not
#: exist. A line has to look like this car was driven on it before it is used.
MIN_TOP_SPEED_KPH = 250.0
MIN_FULL_THROTTLE = 0.15


@dataclass
class AiLine:
    """The racing line as distance, speed, throttle and brake."""

    dist: list[float]
    speed: list[float]          # m/s
    gas: list[float]
    brake: list[float]
    length_m: float

    def __len__(self) -> int:
        return len(self.dist)

    @property
    def top_speed_kph(self) -> float:
        return max(self.speed) * 3.6

    @property
    def full_throttle_fraction(self) -> float:
        flat = sum(1 for g in self.gas if g >= 0.99)
        return flat / max(1, len(self.gas))

    @property
    def usable(self) -> bool:
        """Whether this line plausibly belongs to a car like this one."""
        return (self.top_speed_kph >= MIN_TOP_SPEED_KPH
                and self.full_throttle_fraction >= MIN_FULL_THROTTLE
                and self.length_m > 500.0)

    def rejection(self) -> str | None:
        if self.length_m <= 500.0:
            return "the line is %.0f m long" % self.length_m
        if self.top_speed_kph < MIN_TOP_SPEED_KPH:
            return ("it tops out at %.0f kph, so it was not authored for a car "
                    "like this one" % self.top_speed_kph)
        if self.full_throttle_fraction < MIN_FULL_THROTTLE:
            return ("only %.0f%% of it is full throttle"
                    % (100 * self.full_throttle_fraction))
        return None


def find(track_dir) -> Path | None:
    """The fast_lane.ai for a track, including inside a layout folder."""
    track_dir = Path(track_dir)
    direct = track_dir / "ai" / "fast_lane.ai"
    if direct.exists():
        return direct
    found = sorted(track_dir.rglob("ai/fast_lane.ai"))
    return found[0] if found else None


def load(path) -> AiLine:
    data = Path(path).read_bytes()
    version, count, _lap_time, _sectors = HEADER.unpack_from(data, 0)
    if version != 7:
        raise ValueError(f"fast_lane.ai version {version} is not supported")
    if count <= 1:
        raise ValueError("fast_lane.ai has no usable points")

    dist = []
    offset = HEADER.size
    for _ in range(count):
        _x, _y, _z, length, _id = POINT.unpack_from(data, offset)
        dist.append(length)
        offset += POINT.size

    # The count is repeated before the detail block; treat a mismatch as a
    # format we do not understand rather than reading garbage as speeds.
    repeat = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    if repeat != count:
        raise ValueError("fast_lane.ai detail block does not match the line")
    needed = offset + count * EXTRA_BYTES
    if len(data) < needed:
        raise ValueError("fast_lane.ai is truncated")

    speed, gas, brake = [], [], []
    for i in range(count):
        s, g, b, _lat = EXTRA_HEAD.unpack_from(data, offset + i * EXTRA_BYTES)
        speed.append(max(0.0, s))
        gas.append(min(1.0, max(0.0, g)))
        brake.append(min(1.0, max(0.0, b)))

    return AiLine(dist=dist, speed=speed, gas=gas, brake=brake,
                  length_m=max(dist))


def as_lap(line: AiLine, car, ds: float = 2.5):
    """A Lap-shaped object built from an AI line, for a cold start.

    Enough of `lap.Lap` to drive `Profile` and `fit_longitudinal`: distance,
    speed, throttle, brake, and the engine state inferred from speed via the
    car's own gearing. There is no ERS trace, because an AI line does not carry
    one, so the force balance attributes nothing to deployment and the fitted
    drag comes out low -- a first map built from this is optimistic by
    construction. It exists to be driven and replaced, not to be trusted.
    """
    import lap as laplib

    n = max(2, int(line.length_m / ds))
    step = line.length_m / n
    src = 0
    dist, speed, gas, brake = [], [], [], []
    for i in range(n):
        d = i * step
        while src < len(line) - 2 and line.dist[src + 1] < d:
            src += 1
        dist.append(d)
        speed.append(max(1.0, line.speed[src]))
        gas.append(line.gas[src])
        brake.append(line.brake[src])

    # Time from speed over the fixed distance step, and the engine state that
    # goes with it. Gear is whichever gives the most force, which is what the
    # simulation assumes anyway.
    t = [0.0]
    for i in range(1, n):
        v = max(1.0, 0.5 * (speed[i] + speed[i - 1]))
        t.append(t[-1] + step / v)
    rpm, gear = [], []
    for v in speed:
        best, best_gear = 0.0, 1
        for index, ratio in enumerate(car.gears, start=1):
            total = ratio * car.final_drive
            r = v / car.wheel_radius_m * total * 60.0 / (2.0 * 3.141592653589793)
            if r > car.rpm_limiter:
                continue
            force = car.engine_torque(r, car.boost_wastegate) * total                 * car.mech_efficiency / car.wheel_radius_m
            if force > best:
                best, best_gear = force, index
        ratio = car.gears[best_gear - 1] * car.final_drive
        rpm.append(v / car.wheel_radius_m * ratio * 60.0 / (2.0 * 3.141592653589793))
        gear.append(best_gear)

    zeros = [0.0] * n
    out = laplib.Lap(
        t=t, dist=dist, speed=speed, gas=gas, brake=brake, rpm=rpm, gear=gear,
        boost=[car.boost_wastegate] * n,
        store_kj=[laplib.STORE_CAPACITY_KJ * 0.5] * n,
        deployed_kj=list(zeros), lat_g=list(zeros), long_g=list(zeros),
        strat=[1] * n, length_m=line.length_m)
    laplib.differentiate(out)
    # Lateral load is not in the file; derive it from how hard the line turns,
    # so the grip budget still knows where the corners are.
    for i in range(1, n - 1):
        dv = (speed[i + 1] - speed[i - 1])
        radius_accel = abs(dv) / max(t[i + 1] - t[i - 1], 1e-3)
        out.lat_g[i] = radius_accel
    return out
