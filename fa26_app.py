"""FA26 ERS deployment optimiser -- local web front end.

Serves a small single-page app on 127.0.0.1 and opens it in the default
browser. Everything ships with Python: the server is `http.server`, the page
is three static files, and there is nothing to install.

Three strategies come out of one recorded session and are written into a
setup, each into its own deployment map:

    STRAT 1  Qualifying  fastest lap, spends the store
    STRAT 2  Race        fastest lap that ends with the energy it started
    STRAT 3  Recharge    banks energy, accepting some lap time

The map in use is chosen by the "STRAT Map" setup item, not by the wheel's PU
mode switch -- the per-mode overrides do not fire, so this tool writes them off
and drives the base item instead.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

#: Frozen, everything that ships is unpacked into a temporary folder that
#: PyInstaller names in `sys._MEIPASS`; from source it sits beside this file.
ROOT = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)
if not getattr(sys, "frozen", False):
    for sub in ("core", "cars"):
        sys.path.insert(0, str(ROOT / sub))

import calibrate
import carphysics
import fa26
import install
import lap as laplib
import optimise26
import profile as prof
import recharge
import session as lapsession
import setupfile

UI_DIR = ROOT / "ui"
CAR_ID = fa26.CAR_ID

#: Shown in the window and stamped into anything a user sends back. The git
#: tag is the source of truth: `build_exe.py` refuses to build when this and
#: the tag disagree, because a build that misreports its own version turns
#: every bug report into a guess about which one it came from.
VERSION = "1.0.4"


def car_data() -> Path:
    """The car's own files, unpacked from the user's install on first use.

    This used to be a folder of VRC's physics committed into the project --
    380 files of their paid work, which could not be passed to anyone else.
    `install.ensure_unpacked` reads the copy the user already owns and caches
    it outside the project, so nothing of VRC's ships here at all.
    """
    return install.ensure_unpacked(CAR_ID)


def setup_ini() -> str:
    return (car_data() / "setup.ini").read_text(encoding="utf-8",
                                                errors="replace")

#: Per-track recharge limits the user set by hand, for circuits the FIA has
#: not published a notice for. Kept apart from the shipped table on purpose --
#: that table is evidence and this is an assumption -- and outside the program
#: entirely, because a frozen build unpacks itself into a folder that is
#: deleted when it exits.
USER_LIMITS = install.app_dir() / "user_limits.json"

STRATEGIES = [
    # label, strat number, session allocation, energy-neutral, one-shot
    ("Qualifying", 1, "quali", False, True),
    ("Race", 2, "race", True, False),
    ("Recharge", 3, "race", False, False),
]

#: Which map the written setup opens on. The picker for this was removed --
#: it is an ordinary setup item, changeable in the pit screen, and anyone
#: using this already knows 1 is qualifying. Qualifying is the one worth
#: checking first, because its lap time is the one comparable to a real lap.
DEFAULT_ACTIVE = 1


def docs_dir() -> Path:
    return Path(os.path.expanduser("~")) / "Documents" / "Assetto Corsa"


def baseline_dir() -> Path:
    return docs_dir() / "fa26_baseline"


# --------------------------------------------------------------------- limits
_CAR_LIMITS: dict | None = None


def car_limits() -> dict:
    """The per-lap recharge range the car itself accepts, in tenths of a MJ.

    Read from the car's own setup.ini rather than hardcoded, so a slider
    cannot offer a number the car will silently clamp. Qualifying runs
    4.0-9.0 MJ and the race 4.0-8.5; past the top the car applies no per-lap
    cap at all, which is the ERS_UNLIMITED_MODE switch VRC added for circuits
    with no published allocation.
    """
    global _CAR_LIMITS
    if _CAR_LIMITS is None:
        attrs = setupfile.item_attributes(setup_ini())
        out = {}
        for session, item in (("qualify", "ERS_REGEN_MAX_MJ_Q"),
                              ("race", "ERS_REGEN_MAX_MJ_R")):
            a = attrs.get(item, {})
            out[session] = {"min": int(a.get("MIN", 40)),
                            "max": int(a.get("MAX", 90)),
                            "step": int(a.get("STEP", 1))}
        _CAR_LIMITS = out
    return _CAR_LIMITS


def _CAR_LIMITS_RESET() -> None:
    global _CAR_LIMITS
    _CAR_LIMITS = None
    _DETAIL_CACHE.clear()


def load_user_limits() -> dict:
    try:
        return json.loads(USER_LIMITS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_user_limits(circuit: str, qualify: int, race: int,
                     unlimited: bool) -> None:
    table = load_user_limits()
    table[circuit] = {"qualify": qualify, "race": race,
                      "unlimited": bool(unlimited)}
    USER_LIMITS.parent.mkdir(parents=True, exist_ok=True)
    USER_LIMITS.write_text(json.dumps(table, indent=2, sort_keys=True),
                           encoding="utf-8")


def forget_user_limits(circuit: str) -> None:
    table = load_user_limits()
    if table.pop(circuit, None) is not None:
        USER_LIMITS.write_text(json.dumps(table, indent=2, sort_keys=True),
                               encoding="utf-8")


def apply_limits(alloc, qualify: int, race: int, unlimited: bool):
    """An allocation carrying the user's own qualifying and race figures.

    The other three session variants are not asked for -- five sliders to set
    one number is not a workflow. They follow the way the published notices
    relate them: race-with-Overtake half a MJ above the race figure, practice
    and the out lap at the generous end, each held to what the car accepts.
    """
    limits = car_limits()
    top = limits["qualify"]["max"]
    return replace(
        alloc,
        qualify=qualify,
        race=race,
        race_overtake=min(race + 5, limits["race"]["max"]),
        practice=min(max(qualify, race) + 5, top),
        outlap=min(max(qualify, race) + 5, top),
        source="no per-lap cap (you set this)" if unlimited else "you set this",
    )


# -------------------------------------------------------------------- reading
def scan_lap(path) -> dict:
    """Fuel and starting charge without paying for a full load.

    `lap.load` smooths and differentiates every channel, which is a second or
    two per lap and wasted when all that is wanted is two numbers on a card.
    """
    fuel: list[float] = []
    charge = None
    try:
        with open(path, encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("fuel"):
                    fuel.append(float(row["fuel"]))
                if charge is None and row.get("kers_charge"):
                    charge = float(row["kers_charge"])
    except (OSError, KeyError, ValueError):
        pass
    return {"fuel_l": sum(fuel) / len(fuel) if fuel else None, "charge": charge}


def thin(values: list, count: int) -> list:
    """`values` reduced to at most `count` points, keeping the peaks.

    A plain stride drops the apex of every straight, which is the one feature
    the trace exists to show, so each output point takes the maximum of the
    samples it stands for.
    """
    n = len(values)
    if n <= count:
        return [round(v, 1) for v in values]
    step = n / count
    return [round(max(values[int(i * step):max(int((i + 1) * step),
                                              int(i * step) + 1)]), 1)
            for i in range(count)]


def clock(seconds: float) -> str:
    return "%d:%06.3f" % (int(seconds // 60), seconds % 60)


def friendly_stamp(stamp: str) -> str:
    m = re.match(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})", stamp)
    if not m:
        return stamp
    y, mo, d, h, mi = m.groups()
    return "%s/%s  %s:%s" % (d, mo, h, mi)


def newest_key() -> "str | None":
    """The session being driven now, or the last one driven.

    Only one session is ever offered. A deployment map is written into a setup
    and stays there, so the workflow is drive-solve-apply once per circuit;
    a list of every session ever recorded was forty rows of history to scroll
    past on the way to the one row that mattered.
    """
    folder = baseline_dir()
    if not folder.exists():
        return None
    groups = lapsession.group(folder.glob("*.csv"))
    if not groups:
        return None
    return max(groups, key=lambda k: k.split("__")[-1])


def session_paths(key: str) -> list[Path]:
    groups = lapsession.group(baseline_dir().glob("*.csv"))
    if key not in groups:
        raise KeyError("no recorded session called %s" % key)
    return lapsession.select(groups[key])


def session_files(key: str) -> list[Path]:
    """Every lap in the session, before the 3% filter."""
    groups = lapsession.group(baseline_dir().glob("*.csv"))
    return sorted(groups.get(key, []))


def newest_lap_time(key: str) -> float:
    """When the last lap of this session was written."""
    return max((f.stat().st_mtime for f in session_files(key)), default=0.0)


def session_revision(key: str) -> str:
    """Cheap fingerprint that changes when a new lap lands.

    The page watches this while you drive, so it has to cost nothing: the
    full card reads every CSV in the session to average the fuel.
    """
    files = session_files(key)
    newest = max((f.stat().st_mtime for f in files), default=0)
    return "%s:%d:%.0f" % (key, len(files), newest)


_DETAIL_CACHE: dict = {}


def session_detail(key: str) -> dict:
    """Everything the left rail shows before a solve is run."""
    revision = session_revision(key)
    cached = _DETAIL_CACHE.get(key)
    if cached and cached[0] == revision:
        return cached[1]

    paths = session_paths(key)
    all_files = session_files(key)
    parsed = [(p, lapsession.parse(p)) for p in paths]
    parsed.sort(key=lambda item: item[1][2] if item[1] else 1e9)
    best = parsed[0][0]

    scans = [scan_lap(p) for p in paths]
    fuels = [s["fuel_l"] for s in scans if s["fuel_l"]]
    charges = [s["charge"] for s in scans if s["charge"] is not None]

    track = "__".join(key.split("__")[:-1])
    circuit = recharge.normalise(track)
    alloc = recharge.lookup(str(best))
    saved = load_user_limits().get(circuit)
    limits = car_limits()

    if saved:
        qualify = saved["qualify"]
        race = saved["race"]
        unlimited = bool(saved.get("unlimited", False))
    else:
        qualify = min(max(alloc.qualify, limits["qualify"]["min"]),
                      limits["qualify"]["max"])
        race = min(max(alloc.race, limits["race"]["min"]),
                   limits["race"]["max"])
        unlimited = False

    warnings = []
    if lapsession.map_order_warning(paths, baseline_dir()):
        warnings.append({
            "level": "warn", "title": "Some laps came before your last save",
            "text": "You saved a map after some of these laps were driven, "
                    "so they may not match what the car is running. Save, "
                    "restart the session, then drive."})
    if charges and max(charges) < 0.90:
        warnings.append({
            "level": "warn", "title": "Battery was not full",
            "text": "These laps started at %.0f%%. The qualifying map "
                    "assumes full, so run STRAT 3 for a lap first."
                    % (100 * max(charges))})
    if alloc.is_baseline and not saved:
        warnings.append({
            "level": "info", "title": "No official limit for this circuit",
            "text": "The figure below is a guess, and it is the number that "
                    "matters most — what you can recover sets what you "
                    "can spend."})

    detail = {
        "key": key,
        "revision": revision,
        "newest_lap": newest_lap_time(key),
        "circuit": circuit,
        "name": pretty(circuit),
        "track": track,
        "when": friendly_stamp(key.split("__")[-1]),
        "laps": [{"label": Path(p).name.split("__")[-1].replace(".csv", ""),
                  "time": clock(info[2]) if info else ""}
                 for p, info in parsed],
        "pooled": len(paths),
        "recorded": len(all_files),
        "best": clock(parsed[0][1][2]) if parsed[0][1] else "",
        "fuel_l": sum(fuels) / len(fuels) if fuels else None,
        "charge": max(charges) if charges else None,
        "known": not alloc.is_baseline,
        "source": alloc.source,
        "published": {"qualify": alloc.qualify, "race": alloc.race},
        "qualify": qualify,
        "race": race,
        "unlimited": unlimited,
        "overridden": bool(saved),
        "limits": limits,
        "warnings": warnings,
    }
    _DETAIL_CACHE[key] = (revision, detail)
    return detail


def readiness() -> dict:
    """What is stopping this machine from running the optimizer.

    Everything the app needs is somewhere else on disk -- the game, the car,
    the logger -- and each can be absent. Reported together so the first
    screen can say which one, rather than the app failing on whichever is
    reached first.
    """
    root = install.find_root()
    out = {"ok": False, "version": VERSION,
           "root": str(root) if root else "",
           "found_automatically": root is not None and not install.saved_root(),
           "car": False, "logger": False, "problem": "", "kind": ""}
    if root is None:
        out["kind"] = "no_ac"
        out["problem"] = ("Assetto Corsa could not be found. Paste the folder "
                          "that contains content\\cars.")
        return out
    out["car"] = install.car_folder(CAR_ID, root) is not None
    out["logger"] = install.logger_installed(root)
    if not out["car"]:
        out["kind"] = "no_car"
        out["problem"] = ("The VRC Formula Alpha 2026 is not installed here. "
                          "This tool only works with that car.")
        return out
    try:
        car_data()
    except install.MissingCarData as exc:
        out["kind"] = exc.kind
        out["problem"] = exc.message
        return out
    out["ok"] = True
    return out


#: A lap saved within a few seconds of the game starting still belongs to
#: that run; file timestamps and process creation time are not measured off
#: the same clock tick.
LIVE_SLACK_S = 10.0


def current() -> dict:
    """What the page shows on the left: this session, or nothing yet.

    "This session" means laps driven since the sim started. It used to mean
    the newest folder on disk, which called a session from hours and a reboot
    ago live -- announcing as current exactly the stale state the map-order
    guard exists to catch.
    """
    game = install.game_running()
    key = newest_key()
    payload = {"folder": str(baseline_dir()), "car": CAR_ID,
               "limits": car_limits(), "game": game, "version": VERSION}
    if key is None:
        payload["session"] = None
        return payload
    detail = dict(session_detail(key))
    detail["live"] = bool(
        game["running"] and game["since"]
        and detail["newest_lap"] >= game["since"] - LIVE_SLACK_S)
    payload["session"] = detail
    return payload


def setups_for(circuit: str) -> list[dict]:
    """Setup files in this circuit's folder, newest first.

    Listing them beats a file dialog: the folder is three levels deep inside
    Documents, its name is the track's internal id rather than anything a
    human would search for, and picking the wrong car's folder writes a map
    into a setup that will never load it.
    """
    root = docs_dir() / "setups" / CAR_ID
    if not root.is_dir():
        return []
    out = []
    for folder in root.iterdir():
        if not folder.is_dir() or recharge.normalise(folder.name) != circuit:
            continue
        for path in folder.glob("*.ini"):
            out.append({"path": str(path), "name": path.stem,
                        "folder": folder.name,
                        "when": time.strftime(
                            "%d/%m %H:%M",
                            time.localtime(path.stat().st_mtime)),
                        "mtime": path.stat().st_mtime,
                        "ours": path.with_suffix(".ini.bak").exists()})
    return sorted(out, key=lambda s: s["mtime"], reverse=True)


# ---------------------------------------------------------------------- solve
#: What the progress bar is measuring, and the slice of it each stage owns.
#: The two searched strategies take almost all the time and report real
#: progress from inside the solver, so the bar is a measurement rather than a
#: guess; the recharge map is built rather than searched and is instant.
PHASES = {
    "read": ("Reading your laps", 0.00, 0.07),
    "learn": ("Learning how your car behaves", 0.07, 0.15),
    "quali": ("Working out the qualifying map", 0.15, 0.57),
    "race": ("Working out the race map", 0.57, 0.94),
    "recharge": ("Working out the recharge map", 0.94, 1.00),
}


def pretty(circuit: str) -> str:
    """A circuit key as a name: `red_bull_ring` -> `Red Bull Ring`."""
    return " ".join(w.capitalize() for w in circuit.replace("_", " ").split())


class Cancelled(Exception):
    """Raised out of the progress callback to abandon a running solve."""


def plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


def gap(seconds: float) -> str:
    """A lap-time difference the way a driver would say it."""
    if abs(seconds) < 0.05:
        return "about the same as"
    return "%.1fs %s than" % (abs(seconds),
                              "quicker" if seconds < 0 else "slower")


class Engine:
    """Solver state, and the running commentary the page reads out of."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.lines: list[dict] = []
        self.status = "Ready"
        self.phase = ""
        self.progress = 0.0
        self.running = False
        self.started_at = 0.0
        self.generation = 0
        self.results: list = []
        self.summary: list[dict] = []
        self.lap = None
        self.alloc = None
        self.unlimited = False
        self.error: str | None = None
        self.stopping = False
        self.cancelled = False
        self.car_setup_ini = ""
        self._span = (0.0, 1.0)

    # -------------------------------------------------------------- output
    def _put(self, text: str, kind: str) -> None:
        with self.lock:
            self.lines.append({"kind": kind, "text": text})

    def say(self, text: str = "") -> None:
        """A line anyone can read. This is what the page shows by default."""
        self._put(text, "line")

    def note(self, text: str) -> None:
        """Something that changes what the user should do next."""
        self._put(text, "warn")

    def detail(self, text: str) -> None:
        """The engineering read-out, behind the technical-details toggle.

        Kept rather than deleted: it is what makes a surprising answer
        diagnosable, and every number in it has been needed at least once.
        """
        self._put(text, "detail")

    def headline(self, text: str) -> None:
        self._put(text, "result")

    # ------------------------------------------------------------ progress
    def enter(self, phase: str) -> None:
        name, lo, hi = PHASES[phase]
        with self.lock:
            self._span = (lo, hi)
            self.phase = name
            self.progress = lo
            self.status = name

    def tick(self, fraction: float) -> None:
        self.check()
        lo, hi = self._span
        with self.lock:
            self.progress = max(self.progress, lo + (hi - lo) * min(1.0, fraction))

    # ----------------------------------------------------------------- stop
    def cancel(self) -> bool:
        """Ask the running solve to stop. Returns whether there was one."""
        with self.lock:
            if not self.running:
                return False
            self.stopping = True
            self.status = "Stopping"
        return True

    def check(self) -> None:
        """Give up here if the stop button has been pressed.

        Called from `tick`, which the solver reaches roughly twice a second,
        and at each stage boundary for the parts that report no progress of
        their own.
        """
        if self.stopping:
            raise Cancelled()

    def snapshot(self, since: int, have: int = -1) -> dict:
        """New lines, and the summary only when the caller lacks one.

        The summary carries every zone and a speed trace per strategy, which
        is tens of kilobytes; sending it on all four polls a second for the
        two minutes a solve takes is thirty megabytes of JSON to say nothing
        has changed. `have` is how many strategies the page already holds.
        """
        with self.lock:
            out = {
                "since": len(self.lines),
                "lines": self.lines[since:] if since < len(self.lines) else [],
                "reset": since > len(self.lines),
                "status": self.status,
                "phase": self.phase,
                "progress": round(self.progress, 4),
                "elapsed": round(time.time() - self.started_at, 1)
                if self.started_at else 0.0,
                "running": self.running,
                "stopping": self.stopping,
                "cancelled": self.cancelled,
                "generation": self.generation,
                "error": self.error,
            }
            if have != len(self.summary):
                out["summary"] = self.summary
            return out

    # ---------------------------------------------------------------- start
    def start(self, key: str, qualify: int, race: int, unlimited: bool,
              write_alloc: bool) -> None:
        with self.lock:
            if self.running:
                raise RuntimeError("the optimizer is already running")
            self.lines = []
            self.results = []
            self.summary = []
            self.error = None
            self.stopping = False
            self.cancelled = False
            self.running = True
            self.progress = 0.0
            self.started_at = time.time()
            self.generation += 1
            self.status = "Starting"
        threading.Thread(
            target=self._guard,
            args=(key, qualify, race, unlimited, write_alloc),
            daemon=True).start()

    def _guard(self, *args) -> None:
        try:
            self._solve(*args)
            with self.lock:
                self.progress = 1.0
                self.status = "Done"
                self.phase = ""
        except Cancelled:
            # Half a plan is worse than none: a setup carrying one new map and
            # two old ones looks finished and quietly does something else, so
            # the finished strategies go too.
            with self.lock:
                self.results = []
                self.summary = []
                self.status = "Stopped"
                self.phase = ""
                self.progress = 0.0
                self.cancelled = True
            self.say()
            self.say("Stopped. Nothing saved, and the finished maps were "
                     "discarded — one new map beside two old ones would "
                     "look finished and quietly do something else.")
        except Exception:
            with self.lock:
                self.status = "Something went wrong"
                self.error = traceback.format_exc()
            self.note("The optimizer hit an error. Turn on Details below to "
                      "see where.")
            self.detail(traceback.format_exc())
        finally:
            with self.lock:
                self.running = False

    def _solve(self, key: str, qualify: int, race: int, unlimited: bool,
               write_alloc: bool) -> None:
        self.enter("read")
        paths = session_paths(key)
        track = "__".join(key.split("__")[:-1])
        circuit = recharge.normalise(track)

        data = car_data()
        car = carphysics.load(data)
        self.car_setup_ini = (data / "setup.ini").read_text(
            encoding="utf-8", errors="replace")
        self.tick(0.2)

        # Model the whole session, not the one lap that happens to be fastest.
        # Fitting a single lap overfits it: the map comes out perfect on that
        # lap and has three to five power steps refused on every other lap of
        # the same session, and the lap time it claims is up to 1.9 s better
        # than what it then delivers. Pooling halves that gap and improves
        # out-of-sample accuracy from 0.57% to 0.44%.
        laps = []
        for i, path in enumerate(paths):
            laps.append(laplib.load(path))
            self.tick(0.2 + 0.8 * (i + 1) / len(paths))
        laps.sort(key=lambda l: l.lap_time_s)
        lap = laps[0]

        recorded = len(session_files(key))
        self.say("Read %d %s from your %s session. Your best was %s."
                 % (recorded, plural(recorded, "lap"), pretty(circuit),
                    clock(lap.lap_time_s)))
        if len(laps) > 1:
            self.say("Using all %d, not just the quick one." % len(laps))
        elif recorded > 1:
            self.say("Using your best lap only — the other %d were more "
                     "than 3%% slower." % (recorded - 1))
        self.detail("Modelling      %d lap(s), pooled; length %.0f m"
                    % (len(laps), lap.length_m))

        # Fuel is in the telemetry, so it is not something to type in. The old
        # box defaulted to 45 L while a Monza push lap ran at 6.9 -- 28 kg of
        # car that was not there.
        measured = [s["fuel_l"] for s in (scan_lap(p) for p in paths)
                    if s["fuel_l"]]
        if measured:
            car.fuel_liters = sum(measured) / len(measured)
            self.say("Fuel %s L, read from the telemetry."
                     % ("%.0f" % car.fuel_liters if car.fuel_liters >= 10
                        else "%.1f" % car.fuel_liters))
            self.detail("Fuel           %.2f L, mean over the session"
                        % car.fuel_liters)

        # Was this map written after these laps were driven? Three circuits in
        # a row were applied in the wrong order, which makes the telemetry
        # unattributable and every lap-time comparison against it meaningless.
        if lapsession.map_order_warning(paths, baseline_dir()):
            self.note("Some of these laps predate the map now in your setup, "
                      "so they may not match what you are running. Save, "
                      "restart the session, then drive.")
            self.detail("Order          %s" % lapsession.map_order_warning(
                paths, baseline_dir()))

        # A qualifying map is planned for a full store because it is meant to
        # follow a recharge lap. Arrive short and it is the wrong map -- 0.48 s
        # wrong at Spa, where the lap began at 78%.
        charge = max(l.store_kj[0] for l in laps)
        if charge < laplib.STORE_CAPACITY_KJ * 0.90:
            self.note("Battery was %.0f%% at the start of these laps. The "
                      "qualifying map assumes full — charge up on STRAT 3 "
                      "first." % (100 * charge / laplib.STORE_CAPACITY_KJ))

        self.enter("learn")
        self.check()
        cal = calibrate.fit_longitudinal(laps, car)
        self.check()
        self.tick(0.6)
        profile = prof.Profile(laps)
        self.say("Worked out how your car accelerates, brakes and grips.")
        self.detail("Drag profile   %d bins, median k %.3f"
                    % (len(cal.drag.k), cal.drag.k_default))
        self.detail("Traction       %.2f g     braking %.2f + %.5f v^2"
                    % (cal.traction_ms2 / 9.81, cal.brake_c0, cal.brake_c1))

        alloc = recharge.lookup(str(paths[0]))
        published = not alloc.is_baseline
        if (qualify, race) != (alloc.qualify, alloc.race) or unlimited:
            alloc = apply_limits(alloc, qualify, race, unlimited)
        self.alloc = alloc
        self.unlimited = unlimited

        if unlimited:
            self.say("No recharge limit set for this circuit.")
        else:
            where = ("official"
                     if published and alloc.source.startswith("fia")
                     else "your setting, no official figure exists")
            self.say("Recharge limit %.1f MJ qualifying, %.1f race (%s)."
                     % (alloc.mj("qualify"), alloc.mj("race"), where))
        self.detail("Recharge       Q %.1f  R %.1f  R+OT %.1f  [%s]"
                    % (alloc.mj("qualify"), alloc.mj("race"),
                       alloc.mj("race_overtake"), alloc.source))
        self.detail("Power curve    %s (race), %s (qualifying)"
                    % (alloc.curve_standard or "unknown",
                       alloc.curve_override or "unknown"))
        if alloc.curve_standard and not fa26.taper_is_known(alloc.curve_standard):
            self.detail("               %s has no published numbers, so the "
                        "Base curve stands in." % alloc.curve_standard)

        # Article C5.12.5: sectors where a reset of MGU-K power reduction is
        # permitted, so a map may raise deployment under full throttle there.
        # Every window published so far is bracketed SQ and Q only, so the
        # race and recharge strategies get none of them.
        speed_zones = alloc.speed_zones_for(lap.length_m)
        zones_for = lambda strat: alloc.reset_zones_for(
            "qualify" if strat == 1 else "race", lap.length_m)
        # Article C5.2.8 names a curve for normal running and one for
        # Override. Qualifying runs with Override, the race does not, and the
        # Standard curve starts tapering at 290 kph against the Overtake
        # curve's 337 -- so applying one to both overstates race deployment
        # everywhere above 290.
        taper_for = lambda strat: fa26.taper_for(
            alloc.curve_override if strat == 1 else alloc.curve_standard)

        deployed, harvested = lap.totals()
        self.detail("As driven      deploy %.2f MJ, harvest %.2f MJ"
                    % (deployed, harvested))

        seed = optimise26.seed_splits(lap, profile)
        self.say("Starting from %d zones, set by where you lift and get "
                 "back on the throttle." % len(seed))
        self.detail("Seeded %d splits from the throttle trace." % len(seed))

        # Any setup already sitting in this track's folder -- VRC's own
        # optional setups especially -- carries a hand-authored map. A hill
        # climb only reaches what its starting point deforms into, and VRC's
        # Spa map simulated 0.85 s faster than this solver's own answer, so
        # their work is used as an extra starting point.
        reference = self._reference_maps(track)
        if reference:
            self.say("Also trying %d hand-made %s for this track."
                     % (len(reference), plural(len(reference), "map")))
        if not published and not unlimited:
            self.note("No official recharge figure for this circuit — "
                      "everything below rests on the one you set.")
        self.say()

        for label, strat_no, session, neutral, one_shot in STRATEGIES:
            kind = "quali" if strat_no == 1 else (
                "race" if strat_no == 2 else "recharge")
            self.check()
            self.enter(kind)
            self.say("Working out the %s map…" % label.lower())

            # The car's ERS_SESSION_LIMITS defaults to "Current Session", so
            # the cap actually enforced is whichever session is loaded --
            # practice allows 9.0 MJ where qualifying allows 8.0. A lap that
            # demonstrably harvested more than the table allows is taken as
            # proof of the real limit.
            cap = 99.0 if unlimited else alloc.mj(session)
            if not unlimited and harvested > cap + 0.2 and strat_no == 1:
                self.note("These laps recovered more than qualifying allows. "
                          "Practice is more generous — drive in a Qualify "
                          "session to match this plan.")
            objective = optimise26.Objective(
                name=label, harvest_cap_mj=cap,
                energy_neutral=neutral, one_shot=one_shot,
                min_store_fraction=0.25 if neutral else 0.0,
                # A qualifying map assumes the recharge lap left the store
                # full. Arrive at 78% instead, as a real Spa lap did, and a map
                # tuned only for full falls apart faster than one that was not
                # -- so it is scored at three-quarters charge too.
                charge_fractions=(0.75,) if one_shot else ())
            powertrain = fa26.Powertrain(taper=taper_for(strat_no))
            start_store = optimise26.starting_store_kj(lap, kind)
            if strat_no == 3:
                # Built, not searched -- see optimise26.recharge_map.
                splits, result = optimise26.build_recharge(
                    seed, car, cal, profile, powertrain,
                    start_store_kj=start_store,
                    harvest_cap_kj=objective.harvest_cap_mj * 1000,
                    reset_zones=zones_for(strat_no), speed_zones=speed_zones)
            else:
                splits, result, _ = optimise26.optimise(
                    seed, car, cal, profile, powertrain, objective,
                    start_store, extra_seeds=reference,
                    reset_zones=zones_for(strat_no), speed_zones=speed_zones,
                    on_progress=self.tick)
            self.tick(1.0)
            self.check()

            notes = self._describe(label, strat_no, result, splits, lap, cal,
                                   profile, zones_for(strat_no))
            spread = optimise26.sensitivity(
                splits, car, cal, profile, powertrain, objective)
            self.check()
            worst = max(r.lap_time_s for _, r in spread) - result.lap_time_s
            if strat_no == 3:
                pass
            elif worst < 0.05:
                self.say("   Unaffected by how much charge you start with.")
            else:
                self.say("   Starting with less charge costs at most %.1fs."
                         % worst)
            self.detail("               charge sensitivity: "
                        + ", ".join("%.0f%% -> %s" % (f * 100, clock(r.lap_time_s))
                                    for f, r in spread))
            self.say()

            self.results.append((label, strat_no, splits, result))
            with self.lock:
                self.summary.append({
                    "label": label,
                    "strat": strat_no,
                    "lap": clock(result.lap_time_s),
                    "lap_s": result.lap_time_s,
                    "delta_s": result.lap_time_s - lap.lap_time_s,
                    "deploy_mj": result.deployed_mj,
                    "harvest_mj": result.harvested_mj,
                    "store_start": result.store_start_kj,
                    "store_end": result.store_end_kj,
                    "store_min": result.store_min_kj,
                    "notes": notes,
                    "map": [
                        {"start": s.start_m, "deploy_end": s.deploy_end_m,
                         "clip_start": s.clip_start_m, "end": s.end_m,
                         "deploy_kw": s.deploy_kw, "clip_kw": s.clip_kw}
                        for s in (s.clamped(lap.length_m) for s in splits)
                        if s.deploy_kw > 0 or s.clip_kw > 0],
                    "length_m": lap.length_m,
                    # The simulated speed trace, thinned to something a strip
                    # 1000 units wide can draw. Without it the map is a row of
                    # bars over an axis that means nothing; with it you can see
                    # the deploy blocks sit on the straights.
                    "speed": thin([v * 3.6 for v in result.speed], 240),
                    "peak_kph": max(result.speed) * 3.6 if result.speed else 0,
                })

        self.lap = lap
        self.say("Three maps ready. Save them into a setup, then restart "
                 "the session.")

    def _describe(self, label, strat_no, result, splits, lap, cal, profile,
                  reset_zones) -> list[str]:
        """Say what a finished map is, in words, and return the card's notes.

        The card shows the same sentences as the activity list, so a user who
        collapsed one still sees what is wrong with a map.
        """
        notes: list[str] = []
        pct = lambda kj: 100 * kj / laplib.STORE_CAPACITY_KJ
        self.headline("%s map: %s — %s your best real lap."
                      % (label, clock(result.lap_time_s),
                         gap(result.lap_time_s - lap.lap_time_s)))
        line = "   Battery %.0f%% to %.0f%%" % (
            pct(result.store_start_kj), pct(result.store_end_kj))
        if round(pct(result.store_min_kj)) < round(pct(result.store_end_kj)):
            line += ", low of %.0f%%" % pct(result.store_min_kj)
        self.say(line + ".")
        live = sum(1 for s in splits if s.deploy_kw > 0 or s.clip_kw > 0)
        self.detail("%-11s -> STRAT %d   lap %.3f s   deploy %.2f MJ   "
                    "harvest %.2f MJ   %d splits (%d active)"
                    % (label, strat_no, result.lap_time_s, result.deployed_mj,
                       result.harvested_mj, len(splits), live))
        self.detail("               store %.0f -> %.0f kJ (min %.0f)"
                    % (result.store_start_kj, result.store_end_kj,
                       result.store_min_kj))

        # A map built from laps driven on an empty map takes the car past the
        # speeds the drag fit ever saw, and it is the user who closes that loop.
        if cal.extrapolating_kph(result.speed) > 8.0:
            notes.append(
                "This map goes faster than any lap you have driven here, so "
                "the time is a guess. Drive it and run the optimizer again.")
            self.note("   " + notes[-1])
            self.detail("               fitted to %.0f kph, map runs to %.0f"
                        % (cal.observed_speed_p95 * 3.6,
                           max(result.speed) * 3.6))
        # A recharge lap refuses harvest once it is full, which is the point of
        # it, not a fault to report.
        if result.refused_mj > 0.1 and strat_no != 3:
            notes.append(
                "%.1f MJ of recovery was lost to a full battery. Using more "
                "earlier in the lap would make room." % result.refused_mj)
            self.note("   " + notes[-1])
        # A map the car will not execute is worse than no map: it looks like a
        # plan and quietly does something else. The search is held to this, so
        # a survivor here is a bug, not a preference.
        illegal = optimise26.illegal_increases(splits, profile, reset_zones)
        if illegal:
            notes.append(
                "%d power changes the car will refuse. Please report this."
                % len(illegal))
            self.note("   " + notes[-1])
            self.detail("               illegal increases at %s" % (illegal[:4],))
        return notes

    #: Each extra seed is a whole extra hill climb, so the cap is a direct
    #: multiplier on solve time, not a refinement.
    MAX_REFERENCE_SEEDS = 2

    def _reference_maps(self, track: str) -> list:
        """Maps *someone else* authored for this track, as extra seeds.

        The point of a second starting point is to reach a shape the seed
        cannot deform into -- VRC's Spa map simulated 0.85 s faster than the
        one this solver reached on its own. The point is not to restart from
        this tool's own previous answer, which is what a plain folder scan
        does: a Monza solve picked up the three maps written by the last run
        and ran four full climbs instead of one.

        A file this tool has written is identifiable: `setupfile.write_values`
        leaves a `.bak` beside it holding the content from before the first
        write. So where a `.bak` exists, the `.bak` is the authored original
        and the `.ini` is our own output -- read the former, skip the latter.
        """
        mapping = setupfile.item_index_map(self.car_setup_ini)
        found: list = []
        seen: set = set()

        def remember(splits) -> None:
            """Keep a map only if its shape is one not already collected.

            The shipped corpus entry for a track is usually byte-identical to
            map 1 of the setup sitting in that track's folder -- same file. A
            Spa solve once spent both seed slots on the same map and dropped
            VRC's map 2 entirely, and that map replays 0.30 s faster than what
            the solver produced from the one it kept.
            """
            key = tuple((s.start_m, s.deploy_end_m, s.clip_start_m, s.end_m,
                         s.deploy_kw, s.clip_kw) for s in splits)
            if key not in seen:
                seen.add(key)
                found.append(splits)

        for splits in recharge.reference_maps_for(track):
            remember([fa26.Split(*row) for row in splits])

        circuit = recharge.normalise(track)
        root = docs_dir() / "setups" / CAR_ID
        folders = [d for d in (root.iterdir() if root.is_dir() else [])
                   if d.is_dir() and recharge.normalise(d.name) == circuit]
        for folder in folders:
            for path in sorted(folder.glob("*.ini")):
                backup = path.with_suffix(path.suffix + ".bak")
                source = backup if backup.exists() else path
                try:
                    text = source.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for index in range(1, 13):
                    try:
                        splits = fa26.read_map(text, mapping, index)
                    except Exception:
                        continue
                    if len(splits) >= 4:
                        remember(splits)
        return found[: self.MAX_REFERENCE_SEEDS]

    # ---------------------------------------------------------------- apply
    def apply(self, setup_path: str, write_alloc: bool) -> dict:
        if not self.results:
            raise RuntimeError("nothing to apply -- run the optimiser first")
        mapping = setupfile.item_index_map(self.car_setup_ini)
        backup = fa26.write_strategies(
            setup_path, mapping,
            [(n, splits) for _, n, splits, _ in self.results],
            self.lap.length_m,
            allocation=self.alloc if write_alloc else None,
            active=DEFAULT_ACTIVE,
            # Keep a dated copy of the written setup beside the telemetry.
            # Without one there is no way to tell afterwards which map a
            # recorded lap was driven on, because this overwrites the setup in
            # place -- which is how every Monza lap on disk came to be replayed
            # against a map written after the lap was recorded.
            telemetry_dir=baseline_dir())

        # The per-lap cap switch is not part of the allocation record, so it
        # is written separately -- and always, in both directions. Left alone
        # it would carry over from the last circuit, and a Monza map planned
        # to 5.0 MJ driven with the cap switched off is not the plan at all.
        if write_alloc and "ERS_UNLIMITED_MODE" in mapping:
            setupfile.write_values(
                setup_path,
                {mapping["ERS_UNLIMITED_MODE"]: 1 if self.unlimited else 0},
                backup=False)

        # `write_strategies` hands back a Path, and a Path does not survive
        # json.dumps -- which made the very first apply to any setup report a
        # 500 *after* writing the file correctly.
        return {"backup": str(backup) if backup else "",
                "stale": self._car_has_loaded(setup_path),
                "active": DEFAULT_ACTIVE}

    def _car_has_loaded(self, written: str) -> str:
        """Whether the game has picked up the file just written.

        Assetto Corsa copies the setup it loads into generic/last.ini, so if
        that file is older than the one just written, the car is still running
        the previous map. Three separate on-track sessions were spent chasing a
        bug that was really this, so it is worth saying out loud.
        """
        last = docs_dir() / "setups" / CAR_ID / "generic" / "last.ini"
        try:
            if not last.exists():
                return ""
            if last.stat().st_mtime < Path(written).stat().st_mtime:
                stamp = time.strftime("%H:%M",
                                      time.localtime(last.stat().st_mtime))
                return "the car last loaded a setup at %s" % stamp
        except OSError:
            return ""
        return ""


