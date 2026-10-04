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

import copy
import csv
import ctypes
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
import history
import provenance
import session as lapsession
import setupfile
import sim26

UI_DIR = ROOT / "ui"
CAR_ID = fa26.CAR_ID

#: Shown in the window and stamped into anything a user sends back. The git
#: tag is the source of truth: `build_exe.py` refuses to build when this and
#: the tag disagree, because a build that misreports its own version turns
#: every bug report into a guess about which one it came from.
VERSION = "1.1.2"


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


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


#: FOLDERID_Documents. Asking Windows is the only way to get this right: the
#: folder shown as "Documents" is frequently not `%USERPROFILE%\Documents`.
_FOLDERID_DOCUMENTS = _GUID(
    0xFDD39AD0, 0x238F, 0x46AF,
    (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))


def known_documents() -> Path | None:
    """The real Documents folder, following any redirection."""
    try:
        buffer = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(_FOLDERID_DOCUMENTS), 0, None,
                ctypes.byref(buffer)) != 0:
            return None
        try:
            return Path(buffer.value) if buffer.value else None
        finally:
            ctypes.windll.ole32.CoTaskMemFree(buffer)
    except Exception:
        return None


def docs_candidates() -> list[Path]:
    home = Path(os.path.expanduser("~"))
    found = []
    known = known_documents()
    if known is not None:
        found.append(known / "Assetto Corsa")
    found.append(home / "Documents" / "Assetto Corsa")
    # Business OneDrive names the folder after the tenant, so glob rather
    # than guess: "OneDrive - Contoso" is as ordinary as plain "OneDrive".
    for drive in sorted(home.glob("OneDrive*")):
        found.append(drive / "Documents" / "Assetto Corsa")
    unique = []
    for path in found:
        if path not in unique:
            unique.append(path)
    return unique


def docs_dir() -> Path:
    """Assetto Corsa's Documents folder, wherever Windows really keeps it.

    This used to be `%USERPROFILE%\\Documents\\Assetto Corsa`, which is a
    guess, and wrong on any machine where Documents is redirected into
    OneDrive -- a Windows 11 default for plenty of people. The logger asks
    Custom Shaders Patch where the folder is and writes there quite happily;
    the app looked somewhere else and sat on "Waiting for laps" forever, with
    both halves working perfectly and nothing to say what was wrong.

    The recorded laps are the tiebreak, not the tidiest-looking path: a
    folder that actually holds telemetry beats one that merely exists.
    """
    chosen = install.saved_documents()
    if chosen is not None:
        # Someone pointing at the folder above is saying the same thing, so
        # take either -- this is the folder with `setups` in it.
        nested = chosen / "Assetto Corsa"
        return nested if nested.is_dir() and not (chosen / "setups").is_dir() \
            else chosen
    options = docs_candidates()
    for path in options:
        if (path / "fa26_baseline").is_dir():
            return path
    for path in options:
        if path.is_dir():
            return path
    return options[0]


#: Where an apply keeps its dated copy of the written setup. Normally beside
#: the telemetry, which is what makes a recorded lap attributable to a map
#: afterwards -- but the checks apply to a scratch setup, and a snapshot of
#: that is not a record of anything. It is a file named after a test fixture
#: sitting among the real ones, and fourteen had accumulated before anyone
#: noticed. `check_app.py` points this somewhere of its own.
SNAPSHOT_DIR: "Path | None" = None


def baseline_dir() -> Path:
    """Where the recorded laps are, the user's answer beating ours.

    Someone pointing at the Assetto Corsa documents folder rather than the
    `fa26_baseline` inside it has said something perfectly clear, so take
    either -- and re-check each time, because the subfolder appears the
    moment the first lap is recorded.
    """
    chosen = install.saved_telemetry()
    if chosen is not None:
        nested = chosen / "fa26_baseline"
        return nested if nested.is_dir() else chosen
    return docs_dir() / "fa26_baseline"


# ------------------------------------------------------------ blocked writes
# Windows Security's ransomware protection (Controlled folder access) turns
# away any program it does not know from Documents, without an error anyone
# sees. The game is one of those programs, and this app is another. A driver
# whose logger read "Laps saved: 2" over an empty folder had allowed acs.exe
# and Content Manager and still had to switch the protection off to get a lap.

#: What the driver is told when a write into Documents is refused.
BLOCKED_ADVICE = (
    "The usual cause is Windows Security's ransomware protection (Controlled "
    "folder access). Open Windows Security, then Virus & threat protection, "
    "then Protection history: it names the program it blocked. Allow that "
    "program under Ransomware protection, then Allow an app through "
    "Controlled folder access.")


