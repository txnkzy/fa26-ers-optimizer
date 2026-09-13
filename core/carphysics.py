"""Setup-independent vehicle constants, read from the unpacked data.acd."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def parse_ini(text: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    section = ""
    for line in text.replace("\r", "").split("\n"):
        line = line.split(";", 1)[0].split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out.setdefault(section, {})
        elif "=" in line:
            key, value = line.split("=", 1)
            out.setdefault(section, {})[key.strip()] = value.strip()
    return out


def parse_lut(text: str) -> list[tuple[float, float]]:
    points = []
    for line in text.replace("\r", "").split("\n"):
        line = line.split(";", 1)[0].strip()
        if "|" in line:
            a, b = line.split("|", 1)
            points.append((float(a), float(b)))
    return sorted(points)


def lut_lookup(points: list[tuple[float, float]], x: float) -> float:
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for i in range(1, len(points)):
        x1, y1 = points[i]
        if x <= x1:
            x0, y0 = points[i - 1]
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


@dataclass
class CarPhysics:
    mass_kg: float
    gears: list[float]
    final_drive: float
    mech_efficiency: float
    rpm_limiter: float
    wheel_radius_m: float
    torque_curve: list[tuple[float, float]]     # rpm -> Nm, before boost
    boost_wastegate: float
    boost_eboost: float

    # ERS, from script__params.ini and script_ERS_F1.lua
    es_capacity_mj: float = 4.0
    es_efficiency: float = 0.94
    mguk_max_kw: float = 129.0
    mguk_efficiency: float = 0.93
    mguh_assist_kw: float = 65.0
    deploy_cap_mj: float = 4.0
    recover_cap_mj: float = 2.0
    deploy_rpm_limit: float = 12500.0
    deploy_min_gas: float = 0.9

    fuel_kg_per_liter: float = 0.76
    fuel_liters: float = 0.0

    @property
    def total_mass_kg(self) -> float:
        return self.mass_kg + self.fuel_liters * self.fuel_kg_per_liter

    def engine_torque(self, rpm: float, boost: float) -> float:
        """Crank torque in Nm at a given rpm and absolute boost."""
        if rpm > self.rpm_limiter:
            return 0.0
        return lut_lookup(self.torque_curve, rpm) * (1.0 + boost)

    def _build_tables(self, v_max: float = 130.0, step: float = 0.5) -> None:
        """Precompute force and rpm against speed.

        The solver evaluates these millions of times; searching eight gear
        ratios and interpolating the torque LUT on every step dominated the
        runtime.
        """
        self._table_step = step
        n = int(v_max / step) + 1
        self._force_wastegate = [0.0] * n
        self._force_eboost = [0.0] * n
        self._rpm_table = [0.0] * n
        for i in range(n):
            v = max(i * step, 0.5)
            self._force_wastegate[i] = self._best_force_uncached(v, self.boost_wastegate)
            self._force_eboost[i] = self._best_force_uncached(v, self.boost_eboost)
            self._rpm_table[i] = self._rpm_uncached(v)

    def _lookup(self, table: list[float], speed_ms: float) -> float:
        x = speed_ms / self._table_step
        i = int(x)
        if i < 0:
            return table[0]
        if i >= len(table) - 1:
            return table[-1]
        frac = x - i
        return table[i] + (table[i + 1] - table[i]) * frac

    def best_tractive_force(self, speed_ms: float, boost: float) -> float:
        table = getattr(self, "_force_wastegate", None)
        if table is not None:
            if boost >= self.boost_eboost - 1e-6:
                return self._lookup(self._force_eboost, speed_ms)
            if abs(boost - self.boost_wastegate) < 1e-6:
                return self._lookup(table, speed_ms)
        return self._best_force_uncached(speed_ms, boost)

    def _best_force_uncached(self, speed_ms: float, boost: float) -> float:
        """Engine force at the contact patch, using the best available gear."""
        if speed_ms < 0.5:
            speed_ms = 0.5
        best = 0.0
        for gear in self.gears:
            ratio = gear * self.final_drive
            rpm = speed_ms / self.wheel_radius_m * ratio * 60.0 / (2.0 * 3.141592653589793)
            if rpm > self.rpm_limiter:
                continue
            torque = self.engine_torque(rpm, boost)
            force = torque * ratio * self.mech_efficiency / self.wheel_radius_m
            best = max(best, force)
        return best

    def rpm_at(self, speed_ms: float) -> float:
        table = getattr(self, "_rpm_table", None)
        if table is not None:
            return self._lookup(table, speed_ms)
        return self._rpm_uncached(speed_ms)

    def _rpm_uncached(self, speed_ms: float) -> float:
        """rpm in the gear the engine would actually be using."""
        best_force, best_rpm = 0.0, 0.0
        for gear in self.gears:
            ratio = gear * self.final_drive
            rpm = max(speed_ms, 0.5) / self.wheel_radius_m * ratio * 60.0 / (2.0 * 3.141592653589793)
            if rpm > self.rpm_limiter:
                continue
            force = self.engine_torque(rpm, self.boost_wastegate) * ratio / self.wheel_radius_m
            if force > best_force:
                best_force, best_rpm = force, rpm
        return best_rpm


def load(data_dir: str | Path) -> CarPhysics:
    data_dir = Path(data_dir)
    read = lambda name: (data_dir / name).read_text(encoding="utf-8", errors="replace")

    car = parse_ini(read("car.ini"))
    engine = parse_ini(read("engine.ini"))
    drivetrain = parse_ini(read("drivetrain.ini"))
    tyres = parse_ini(read("tyres.ini"))
    # The FA26 keeps these inside its obfuscated script, so treat the file as
    # optional and fall back to the dataclass defaults.
    params_path = data_dir / "script__params.ini"
    params = parse_ini(params_path.read_text(encoding="utf-8", errors="replace"))         if params_path.exists() else {}

    count = int(drivetrain["GEARS"]["COUNT"])
    gears = [float(drivetrain["GEARS"][f"GEAR_{i}"]) for i in range(1, count + 1)]

    ess = params.get("ENERGY_STORE_SYSTEM", {})
    rear = params.get("MOTOR_REAR", {})
    turbo = engine.get("TURBO_0", {})

    result = CarPhysics(
        mass_kg=float(car["BASIC"]["TOTALMASS"]),
        gears=gears,
        final_drive=float(drivetrain["GEARS"]["FINAL"]),
        mech_efficiency=float(engine["ENGINE_DATA"].get("MECHANICAL_EFFICIENCY", 0.92)),
        rpm_limiter=float(engine["ENGINE_DATA"]["LIMITER"]),
        wheel_radius_m=float(tyres["REAR"]["RADIUS"]),
        torque_curve=parse_lut(read(engine["HEADER"]["POWER_CURVE"])),
        boost_wastegate=float(turbo.get("WASTEGATE", 2.25)),
        boost_eboost=float(turbo.get("MAX_BOOST", 2.6)),
        es_capacity_mj=float(ess.get("CAPACITY_MJ", 4)),
        es_efficiency=float(ess.get("EFFICIENCY", 0.94)),
        mguk_max_kw=float(rear.get("MAX_POWER_KW", 129)),
        mguk_efficiency=float(rear.get("EFFICIENCY", 0.93)),
        fuel_kg_per_liter=float(car.get("FUEL_EXT", {}).get("KG_PER_LITER", 0.76)),
    )
    result._build_tables()
    return result