ENGINE = Engine()


# --------------------------------------------------------------------- server
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
        ".ico": "image/x-icon", ".woff2": "font/woff2", ".txt": "text/plain"}


#: Whether the page has ever actually been handed to a window. The launcher
#: has no other way to tell a window that was used and closed from one that
#: was never really ours -- see the end of `main`.
SERVED = {"pages": 0}


class Handler(BaseHTTPRequestHandler):
    server_version = "FA26Optimiser"

    def log_message(self, *args) -> None:  # noqa: D102 - quiet console
        pass

    # ----------------------------------------------------------------- send
    def _json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name: str) -> None:
        path = (UI_DIR / name).resolve()
        if UI_DIR.resolve() not in path.parents or not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    # ------------------------------------------------------------------ get
    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        route = url.path
        try:
            if route in ("/", "/index.html"):
                SERVED["pages"] += 1
                self._static("index.html")
            elif route.startswith("/ui/"):
                self._static(route[4:])
            elif route == "/api/ready":
                self._json(readiness())
            elif route == "/api/current":
                self._json(current())
            elif route == "/api/revision":
                key = newest_key()
                self._json({"revision": session_revision(key) if key else ""})
            elif route == "/api/setups":
                self._json({"setups": setups_for(query.get("circuit", [""])[0])})
            elif route == "/api/poll":
                self._json(ENGINE.snapshot(
                    int(query.get("since", ["0"])[0]),
                    int(query.get("have", ["-1"])[0])))
            else:
                self.send_error(404)
        except Exception as exc:
            self._json({"error": str(exc),
                        "trace": traceback.format_exc()}, code=500)

    # ----------------------------------------------------------------- post
    def do_POST(self) -> None:
        route = urlparse(self.path).path
        try:
            body = self._body()
            if route == "/api/solve":
                circuit = body.get("circuit", "")
                qualify = int(body["qualify"])
                race = int(body["race"])
                unlimited = bool(body.get("unlimited"))
                if body.get("remember"):
                    save_user_limits(circuit, qualify, race, unlimited)
                elif body.get("forget"):
                    forget_user_limits(circuit)
                ENGINE.start(body["key"], qualify, race, unlimited,
                             bool(body.get("write_alloc", True)))
                self._json({"ok": True})
            elif route == "/api/apply":
                self._json(ENGINE.apply(body["path"],
                                        bool(body.get("write_alloc", True))))
            elif route == "/api/setpath":
                folder = Path(body.get("path", "").strip().strip('"'))
                if not install.is_ac_root(folder):
                    raise RuntimeError(
                        "That folder has no content\\cars in it, so it is not "
                        "an Assetto Corsa install.")
                install.save_root(folder)
                _CAR_LIMITS_RESET()
                self._json(readiness())
            elif route == "/api/installlogger":
                install.install_logger()
                self._json(readiness())
            elif route == "/api/cancel":
                self._json({"stopping": ENGINE.cancel()})
            elif route == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown,
                                 daemon=True).start()
            else:
                self.send_error(404)
        except Exception as exc:
            self._json({"error": str(exc),
                        "trace": traceback.format_exc()}, code=500)