def controlled_folder_access() -> str:
    """"on", "off" or "unknown", as Windows Defender records it.

    Best effort: Defender keeps it in the registry, readable without admin,
    as 1 when blocking. Another antivirus can block writes with this "off",
    so it is a hint to show beside an empty folder, never a verdict.
    """
    try:
        import winreg
    except ImportError:
        return "unknown"
    found = "off"
    for key in (r"SOFTWARE\Policies\Microsoft\Windows Defender\Windows Defender "
                r"Exploit Guard\Controlled Folder Access",
                r"SOFTWARE\Microsoft\Windows Defender\Windows Defender Exploit "
                r"Guard\Controlled Folder Access"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                value, _kind = winreg.QueryValueEx(
                    handle, "EnableControlledFolderAccess")
        except FileNotFoundError:
            continue
        except OSError:
            found = "unknown"
            continue
        # A policy value, where there is one, is the one Windows applies.
        return "on" if value == 1 else "off"
    return found


_WRITABLE: dict[str, bool] = {}


def can_write(folder: Path) -> bool | None:
    """Whether this program may create a file in `folder`. None if it is absent.

    Found by trying, once per folder per run -- readiness is polled every few
    seconds, and a blocked write raises a Windows notification each time.
    """
    if not folder.is_dir():
        return None
    key = str(folder)
    if key not in _WRITABLE:
        probe = folder / ("fa26_write_check_%d.tmp" % os.getpid())
        try:
            probe.write_bytes(b"fa26")
        except OSError:
            _WRITABLE[key] = False
        else:
            _WRITABLE[key] = True
            try:
                probe.unlink()
            except OSError:
                pass
    return _WRITABLE[key]


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
def _finite(value):
    """`value` with every NaN or infinite float replaced by None."""
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


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
                # A channel the game had no value for can come through as
                # "nan"; it is a missing reading, not a number to average.
                if row.get("fuel"):
                    value = float(row["fuel"])
                    if value == value:
                        fuel.append(value)
                if charge is None and row.get("kers_charge"):
                    value = float(row["kers_charge"])
                    if value == value:
                        charge = value
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


def usable_session(key: str, on_lap=None):
    """(paths, laps, rejected) -- the laps to model, loaded and checked.

    The pooling rule works from file names alone, so it can choose a lap that
    turns out to be unusable once loaded -- three laps the logger never cut,
    saved as one 3:48. When every lap it chose is refused, it chooses again
    from the rest of the session rather than giving up with real laps still
    on disk. `rejected` is (path, reason) for each lap that was refused.
    """
    candidates = session_files(key)
    rejected: list = []
    while candidates:
        paths = lapsession.select(candidates)
        laps = []
        for i, path in enumerate(paths):
            try:
                lap = laplib.load(path)
                why = laplib.problem(lap)
            except Exception as exc:
                lap, why = None, "unreadable (%s)" % exc
            if why:
                rejected.append((path, why))
            else:
                laps.append(lap)
            if on_lap:
                on_lap((i + 1) / len(paths))
        if laps:
            return [Path(l.path) for l in laps], laps, rejected
        candidates = [p for p in candidates if p not in paths]
    return [], [], rejected


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
    folder = baseline_dir()
    out = {"ok": False, "version": VERSION,
           "root": str(root) if root else "",
           "found_automatically": root is not None and not install.saved_root(),
           "car": False, "logger": False, "problem": "", "kind": "",
           # Every folder reached for outside this program, each with what
           # is actually in it. When something is not found, this is the
           # whole answer, and it used to be invisible.
           "app_dir": str(install.app_dir()),
           "live_search": install.live_search_enabled(),
           "protection": controlled_folder_access(),
           "blocked_advice": BLOCKED_ADVICE,
           "documents": {
               "folder": str(docs_dir()),
               "exists": docs_dir().is_dir(),
               "writable": can_write(docs_dir()),
               "setups": len(list((docs_dir() / "setups" / CAR_ID).iterdir()))
                         if (docs_dir() / "setups" / CAR_ID).is_dir() else 0,
               "chosen_by_hand": install.saved_documents() is not None,
           },
           "telemetry": {
               "folder": str(folder),
               "exists": folder.is_dir(),
               "laps": len(list(folder.glob("*.csv"))) if folder.is_dir() else 0,
               "chosen_by_hand": install.saved_telemetry() is not None,
               "looked_in": [str(option / "fa26_baseline")
                             for option in docs_candidates()],
           }}
    if root is None:
        out["kind"] = "no_ac"
        out["problem"] = ("Assetto Corsa could not be found. Paste the folder "
                          "that contains content\\cars.")
        return out
    out["logger_outdated"] = (install.logger_installed(root)
                              and not install.logger_current(root))
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


_LIVE_CACHE: dict = {}

#: A provisional estimate is worth having; a provisional estimate presented
#: as an answer is not. Below this many pooled laps the fit is still moving
#: enough that the number should be read as an indication.
LIVE_SETTLED_LAPS = 3


def live_estimate(key: str) -> dict:
    """A provisional qualifying lap time, cheap enough to run between laps.

    A full search is minutes, and running one while the driver is still on
    track would take the processor the game is using -- so this is not one.
    It is the screening step of the search on its own: the blanket family of
    starting maps is simulated once each and the best is reported. That is
    about a second of work, it needs no climb, and it is the same family the
    real search climbs from, so it is a floor on what a full run would find
    rather than a different answer.

    Which is why it is labelled. `provisional` says the fit is still moving;
    `converged` says enough consistent laps are pooled that another one is
    unlikely to change it much. Presenting an early number with the confidence
    of a finished one is the failure this is written to avoid.
    """
    revision = session_revision(key)
    cached = _LIVE_CACHE.get(key)
    if cached and cached[0] == revision:
        return cached[1]

    paths = session_paths(key)
    laps = []
    for path in paths:
        try:
            candidate = laplib.load(path)
        except Exception:
            continue
        if not laplib.problem(candidate):
            laps.append(candidate)
    if not laps:
        return {"ready": False, "reason": "no usable laps yet"}

    data = install.ensure_unpacked(fa26.CAR_ID)
    car = carphysics.load(data)
    measured = [s["fuel_l"] for s in (scan_lap(p) for p in paths) if s["fuel_l"]]
    if measured:
        car.fuel_liters = sum(measured) / len(measured)
    cal = calibrate.fit_longitudinal(laps, car)
    shape = prof.Profile(laps)
    laps.sort(key=lambda l: l.lap_time_s)

    alloc = recharge.lookup(str(paths[0]))
    saved = load_user_limits().get(recharge.normalise(
        "__".join(key.split("__")[:-1])))
    cap = (saved["qualify"] / 10.0) if saved else alloc.mj("qualify")
    powertrain = fa26.Powertrain(taper=fa26.taper_for(alloc.curve_override))
    objective = optimise26.Objective(
        name="Qualifying", harvest_cap_mj=cap, energy_neutral=False,
        one_shot=True, min_store_fraction=0.0)
    seed = optimise26.seed_splits(laps[0], shape)
    best = optimise26.screen(
        seed, car, cal, shape, powertrain, objective,
        optimise26.starting_store_kj(laps[0], "quali"),
        reset_zones=alloc.reset_zones_for("qualify", shape.length_m),
        speed_zones=alloc.speed_zones_for(shape.length_m))

    settled = len(laps) >= LIVE_SETTLED_LAPS
    out = {
        "ready": best is not None,
        "laps": len(laps),
        "best_real_s": laps[0].lap_time_s,
        "best_real": clock(laps[0].lap_time_s),
        "predicted_s": best.lap_time_s if best else 0.0,
        "predicted": clock(best.lap_time_s) if best else "",
        "delta_s": (best.lap_time_s - laps[0].lap_time_s) if best else 0.0,
        "converged": bool(settled),
        "note": ("From %d pooled laps." % len(laps) if settled
                 else "From %d lap%s — drive a couple more before trusting it."
                      % (len(laps), "" if len(laps) == 1 else "s")),
    }
    _LIVE_CACHE[key] = (revision, out)
    return out


class LiveSearch:
    """A full search between laps, for people who have asked for one.

    The provisional estimate is the screening step alone -- fifteen
    simulations, under half a second -- and it runs for everybody because it
    costs nothing. This is the other thing: the real search, the same one the
    button runs, started again every time a lap lands.

    It is off by default and it stays off until somebody turns it on, because
    the processor it would run on is the processor running Assetto Corsa. A
    solve is minutes of one core at full tilt, and a user whose frame rate
    dropped would have no reason at all to suspect the lap-time tool sitting
    behind the game. So the cost is stated where the switch is, and the switch
    starts off.

    One search at a time, never while the main engine is working, and abandoned
    the moment a newer lap arrives -- a result computed from laps that have
    already been superseded is the stale-lap problem again, wearing a hat.
    """

    #: How long after a lap lands before starting. A driver who has just
    #: crossed the line is about to drive another lap, and starting a solve
    #: into the first corner is the worst possible moment for it.
    SETTLE_S = 8.0

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.result: dict | None = None
        self.running = False
        self.want = None            # (key, revision) waiting to be searched
        self.generation = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def snapshot(self, key: str) -> dict:
        with self.lock:
            found = self.result
            busy = self.running
        if found and found.get("key") == key:
            out = dict(found)
            out["searching"] = busy
            return out
        return {"ready": False, "searching": busy}

    def note(self, key: str, revision: str) -> None:
        """A session is current. Queue a search for it if one is wanted."""
        if not install.live_search_enabled():
            with self.lock:
                self.result = None
            return
        with self.lock:
            self.want = (key, revision)

    def _loop(self) -> None:
        pending = None
        since = 0.0
        while True:
            time.sleep(1.0)
            with self.lock:
                want = self.want
            if want is None:
                continue
            if want != pending:
                pending, since = want, time.time()
                continue
            if time.time() - since < self.SETTLE_S:
                continue
            if ENGINE.running:
                since = time.time()          # the button wins; wait it out
                continue
            with self.lock:
                if self.result and self.result.get("revision") == want[1]:
                    continue
                self.running = True
                self.generation += 1
                mine = self.generation
            try:
                found = self._search(want[0], mine)
            except Exception:
                found = None
            with self.lock:
                self.running = False
                if found is not None and mine == self.generation:
                    found["key"], found["revision"] = want[0], want[1]
                    self.result = found

    def _stale(self, mine: int) -> bool:
        with self.lock:
            return mine != self.generation or ENGINE.running

    def _search(self, key: str, mine: int) -> dict | None:
        paths, laps, _rejected = usable_session(key)
        if not laps:
            return None
        laps.sort(key=lambda l: l.lap_time_s)

        data = install.ensure_unpacked(fa26.CAR_ID)
        car = carphysics.load(data)
        measured = [s["fuel_l"] for s in (scan_lap(p) for p in paths) if s["fuel_l"]]
        if measured:
            car.fuel_liters = sum(measured) / len(measured)
        cal = calibrate.fit_longitudinal(laps, car)
        shape = prof.Profile(laps)

        alloc = recharge.lookup(str(paths[0]))
        saved = load_user_limits().get(
            recharge.normalise("__".join(key.split("__")[:-1])))
        cap = (saved["qualify"] / 10.0) if saved else alloc.mj("qualify")
        powertrain = fa26.Powertrain(taper=fa26.taper_for(alloc.curve_override))
        objective = optimise26.Objective(
            name="Qualifying", harvest_cap_mj=cap, energy_neutral=False,
            one_shot=True, min_store_fraction=0.0, charge_fractions=(0.75,))

        def abandon(_fraction: float) -> None:
            """The search's own way out. Checked on every progress tick, so a
            newer lap or a press of the button stops it within a sweep rather
            than at the end of a solve."""
            if self._stale(mine):
                raise Cancelled()

        started = time.time()
        _splits, result, _evals = optimise26.optimise(
            optimise26.seed_splits(laps[0], shape), car, cal, shape, powertrain,
            objective, optimise26.starting_store_kj(laps[0], "quali"),
            reset_zones=alloc.reset_zones_for("qualify", shape.length_m),
            speed_zones=alloc.speed_zones_for(shape.length_m),
            on_progress=abandon)
        return {
            "ready": True,
            "laps": len(laps),
            "predicted_s": result.lap_time_s,
            "predicted": clock(result.lap_time_s),
            "best_real_s": laps[0].lap_time_s,
            "best_real": clock(laps[0].lap_time_s),
            "deploy_mj": result.deployed_mj,
            "took_s": time.time() - started,
        }


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


def braking_zones(profile, min_m: float = 25.0) -> list[list[float]]:
    """Contiguous stretches where the driver is on the brakes, in metres.

    Read off the pooled profile rather than any one lap, so a zone is where
    the driver brakes rather than where they braked once. Short ones are
    dropped: a stab of brake mid-corner is not a landmark.
    """
    zones: list[list[float]] = []
    start = None
    for slot, value in enumerate(getattr(profile, "brake_mean", [])):
        here = slot * profile.ds
        if value >= 0.05:
            if start is None:
                start = here
        elif start is not None:
            if here - start >= min_m:
                zones.append([round(start), round(here)])
            start = None
    if start is not None and profile.length_m - start >= min_m:
        zones.append([round(start), round(profile.length_m)])
    return zones


def first_lift_m(profile) -> float:
    """Where the driver first comes off the power after the line.

    Until that point the car is running on whatever demand the previous lap
    left it with, because power demand may not be raised under throttle and the
    lap begins with the car already flat. Read off the pooled profile so it is
    where this driver lifts rather than where they lifted once.
    """
    gas = getattr(profile, "gas_mean", None)
    if not gas:
        return 0.0
    for slot, value in enumerate(gas):
        if value < fa26.DEMAND_RESET_GAS:
            return slot * profile.ds
    return 0.0


def commanded_kj(splits, profile, a: float, b: float) -> float:
    """Energy a map asks for between two distances, at the driver's own pace.

    What the car will actually deliver depends on the latch, the taper and the
    store; this is only what the map requests, which is the quantity worth
    reporting when the question is whether it is requesting it somewhere
    useless.
    """
    total = 0.0
    for split in splits:
        if split.deploy_kw <= 0:
            continue
        lo = max(a, float(split.start_m))
        hi = min(b, float(split.deploy_end_m))
        if hi <= lo:
            continue
        d = lo
        while d < hi:
            step = min(profile.ds, hi - d)
            speed = profile.measured_speed_at(d)
            if speed > 1.0 and profile.gas_mean[profile._slot(d)] >= 0.2:
                total += split.deploy_kw * step / speed
            d += step
    return total


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
        #: The one instruction the driver has to act on outside the setup
        #: screen: which of the two landmarks to put STRAT 1 on at. It rides
        #: on every poll rather than inside the summary, because the page
        #: shows it above the maps and the summary is only sent when it
        #: changes.
        self.switch_note = ""
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
                "switch": self.switch_note,
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
            self.switch_note = ""
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
                self.switch_note = ""
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
        paths, laps, rejected = usable_session(
            key, on_lap=lambda f: self.tick(0.2 + 0.8 * f))
        # A map is written in metres along the lap, so a wrong lap length is
        # not a small error -- every zone lands in the wrong place, and
        # `Split.clamped` folds the whole map onto a single point. A Bahrain
        # report came back with every zone reading start 0, end 610, which is
        # a real map clamped to a 610 m lap. Nothing checked, so it reached
        # the setup and the only symptom was a map that did nothing.
        # What the session looked like when this solve read it. A solve takes
        # minutes and the driver is often still on track for them: an Austin
        # run modelled a 1:41.650 while 1:40.204s were landing, and said
        # nothing. Compared again at the end, see `_check_fresh`.
        self.read_revision = session_revision(key)
        self.read_count = len(session_files(key))
        self.read_best = min((lapsession.parse(f)[2] for f in session_files(key)
                              if lapsession.parse(f)), default=0.0)

        if not laps:
            raise RuntimeError(
                "These laps cannot be used: %s. The in-game logger records "
                "distance from the track length it read when it started, so "
                "restarting the session with the logger already enabled "
                "usually fixes it." % (rejected[0][1] if rejected
                                       else "none could be read"))
        for _path, why in rejected:
            self.note("Left out a lap: %s." % why)

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
        # And the same question asked of the car rather than of the clock:
        # which map the deployment in these laps is actually explained by.
        try:
            for line in provenance.describe(
                    laps, paths, baseline_dir(),
                    setupfile.item_index_map(self.car_setup_ini), circuit):
                self.note(line)
        except Exception:
            pass

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
            # "published:2026" is an official figure as much as "fia:06" is;
            # only the notice it came from differs. Austin was being told no
            # official figure existed for it while the table was quoting one.
            where = ("official"
                     if published and (alloc.source.startswith("fia")
                                       or alloc.source.startswith("published"))
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
        # An unnamed curve falls back to Overtake for both. Standard was tried
        # for the race and reverted: it is far too severe for this car. Across
        # every full-throttle sample recorded here, split by the strat actually
        # selected, the race holds 299 kW at 325 km/h where Standard permits
        # 127, and 188 at 335 where it permits 79.
        #
        # The reasoning that put Standard here was circular. Bahrain telemetry
        # showed at most 202 kW above 300 km/h -- but that was the most the map
        # of the day ever asked for, not the most the car would give. Asking
        # the same question of a circuit whose map does demand full power up
        # there answers it properly.
        #
        # The race is not flat to 337 either: it is somewhere near flat to 310
        # and gone by 365. Refining that needs evidence better than a 99th
        # percentile of a differentiated channel, so Overtake stands.
        taper_for = lambda strat: fa26.taper_for(
            alloc.curve_override if strat == 1 else alloc.curve_standard)

        # Article C5.2.8iii: in a race, declared sectors run an alternative
        # curve instead of the Base one -- Baku caps T1-T2, T3-T12 and T15-T16
        # at Alt 1's 250 kW and leaves the straights between them on Base. The
        # race and recharge strategies are raced, so they get it; qualifying
        # does not. A curve the notices have never published numbers for is
        # not guessed at.
        alt_zones = alloc.alt_zones_for("race", lap.length_m)
        alt_known = fa26.taper_is_known(alloc.curve_alternative)
        if alt_zones and alt_known:
            self.detail("Race curve     %s in %d sector%s: %s"
                        % (alloc.curve_alternative, len(alt_zones),
                           "" if len(alt_zones) == 1 else "s",
                           ", ".join("%d-%d m" % z for z in alt_zones)))
        elif alt_zones:
            self.detail("               %s has no published numbers, so its "
                        "sectors keep the Base curve." % alloc.curve_alternative)

        def powertrain_for(strat: int):
            if strat == 1 or not alt_zones or not alt_known:
                return fa26.Powertrain(taper=taper_for(strat))
            return fa26.Powertrain(taper=taper_for(strat),
                                   alt_taper=fa26.taper_for(alloc.curve_alternative),
                                   alt_zones=alt_zones)

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

            # The car's Power and Energy Limits item (ERS_SESSION_LIMITS)
            # defaults to "Current Session", so the cap actually enforced is
            # whichever session is loaded -- practice allows 9.0 MJ where
            # qualifying allows 8.0. Apply writes the notice's figures and
            # leaves that item as the driver set it, so a lap that
            # demonstrably harvested more than the table allows is taken as
            # proof of the real limit.
            cap = 99.0 if unlimited else alloc.mj(session)
            if not unlimited and harvested > cap + 0.2 and strat_no == 1:
                self.note("These laps recovered more than qualifying allows. "
                          "Practice is more generous — set Power and Energy "
                          "Limits to Qualify in the setup, or drive in a "
                          "Qualify session, to match this plan.")
            objective = optimise26.Objective(
                name=label, harvest_cap_mj=cap,
                energy_neutral=neutral, one_shot=one_shot,
                # No floor. A quarter of the battery held back every lap
                # costs a second and buys a reserve the driver already
                # has in STRAT 1 -- see Objective.min_store_fraction.
                min_store_fraction=0.0,
                # A qualifying map assumes the recharge lap left the store
                # full. Arrive at 78% instead, as a real Spa lap did, and a map
                # tuned only for full falls apart faster than one that was not
                # -- so it is scored at three-quarters charge too.
                charge_fractions=(0.75,) if one_shot else ())
            powertrain = powertrain_for(strat_no)
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
                                   profile, zones_for(strat_no), cap)
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
                    # Where the driver is on the brakes, so the strip has
                    # landmarks. Distance alone is an axis that means nothing:
                    # "it deploys nothing after the chicane" is not a thing
                    # you can see until you can see where the chicane is.
                    "braking": braking_zones(profile),
                })

        # Needs both maps, so it waits until all three are solved: the run
        # down to the line is driven on the race map and finished on the
        # qualifying one, and the whole question is where between them to
        # change.
        by_strat = {no: sp for _, no, sp, _ in self.results}
        if 1 in by_strat and 2 in by_strat:
            # It used to be appended to STRAT 1's notes as well. It has its
            # own place above the maps now, and the same sentence in both is
            # one of them not being read.
            self._when_to_switch(
                by_strat[1], by_strat[2], car, cal, profile,
                powertrain_for(1), alloc, lap.length_m)

        self.lap = lap
        try:
            with self.lock:
                kept = list(self.summary)
            history.record(circuit, key, kept, lap.lap_time_s, len(laps),
                           {"qualify": qualify, "race": race,
                            "unlimited": bool(unlimited)})
        except Exception:
            pass                       # a run is worth more than its record
        self._check_fresh(key)
        self.say("Three maps ready. Save them into a setup, then restart "
                 "the session.")

    def _check_fresh(self, key: str) -> None:
        """Say so if the session moved on while this was being worked out.

        The laps are read once, at the start, and a solve runs for minutes.
        Anything driven in the meantime is not in these maps -- and it is
        usually the best of the session, because the driver is still improving.
        An Austin run planned around a 1:41.650 while two 1:40.204s were being
        saved, and reported the 1:41.650 as the lap to beat.
        """
        try:
            now = session_revision(key)
            files = session_files(key)
            best = min((lapsession.parse(f)[2] for f in files
                        if lapsession.parse(f)), default=0.0)
        except OSError:
            return
        if now == getattr(self, "read_revision", None):
            return
        landed = len(files) - getattr(self, "read_count", len(files))
        if best and self.read_best and best < self.read_best - 0.01:
            self.note("You drove a %s while this was working, %.2fs quicker "
                      "than anything it used. These maps do not know about it "
                      "— run it again to plan around your real pace."
                      % (clock(best), self.read_best - best))
        elif landed > 0:
            self.note("%d more %s landed while this was working, and are not "
                      "in these maps. Run it again to include them."
                      % (landed, plural(landed, "lap")))

    #: A lap is "as good as" the best it can manage within this. Below it the
    #: difference is not something a driver would feel or a stopwatch would
    #: separate from an ordinary lap-to-lap wobble.
    APPROACH_TOLERANCE_S = 0.05

    #: Charge levels to price the run to the line at. Finer near the top
    #: than the general sensitivity sweep, because that is where the answer
    #: lives: a lap almost never starts full.
    LINE_LEVELS = (0.6, 0.7, 0.8, 0.9, 1.0)

    #: Allowance a finished map may leave unrecovered before it is worth
    #: saying so. Chosen from what the corpus does: most circuits land within
    #: 0.6 MJ of their cap, and the two that do not -- Austin at 2.36 and Spa
    #: at 2.25 -- are the ones worth a second look.
    UNUSED_ALLOWANCE_MJ = 1.5

    #: Charge a qualifying map is still *predicted* to hold at the line before
    #: it is worth saying so. A one-lap map is meant to arrive empty and a
    #: searched one normally does, so this fires on a search that could not
    #: place the energy rather than on an ordinary map.
    #:
    #: It would not have caught the Austin map that started this. That one was
    #: predicted to finish empty and finished on 74%, because the model was
    #: wrong about where the energy went, and a check on the model's own
    #: prediction cannot catch the model being wrong. `OPENING_SHARE` is the
    #: one that catches that.
    QUALI_LEFTOVER_PCT = 10.0

    #: Share of a map's commanded deployment that may sit before the driver's
    #: first lift of the lap before it is worth saying so.
    #:
    #: The car crosses the line already at full throttle, and power demand may
    #: not be raised under throttle, so the opening command is refused until
    #: the driver first comes off the power. Measured, not assumed: every one
    #: of seven Austin laps deployed exactly zero before the first lift, and
    #: 160 of 216 laps across the whole corpus deployed under 50 kJ there. The
    #: simulation does not model this -- it primes the latch from the tail of
    #: the same map, which is not the map anyone arrives on -- so the energy
    #: has to be counted and flagged rather than simulated away.
    #:
    #: A tenth, because both Austin maps waste energy there and only one of
    #: them is dramatic about it: map A commands 2.30 MJ before the first lift
    #: and map B 1.53 MJ, 19% and 11% of what each spends. Set at 12% it
    #: caught the bad one and let a megajoule and a half through on the other.
    OPENING_SHARE = 0.10

    #: How far before the line to consider putting STRAT 1 on, and how finely.
    #: Six hundred metres is the whole run out of any final corner; beyond it
    #: the driver is on the previous lap's business.


    def _when_to_switch(self, quali, race, car, cal, profile, powertrain,
                        alloc, length_m) -> list[str]:
        """When to put STRAT 1 on, in the two places a driver can act on.

        Deploy out of the last corner and the lap starts fast and light; wait
        and it starts slow and full. Those are not two effects to be weighed
        against each other, they are one decision, and the simulator used to
        take entry speed and entry charge as independent inputs -- which is
        why the study built to work around that mispredicted by a factor of
        eighteen at Albert Park and in the wrong direction at Monza.

        `sim26.run_to_line` integrates the approach under whichever map is live
        on it, so the speed and the charge that come out are both consequences
        of where the switch happened.

        What comes back is one of two instructions, because those are the only
        two a driver can actually follow. Metres are not: nobody judges 340 m
        at 280 kph with a wheel in their hands, and the answer used to be given
        as "about 340 m before the line -- roughly 4.2s out", which is a number
        to read afterwards rather than something to do. The last braking zone
        is a landmark you cannot miss, and the line is painted on the road, so
        the question becomes which of those two to flip at. Both are simulated
        and the quicker one is named. The answer is not the same everywhere:
        Monza and Suzuka want it before the last corner, Madrid and the Red
        Bull Ring want it at the line and lose a tenth for every early flip.
        """
        cap_kj = alloc.mj("qualify") * 1000.0
        zones = dict(reset_zones=alloc.reset_zones_for("qualify", length_m),
                     speed_zones=alloc.speed_zones_for(length_m))

        def lap_from(switch_m):
            # The approach has to reach back past the switch or the map never
            # changes inside it, and a long run to the line -- Monza's, out of
            # Parabolica -- is further from it than the default window.
            state = sim26.run_to_line(
                race, quali, switch_m, car, cal, profile, powertrain,
                start_store_kj=laplib.STORE_CAPACITY_KJ,
                harvest_cap_kj=cap_kj,
                approach_m=max(sim26.PRIME_M, switch_m + 50.0), **zones)
            result = sim26.simulate(
                quali, car, cal, profile, powertrain,
                start_store_kj=state.store_kj, harvest_cap_kj=cap_kj,
                settle=False, entry=state, **zones)
            return state, result.lap_time_s

        # The two places a driver can flip the switch without looking at a
        # number: as the line goes under the car, or on the way into the last
        # braking zone of the lap. `braking_zones` reads the pooled profile,
        # so the last one is where this driver actually brakes for the final
        # corner rather than where the track map says it is.
        # Not `zones`: that name is already the reset and speed zones every
        # simulation here is run with, and `lap_from` closes over it.
        braking = [z for z in braking_zones(profile) if z[1] < length_m - 30.0]
        corner_m = (length_m - braking[-1][0]) if braking else None

        try:
            at_line_state, at_line = lap_from(0.0)
            if corner_m is None:
                return []
            corner_state, before_corner = lap_from(corner_m)
        except Exception:
            return []

        gained = at_line - before_corner
        if gained > self.APPROACH_TOLERANCE_S:
            where, state, lap_s = "before the final corner", corner_state, before_corner
            other = at_line
        else:
            where, state, lap_s = ("while crossing the start/finish line",
                                   at_line_state, at_line)
            other = before_corner

        cost = abs(other - lap_s)
        note = ("Switch to STRAT 1 %s." % where)
        if cost >= self.APPROACH_TOLERANCE_S:
            note += (" The other one costs %.2fs." % cost)
        else:
            note += " Either works here — they are within %.2fs." % cost
        note += (" You will start the lap at %.0f kph on %.0f%% battery."
                 % (state.speed_ms * 3.6,
                    100 * state.store_kj / laplib.STORE_CAPACITY_KJ))

        with self.lock:
            self.switch_note = note
        return [note]

    def _describe(self, label, strat_no, result, splits, lap, cal, profile,
                  reset_zones, cap: float = 99.0) -> list[str]:
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
        # A recharge lap refuses harvest once it is full, which is the point
        # of it, not a fault to report.
        #
        # And refused is not the same as wasted. Once the lap has recovered
        # everything the per-lap allowance permits, the rest could not have
        # been used however early it was spent -- a Monaco race map harvesting
        # 8.50 against a cap of 8.50 refused 0.6 MJ it had no room for and no
        # right to, and telling the user to make room for it was advice that
        # could not be taken. What is worth reporting is only the part the
        # allowance would still have allowed.
        headroom = max(0.0, cap - result.harvested_mj)
        recoverable = min(result.refused_mj, headroom)
        if recoverable > 0.1 and strat_no != 3:
            notes.append(
                "%.1f MJ of recovery was lost to a full battery. Using more "
                "earlier in the lap would make room." % recoverable)
            self.note("   " + notes[-1])
        elif result.refused_mj > 0.1 and strat_no != 3:
            self.detail("               %.1f MJ refused by a full store, but "
                        "the lap is already at its %.1f MJ allowance, so there "
                        "was nothing to be gained by making room."
                        % (result.refused_mj, cap))
        # Allowance left on the table, which is a different failure from
        # recovery being refused: here the store had room and the lap simply
        # never went and got it. A Spa race map came back deploying 6.25 MJ
        # against an 8.5 MJ allowance while refusing 1.26 -- 2.25 MJ of
        # permitted energy that was neither used nor recovered. It is a search
        # result worth doubting rather than a fault in the car, so it is said
        # plainly and not dressed up as advice.
        # A race map that runs the store to nothing is the fastest one and is
        # what the regulations intend -- drivers are expected to use the whole
        # battery every lap -- but it is worth saying out loud, because the
        # driver who reaches for an overtake at the wrong moment will find
        # nothing there.
        if strat_no == 2 and result.store_min_kj < laplib.STORE_CAPACITY_KJ * 0.08:
            notes.append(
                "This map runs the battery down to %.0f%% at its lowest. That "
                "is where the lap time is, but there is nothing spare for an "
                "attack at that point — switch to STRAT 1 for that."
                % (100 * result.store_min_kj / laplib.STORE_CAPACITY_KJ))
            self.note("   " + notes[-1])

        # A qualifying map exists to empty the battery over one lap, and one
        # that crosses the line still holding charge has failed at the only
        # thing it was asked to do. This is not hypothetical: the map the
        # search wrote for Austin finished the lap on 74%, three megajoules
        # unspent, and nothing here said a word about it -- it was found by
        # driving the thing and reading the telemetry afterwards.
        #
        # The store is reported as a fraction of capacity rather than against
        # the allowance, because this is about the battery the lap had in its
        # hands and did not use, not about recovery it was never granted.
        opening = first_lift_m(profile)
        if opening > 0:
            before = commanded_kj(splits, profile, 0.0, opening)
            total = commanded_kj(splits, profile, 0.0, lap.length_m)
            if total > 0 and before / total >= self.OPENING_SHARE:
                notes.append(
                    "%.1f MJ of this map — %.0f%% of everything it spends — is "
                    "commanded before the first corner, and the car will refuse "
                    "most of it. You cross the line flat out, and the power it "
                    "may ask for cannot go up until you lift, so the opening "
                    "stretch runs on whatever the lap before it left behind. "
                    "Move that energy later in the lap."
                    % (before / 1000.0, 100.0 * before / total))
                self.note("   " + notes[-1])

        left = 100.0 * result.store_end_kj / laplib.STORE_CAPACITY_KJ
        if strat_no == 1 and left >= self.QUALI_LEFTOVER_PCT:
            notes.append(
                "This map finishes the lap still holding %.0f%% of the battery "
                "— about %.1f MJ it never spent. A qualifying map should arrive "
                "at the line empty, so either the search could not find "
                "anywhere to put it, or somewhere it did put it is not "
                "delivering. Worth re-running before you trust it."
                % (left, result.store_end_kj / 1000.0))
            self.note("   " + notes[-1])

        unused = cap - result.harvested_mj
        if (strat_no != 3 and cap < 90.0
                and unused > self.UNUSED_ALLOWANCE_MJ and recoverable <= 0.1):
            notes.append(
                "This map recovers %.1f MJ of the %.1f MJ you are allowed, "
                "so %.1f MJ goes unused. That may be all this circuit offers, "
                "or the search may have settled early — worth re-running."
                % (result.harvested_mj, cap, unused))
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
            telemetry_dir=SNAPSHOT_DIR or baseline_dir())

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
                "active": DEFAULT_ACTIVE,
                "limits": ([] if not write_alloc or self.unlimited
                           else recharge.menu_rows(self.alloc)),
                "limits_from": ("notice" if self.alloc.source.startswith("fia:")
                                else "default" if self.alloc.is_baseline
                                else "yours"),
                "unlimited": bool(write_alloc and self.unlimited)}

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
LIVE = LiveSearch()


