"""The compiled lap integration, and the bridge to it. Experimental.

`native/fa26sim` is a Rust port of the loop in `sim26.simulate_py`. This
module loads the library it builds, hands it one lap at a time, and turns
what comes back into the same `Result` the Python gives.

The Python is still the reference. Nothing here is used unless the library
is present and the inputs are exactly the kinds it was ported from, and
`FA26_SIM=python` in the environment turns it off altogether. Whether the
two agree is `native/parity.py`'s job.

Build it with `python native/build.py`.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import carphysics
import fa26

ABI_VERSION = 1
LIBRARY = "fa26sim.dll"

_D = ctypes.c_double
_P = ctypes.c_void_p

_lib = None
_pow = None


def _load():
    """The library, or None. Never raises: no library just means Python."""
    if os.environ.get("FA26_SIM", "").strip().lower() == "python":
        return None
    if sys.platform != "win32":
        return None
    folders = [Path(__file__).resolve().parent]
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        folders.append(Path(frozen))
    for folder in folders:
        path = folder / LIBRARY
        if not path.is_file():
            continue
        try:
            lib = ctypes.CDLL(str(path))
            lib.fa26_abi_version.restype = ctypes.c_int
            if lib.fa26_abi_version() != ABI_VERSION:
                continue
            lib.fa26_simulate.restype = ctypes.c_int
            lib.fa26_simulate.argtypes = [_P] * 21
            return lib
        except (OSError, AttributeError):
            continue
    return None


def _c_pow():
    """Address of the C runtime's `pow`, the one behind Python's `x ** 0.5`.

    It is not always the correctly rounded square root -- about one value in
    two thousand differs in the last bit -- so the library is handed this
    function rather than using its own `sqrt`, and the two integrations stay
    bit for bit the same.
    """
    ucrt = ctypes.CDLL("ucrtbase")
    return ctypes.cast(ucrt.pow, _P)


try:
    _lib = _load()
    if _lib is not None:
        _pow = _c_pow()
except Exception:                                   # noqa: BLE001
    _lib = None

#: Whether the compiled integration is there to be used at all.
available = _lib is not None and _pow is not None and bool(_pow.value)


def eligible(car, powertrain) -> bool:
    """Whether this car and powertrain are the ones the port was made from.

    A subclass, or an object with a method swapped out, may behave differently
    from the code that was ported; those go to the Python.
    """
    return (available
            and type(powertrain) is fa26.Powertrain
            and type(car) is carphysics.CarPhysics
            and getattr(car, "_force_wastegate", None) is not None)


class _Geometry:
    """One lap geometry as C arrays, plus the buffers results come back in."""

    def __init__(self, profile, data):
        (n, step, ceiling, gas, deploy_frac, harvest_frac, full_throttle,
         on_throttle, grip, drag_k, in_reset, cut_kph, lifting) = data
        self.data = data                    # held so its identity stays ours
        self.n = n
        self.step = step
        arr = _D * n
        self.ceiling = arr(*ceiling)
        self.gas = arr(*gas)
        self.deploy_frac = arr(*deploy_frac)
        self.harvest_frac = arr(*harvest_frac)
        self.grip = arr(*grip)
        self.drag_k = arr(*drag_k)
        self.cut_kph = arr(*cut_kph)
        self.measured = arr(*[profile.measured_speed_at(i * step)
                              for i in range(n)])
        self.start_speed = profile.measured_speed_at(0.0)
        self.lifting = (ctypes.c_int32 * n)(*lifting)
        self.base_flags = [(1 if full_throttle[i] else 0)
                           | (2 if on_throttle[i] else 0)
                           | (4 if in_reset[i] else 0) for i in range(n)]
        self._flags = {}
        self.dists = [i * step for i in range(n)]
        self.cap = n + 8
        self.speeds = (_D * self.cap)()
        self.lift_corner = (ctypes.c_int32 * n)()
        self.lift_kph = arr()
        self.out = (_D * 14)()

    def flags(self, powertrain):
        """Per-step flags, with the alternative power curve's zones marked."""
        zones = (tuple(tuple(z) for z in powertrain.alt_zones)
                 if powertrain.alt_taper else ())
        hit = self._flags.get(zones)
        if hit is None:
            step = self.step
            values = list(self.base_flags)
            if zones:
                for i in range(self.n):
                    d = i * step
                    if any(a <= d < b for a, b in zones):
                        values[i] |= 8
            hit = self._flags[zones] = (ctypes.c_uint8 * self.n)(*values)
        return hit


#: Geometries by the identity of the Python tuple they were made from. Kept
#: here and not on the profile: profiles and cars are pickled across to the
#: search's worker processes, and C arrays cannot be.
_geometries = {}
_force_tables = {}
_KEEP = 32