def free_port(preferred: int = 8731) -> int:
    for port in (preferred, 0):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return probe.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port")


# ---------------------------------------------------------------------- window
#: Chromium's app mode, which is what makes this a window rather than a tab:
#: no address bar, no tabs, its own taskbar button carrying the page's icon.
#: Edge ships with Windows, so on the target machine this needs nothing
#: installed -- which matters more for a tool meant to be handed to strangers
#: than a marginally nicer window frame would.
BROWSERS = (
    (r"Microsoft\Edge\Application\msedge.exe", "Edge"),
    (r"Google\Chrome\Application\chrome.exe", "Chrome"),
    (r"BraveSoftware\Brave-Browser\Application\brave.exe", "Brave"),
    (r"Chromium\Application\chrome.exe", "Chromium"),
)


def window_profile() -> Path:
    """A profile directory belonging to this launch alone.

    Kept out of the project folder and away from the real browser profile:
    app mode needs a directory of its own, and pointing it at yours makes it
    refuse to start whenever the browser is already open.

    One per launch, though, and that part is not tidiness. Chromium hands its
    command line to whichever process already owns a profile directory and
    then exits immediately -- so a shared directory made every run after the
    first fail: the browser returned at once, the `.wait()` in `open_window`
    returned with it, and the server was shut down underneath a window that
    was still loading. What the user saw was the browser's own connection
    error against a numeric address, with the application already gone.

    Edge leaves background processes alive after its last window closes, so
    there was nothing rare about this. A directory nobody else owns cannot be
    handed off, and `.wait()` means what it says again.
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    root = Path(base) / "FA26 Optimizer"
    root.mkdir(parents=True, exist_ok=True)
    sweep(root)
    profile = root / ("window-%d" % os.getpid())
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def sweep(root: Path) -> None:
    """Delete profiles left by earlier runs, and never a live one.

    A directory whose files are open cannot be renamed on Windows, which is
    exactly the test wanted here: a rename that fails belongs to a window
    somebody still has open, and leaving it alone is the right answer.
    """
    for old in list(root.glob("window-*")) + [root / "window"]:
        if not old.is_dir():
            continue
        try:
            spent = old.with_name(old.name + ".old")
            shutil.rmtree(spent, ignore_errors=True)
            old.rename(spent)
        except OSError:
            continue
        shutil.rmtree(spent, ignore_errors=True)


def find_browser():
    """A Chromium-family executable able to host an app window, or None."""
    roots = [os.environ.get(name) for name in
             ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for root in filter(None, roots):
        for rel, name in BROWSERS:
            exe = Path(root) / rel
            if exe.exists():
                return str(exe), name
    try:
        import winreg
    except ImportError:
        return None
    root = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for exe_name, label in (("msedge.exe", "Edge"), ("chrome.exe", "Chrome")):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                root + "\\" + exe_name) as handle:
                return winreg.QueryValueEx(handle, "")[0], label
        except OSError:
            continue
    return None


def open_window(url: str, prefer_browser: bool = False) -> None:
    """Show the UI in its own window and block until that window is closed.

    Three ways down, best first. pywebview embeds the page in a genuine native
    window; it is neither required nor shipped, but it is used when it happens
    to be installed. Otherwise a Chromium browser in app mode, which behaves
    like a window even though it is not one -- own taskbar button, own icon,
    no address bar and no tabs. Only if neither exists does this fall back to
    a tab, where nothing tells us the user has finished, so the process has to
    be stopped by hand.
    """
    if not prefer_browser:
        try:
            import webview
        except ImportError:
            webview = None
        if webview is not None:
            webview.create_window("FA26 ERS Deployment Optimizer", url,
                                  width=1440, height=940, min_size=(900, 620))
            webview.start()
            return

        found = find_browser()
        if found:
            exe, _name = found
            profile = window_profile()
            profile.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([
                exe,
                "--app=" + url,
                "--user-data-dir=" + str(profile),
                "--window-size=1440,940",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-sync",
            ]).wait()
            return

    webbrowser.open(url)
    print("No app window was available, so this opened in your browser.")
    print("Close this console window when you are done.")
    threading.Event().wait()


def server_answers(url: str, seconds: float = 8.0) -> bool:
    """Wait until the page can really be fetched, before showing a window.

    Opening the window first means any failure at all is reported by the
    browser, as a connection error against a numeric address -- which tells
    the user nothing except that a program they were told runs locally
    appears to be failing to reach the network.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as answer:
                if answer.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def fatal(message: str) -> None:
    """Report a startup failure when there is no console to print it to.

    The launcher runs under pythonw so no black window sits behind the app,
    which also means a traceback goes nowhere and the symptom of any failure
    is a double-click that appears to do nothing.
    """
    log = install.app_dir() / "error.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    message = "FA26 Optimizer %s\n%s\n\n%s" % (
        VERSION, time.strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        log.write_text(message, encoding="utf-8")
        message += "\n\nWritten to " + str(log)
    except OSError:
        pass
    print(message)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, message, "FA26 ERS Deployment Optimizer", 0x10)
    except Exception:
        pass


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Nothing is checked here any more. The game, the car and the logger are
    # all things a new user may not have in place yet, and a console message
    # they never see is no way to tell them -- the window opens and says so.
    try:
        # `--port` exists so the build can be driven over HTTP without a
        # window; there is no reason for anyone else to pass it.
        wanted = 0
        if "--port" in args:
            wanted = int(args[args.index("--port") + 1])
        port = wanted or free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except Exception:
        fatal("Could not start the local server.\n\n"
              + traceback.format_exc())
        return 1

    url = "http://127.0.0.1:%d/" % port
    print("FA26 ERS Deployment Optimizer  --  %s" % url)
    print("telemetry: %s" % baseline_dir())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        if "--headless" in args:
            server.serve_forever()
        else:
            if not server_answers(url):
                fatal("The application could not start.\n\n"
                      "Its own window runs on this machine at %s, and that "
                      "address did not answer. Nothing here uses the "
                      "internet; this is the program talking to itself.\n\n"
                      "Security software blocking a local connection is the "
                      "usual cause." % url)
                return 1
            open_window(url, prefer_browser="--browser" in args)
            # Nothing served means the window never reached us -- the browser
            # passed our command line to another of its own processes and
            # quit. The profile above is meant to make that impossible; if it
            # happens anyway, say so rather than vanish without a word.
            if not SERVED["pages"]:
                fatal("The window closed without ever showing the page.\n\n"
                      "This usually means another copy of the browser took "
                      "the window over. Close any window titled \"FA26 ERS "
                      "Deployment Optimizer\" and start the application "
                      "again.")
                return 1
    except KeyboardInterrupt:
        pass
    except Exception:
        fatal("Could not open the window.\n\n" + traceback.format_exc())
        return 1
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