# --------------------------------------------------------------------- server
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
        ".ico": "image/x-icon", ".woff2": "font/woff2", ".txt": "text/plain"}


#: Whether the page has ever actually been handed to a window. The launcher
#: has no other way to tell a window that was used and closed from one that
#: was never really ours -- see the end of `main`.
SERVED = {"pages": 0, "last": 0.0}

#: An Edge process that exits sooner than this held no window anyone used: it
#: handed the window to another Edge process and left. Two players saw it --
#: one a "127.0.0.1 refused to connect" page, one the app reading "Cannot reach
#: the optimizer" -- because the launcher took the exit for the window closing
#: and shut the server down under a window that was still open.
HANDOFF_S = 15.0
#: How long to wait for the window to load the page at all.
FIRST_PAGE_S = 30.0
#: How long the page may go quiet before it is taken to be closed. It asks
#: every 4 s, but Chromium throttles a minimised window's timers to once a
#: minute, so this has to be well clear of 60 s.
IDLE_S = 150.0


def wait_while_watched(first_page_s: float = FIRST_PAGE_S,
                       idle_s: float = IDLE_S) -> bool:
    """Keep serving while a page keeps asking. False if none ever loaded.

    The page is the only thing that knows whether the window is still open:
    which process Edge keeps it in is Edge's business, and changes with an
    update pending, Startup boost, or nothing anyone can see.
    """
    start = time.monotonic()
    while True:
        time.sleep(1.0)
        now = time.monotonic()
        if not SERVED["pages"]:
            if now - start > first_page_s:
                return False
            continue
        if now - SERVED["last"] > idle_s:
            return True