def _geometry(profile, data) -> _Geometry:
    hit = _geometries.get(id(data))
    if hit is None or hit.data is not data:
        if len(_geometries) >= _KEEP:
            _geometries.clear()
        hit = _geometries[id(data)] = _Geometry(profile, data)
    return hit


_curves = {}


def _curve(taper):
    """A power curve as a flat C array of (kph, kW) and its point count."""
    points = fa26._curve_points(taper)
    hit = _curves.get(points)
    if hit is None:
        flat = [x for point in points for x in point]
        hit = _curves[points] = ((_D * len(flat))(*flat), len(points))
    return hit


def _force_table(car):
    """The engine force table `best_tractive_force` would read for this car."""
    if car.boost_wastegate >= car.boost_eboost - 1e-6:
        table = car._force_eboost
    else:
        table = car._force_wastegate
    hit = _force_tables.get(id(table))
    if hit is None or hit[0] is not table or len(hit[1]) != len(table):
        if len(_force_tables) >= _KEEP:
            _force_tables.clear()
        hit = _force_tables[id(table)] = (table, (_D * len(table))(*table))
    return hit[1]


def simulate(sim, splits, car, longitudinal, profile, powertrain,
             start_store_kj, harvest_cap_kj, store_capacity_kj, ds, settle,
             reset_zones, speed_zones, entry, window):
    """`sim26.simulate_py`, compiled. Returns None if it cannot take the lap.

    `sim` is the `sim26` module, passed in because it imports this one.
    """
    data = sim._geometry(profile, longitudinal, ds,
                         tuple(map(tuple, reset_zones)),
                         tuple(map(tuple, speed_zones)))
    g = _geometry(profile, data)
    n, step = g.n, g.step

    lo, hi = 0, n
    if window is not None:
        lo = int(window[0] / step)
        hi = max(lo + 1, int(window[1] / step))
    if lo < 0 or hi - lo > g.cap:
        return None

    taper, n_taper = _curve(powertrain.taper)
    if powertrain.alt_taper:
        alt, n_alt = _curve(powertrain.alt_taper)
    else:
        alt, n_alt = taper, 0
    table = _force_table(car)

    flat = []
    for s in splits:
        flat += (s.start_m, s.deploy_end_m, s.clip_start_m, s.end_m,
                 s.deploy_kw, s.clip_kw)
    c_splits = (_D * max(len(flat), 1))(*flat)

    mass = car.total_mass_kg
    if entry is not None:
        start_store_kj = entry.store_kj
        e = (entry.speed_ms, entry.demand_kw, entry.prev_level_kw,
             entry.throttle_time_s)
    else:
        e = (0.0, 0.0, 0.0, 0.0)
    params = (_D * 24)(
        step, mass, longitudinal.traction_ms2 * mass, powertrain.floor_kw,
        powertrain.floor_hold_s, powertrain.ramp_down_kw_s, start_store_kj,
        harvest_cap_kj, store_capacity_kj, fa26.MAX_HARVEST_KW,
        fa26.MOTOR_EFFICIENCY, fa26.DEMAND_RESET_GAS, fa26.MAX_DEPLOY_KW,
        fa26.HARVEST_CUTOFF_KPH, fa26.HARVEST_RAMP_KPH,
        fa26.HARVEST_OFF_THROTTLE_KW, sim.INSTANT_CUT_KPH, car._table_step,
        g.start_speed, e[0], e[1], e[2], e[3], profile.length_m)
    ints = (ctypes.c_int64 * 10)(
        n, lo, hi, 1 if settle else 0, 1 if entry is not None else 0,
        max(0, n - int(sim.PRIME_M / step)), len(flat) // 6, n_taper, n_alt,
        len(table))

    out = g.out
    code = _lib.fa26_simulate(
        params, ints, g.ceiling, g.gas, g.deploy_frac, g.harvest_frac, g.grip,
        g.drag_k, g.cut_kph, g.measured, g.flags(powertrain), g.lifting,
        c_splits, taper, alt, table, _pow, out, g.speeds, g.lift_corner,
        g.lift_kph)
    if code != 0:
        return None

    count = int(out[13])
    if lo == 0 and count == n:
        dist = list(g.dists)
    else:
        dist = [(i % n) * step for i in range(lo, lo + count)]
    lift_corner, lift_kph = g.lift_corner, g.lift_kph
    return sim.Result(
        exit=sim.Carry(speed_ms=out[8], store_kj=out[3], demand_kw=out[9],
                       prev_level_kw=out[10], throttle_time_s=out[11]),
        lap_time_s=out[0], deployed_mj=out[1] / 1000.0,
        harvested_mj=out[2] / 1000.0, store_end_kj=out[3],
        store_min_kj=out[4], store_start_kj=out[5],
        harvest_capped=out[6] != 0.0, refused_mj=out[7] / 1000.0,
        speed=g.speeds[:count], dist=dist,
        lifts={lift_corner[j] * step: lift_kph[j]
               for j in range(int(out[12]))})