class Handler(BaseHTTPRequestHandler):
    server_version = "FA26Optimiser"

    def log_message(self, *args) -> None:  # noqa: D102 - quiet console
        pass

    # ----------------------------------------------------------------- send
    def _json(self, payload, code: int = 200) -> None:
        # Never NaN or Infinity: Python writes them, no browser reads them,
        # and the whole response is lost. One bad number in a player's lap
        # file made the page say "Cannot reach the optimizer" while the
        # optimizer sat there answering.
        body = json.dumps(_finite(payload), allow_nan=False).encode("utf-8")
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

    def _local_only(self) -> bool:
        """Whether this request really came from our own window.

        A server on loopback is reachable by any page the user happens to
        have open: the browser sends the request whichever site asked for
        it. Two things already blunt that -- no CORS header, so nothing can
        read an answer, and a JSON content type on every call that changes
        anything, which forces a preflight this server does not answer. What
        neither covers is a domain that resolves to 127.0.0.1: to the
        browser that is same-origin, and both defences fall away.

        The attack needs the browser to send the attacker's own name in the
        Host header, so checking it is what closes the hole. Nothing
        legitimate reaches this server under any other name.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host.strip("[]").lower() not in ("127.0.0.1", "localhost",
                                            "::1", ""):
            return False
        origin = self.headers.get("Origin")
        if origin:
            where = (urlparse(origin).hostname or "").lower()
            if where not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    # ------------------------------------------------------------------ get
    def do_GET(self) -> None:
        if not self._local_only():
            self.send_error(403)
            return
        SERVED["last"] = time.monotonic()
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
            elif route == "/api/live":
                key = query.get("key", [""])[0] or newest_key()
                if not key:
                    self._json({"ready": False, "reason": "no session yet"})
                else:
                    out = live_estimate(key)
                    LIVE.note(key, session_revision(key))
                    out["full"] = LIVE.snapshot(key)
                    out["live_search"] = install.live_search_enabled()
                    self._json(out)
            elif route == "/api/history":
                self._json({"runs": history.load()})
            elif route == "/api/compare":
                found = history.compare(query.get("a", [""])[0],
                                        query.get("b", [""])[0])
                if found is None:
                    self._json({"error": "one of those runs is gone"}, code=404)
                else:
                    self._json(found)
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
        if not self._local_only():
            self.send_error(403)
            return
        SERVED["last"] = time.monotonic()
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
            elif route == "/api/livesearch":
                install.save_live_search(bool(body.get("on")))
                self._json({"live_search": install.live_search_enabled()})
            elif route == "/api/setpath":
                folder = Path(body.get("path", "").strip().strip('"'))
                if not install.is_ac_root(folder):
                    raise RuntimeError(
                        "That folder has no content\\cars in it, so it is not "
                        "an Assetto Corsa install.")
                install.save_root(folder)
                _CAR_LIMITS_RESET()
                self._json(readiness())
            elif route == "/api/pickfolder":
                titles = {
                    "ac": "Where is Assetto Corsa?",
                    "documents": "Where is the Assetto Corsa documents folder?",
                    "telemetry": "Where does the logger write your laps?",
                }
                which = body.get("which") or "ac"
                chosen = pick_folder(titles.get(which, "Choose a folder"),
                                     body.get("start") or "")
                self._json({"path": chosen})
            elif route == "/api/setdocuments":
                folder = (body.get("path") or "").strip().strip('"')
                if folder and not Path(folder).is_dir():
                    raise RuntimeError("There is no folder at %s." % folder)
                install.save_documents(folder or None)
                _DETAIL_CACHE.clear()
                self._json(readiness())
            elif route == "/api/settelemetry":
                folder = (body.get("path") or "").strip().strip('"')
                if folder and not Path(folder).is_dir():
                    raise RuntimeError("There is no folder at %s." % folder)
                install.save_telemetry(folder or None)
                _DETAIL_CACHE.clear()
                self._json(readiness())
            elif route == "/api/installlogger":
                # `force` is the page saying the user has read the warning.
                # Without it this refuses while the game is running, because
                # CSP reloads a Lua app whose file changes and a reload throws
                # away the lap being recorded.
                install.install_logger(force=bool(body.get("force")))
                self._json(readiness())
            elif route == "/api/cancel":
                self._json({"stopping": ENGINE.cancel()})
            elif route == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown,
                                 daemon=True).start()
            else:
                self.send_error(404)
        except PermissionError as exc:
            # "[WinError 5] Access is denied" says nothing a driver can act on.
            where = Path(exc.filename).name if exc.filename else "the setup"
            self._json({"error": "Windows would not let this app save %s. %s"
                                 % (where, BLOCKED_ADVICE),
                        "trace": traceback.format_exc()}, code=500)
        except Exception as exc:
            self._json({"error": str(exc),
                        "trace": traceback.format_exc()}, code=500)


class Server(ThreadingHTTPServer):
    """The local server, refusing to share a port with anything else.

    ThreadingHTTPServer sets SO_REUSEADDR, which on Windows lets a second
    process bind a port another process is already listening on -- and then
    the two of them split the incoming connections arbitrarily. A second copy
    of this app would silently answer some of the first copy's requests, and
    an old build left running would answer for a new one. That is not a
    hypothetical: it is how a test here spent an afternoon proving the build
    shipped a logger it did not ship, by talking to a stale instance of an
    older build that was still holding the port.

    Refusing the bind instead is what makes free_port's fallback do its job.
    """

    allow_reuse_address = False


#: BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE -- real folders only, and the
#: resizable dialog with a text box rather than the 1995 one.
_BIF_FLAGS = 0x00000001 | 0x00000040


def app_window() -> int:
    """The application's own window, so a dialog opens in front of it.

    Without an owner the picker is a top-level window of its own and Windows
    is free to leave it behind the page that asked for it -- which looks
    exactly like the button having done nothing.
    """
    found = []

    def visit(hwnd, _):
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
            if "FA26 ERS Deployment Optimizer" in buffer.value:
                if ctypes.windll.user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
                    return False
        return True

    try:
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                  ctypes.c_void_p)(visit)
        ctypes.windll.user32.EnumWindows(proc, 0)
    except Exception:
        return 0
    return found[0] if found else 0


def pick_folder(title: str, start: str = "") -> str:
    """Show the folder picker and return what was chosen, or an empty string.

    Runs on whichever thread serves the request, which is one of the server's
    own -- the dialog is modal to itself, not to the application, so the page
    carries on and a cancelled dialog simply returns nothing.
    """
    try:
        shell = ctypes.windll.shell32
        ole = ctypes.windll.ole32

        class BROWSEINFO(ctypes.Structure):
            _fields_ = [("hwndOwner", ctypes.c_void_p),
                        ("pidlRoot", ctypes.c_void_p),
                        ("pszDisplayName", ctypes.c_wchar_p),
                        ("lpszTitle", ctypes.c_wchar_p),
                        ("ulFlags", ctypes.c_uint),
                        ("lpfn", ctypes.c_void_p),
                        ("lParam", ctypes.c_void_p),
                        ("iImage", ctypes.c_int)]

        # BFFM_INITIALIZED = 1, BFFM_SETSELECTIONW = 1127: open where the
        # folder already is, so "change this" starts from the current answer
        # rather than from the desktop.
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                       ctypes.c_uint, ctypes.c_void_p,
                                       ctypes.c_void_p)

        def on_message(hwnd, message, lparam, data):
            if message == 1 and start:
                ctypes.windll.user32.SendMessageW(
                    hwnd, 1127, 1, ctypes.c_wchar_p(start))
            return 0

        callback = proc_type(on_message)
        ole.CoInitialize(None)
        buffer = ctypes.create_unicode_buffer(1024)
        info = BROWSEINFO()
        info.hwndOwner = ctypes.c_void_p(app_window() or 0)
        info.pszDisplayName = ctypes.cast(buffer, ctypes.c_wchar_p)
        info.lpszTitle = title
        info.ulFlags = _BIF_FLAGS
        info.lpfn = ctypes.cast(callback, ctypes.c_void_p)
        pidl = shell.SHBrowseForFolderW(ctypes.byref(info))
        if not pidl:
            return ""
        path = ctypes.create_unicode_buffer(1024)
        ok = shell.SHGetPathFromIDListW(ctypes.c_void_p(pidl), path)
        ole.CoTaskMemFree(ctypes.c_void_p(pidl))
        return path.value if ok else ""
    except Exception:
        return ""


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


#: Chromium switches that turn off everything that talks to anything: cast
#: discovery, component updates, crash reporting, reachability pings,
#: phishing lookups and translation. Measured on a real run, an unhardened
#: Edge window opened connections to four devices on the local network on
#: port 8009 -- Chromecast discovery, and not something a tool that reads your
#: lap times has any business doing. Nothing here changes how the page renders.
QUIET_SWITCHES = [
    "--disable-background-networking",
    "--disable-sync",
    "--disable-features=MediaRouter,Translate,OptimizationHints",
    "--disable-component-update",
    "--disable-domain-reliability",
    "--disable-breakpad",
    "--disable-client-side-phishing-detection",
    "--no-pings",
    "--no-service-autorun",
]

#: The WebView2 runtime's registry keys, stable, beta, dev and canary -- the
#: ones pywebview reads. Without one pywebview falls back to the Internet
#: Explorer engine, which cannot run this page at all, so it is checked here
#: first rather than discovered as a blank window.
_WEBVIEW2_KEYS = ("{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                  "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
                  "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",
                  "{65C35B14-6C1D-4122-AC46-7148CC9D6497}")


def webview2_version() -> str:
    """The installed WebView2 runtime's version, or "" if there is none."""
    try:
        import winreg
    except ImportError:
        return ""
    for hive, prefix in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node"),
                         (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE"),
                         (winreg.HKEY_CURRENT_USER, r"SOFTWARE")):
        for key in _WEBVIEW2_KEYS:
            try:
                with winreg.OpenKey(hive, prefix + r"\Microsoft\EdgeUpdate"
                                    r"\Clients\%s" % key) as handle:
                    version = str(winreg.QueryValueEx(handle, "pv")[0])
            except OSError:
                continue
            try:
                if int(version.split(".")[0]) >= 86:     # pywebview's minimum
                    return version
            except ValueError:
                continue
    return ""


def native_window(url: str) -> bool:
    """The UI in a window of its own. False if this machine cannot show one.

    Spotify, Discord and Claude carry a copy of Chromium inside them, which is
    why nobody sees a browser. Windows ships one for exactly this -- WebView2,
    part of Windows 11 and of Windows 10 through its updates -- and pywebview
    puts it in a real window: this program's title, icon and taskbar button,
    and no Edge processes to hand the window between. The Edge window it
    replaces closed under two players when Edge did just that.
    """
    if not webview2_version():
        return False
    try:
        import webview
    except ImportError:
        return False
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                          " ".join(QUIET_SWITCHES))
    webview.create_window("FA26 ERS Deployment Optimizer", url,
                          width=1440, height=940, min_size=(900, 620),
                          background_color="#0b0e14")
    webview.start(gui="edgechromium")
    return True


def open_window(url: str, prefer_browser: bool = False) -> bool:
    """Show the UI in its own window and block until that window is closed.

    True when the return means the window really is closed -- a native window
    cannot be handed anywhere. False when it may not: an Edge process that
    exits can have passed its window on, and a tab never says at all.

    Three ways down, best first. A native window on WebView2, shipped with the
    build since 1.1.1. Without WebView2, a Chromium browser in app mode, which
    behaves like a window even though it is not one -- own taskbar button, no
    address bar and no tabs. Only if neither exists does this fall back to a
    tab, which `main` keeps serving for as long as the page keeps asking.
    """
    if not prefer_browser:
        try:
            if native_window(url):
                return True
        except Exception:
            # A window that would not start leaves the app mode one to try,
            # which is better than a program that appears to do nothing.
            pass

        found = find_browser()
        if found:
            exe, _name = found
            profile = window_profile()
            profile.mkdir(parents=True, exist_ok=True)
            # The window is Edge, so Edge's own habits become this program's
            # behaviour as far as anyone watching the machine is concerned --
            # hence the same quiet switches the native window gets.
            subprocess.Popen([
                exe,
                "--app=" + url,
                "--user-data-dir=" + str(profile),
                "--window-size=1440,940",
                "--no-first-run",
                "--no-default-browser-check",
            ] + QUIET_SWITCHES).wait()
            return False

    # A tab says nothing when it closes; `main` keeps serving while the page
    # keeps asking, and stops a couple of minutes after it goes quiet.
    webbrowser.open(url)
    print("No app window was available, so this opened in your browser.")
    print("It closes by itself a couple of minutes after you close the tab.")
    return False


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
        try:
            server = Server(("127.0.0.1", port), Handler)
        except OSError:
            # `free_port` asks the system for a free port, lets go of it, and
            # then this binds it -- so two copies started together can both be
            # handed the same one and the second fails. Someone double-clicking
            # the icon twice should not get an error message about sockets, so
            # take whatever port is actually free instead.
            if wanted:
                raise
            server = Server(("127.0.0.1", 0), Handler)
            port = server.server_address[1]
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
            opened = time.monotonic()
            closed = open_window(url, prefer_browser="--browser" in args)
            # Back this soon from Edge or a tab, the process that returned
            # held no window anyone used: Edge passed it to another of its
            # processes, or a tab never reports closing at all. Either way the
            # window may well still be open, so it is the page that says when
            # to stop -- it asks every few seconds while it is there. A native
            # window that has returned is closed, however soon.
            if closed:
                pass
            elif time.monotonic() - opened < HANDOFF_S:
                if not wait_while_watched():
                    fatal("The window never loaded the page.\n\n"
                          "Close any window titled \"FA26 ERS Deployment "
                          "Optimizer\" and start the application again.")
                    return 1
            elif not SERVED["pages"]:
                fatal("The window closed without ever showing the page.\n\n"
                      "Close any window titled \"FA26 ERS Deployment "
                      "Optimizer\" and start the application again.")
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
