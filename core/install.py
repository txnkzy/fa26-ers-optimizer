"""Find Assetto Corsa, and unpack the car's data from the user's own copy.

Two things were stopping this tool from being handed to anyone else.

The install path was a constant pointing at one machine's Steam library, so
nobody else's install was ever found. Steam does not put every game under its
own folder -- on the machine this was written on, Steam is on C: and Assetto
Corsa is on D: -- so the library list has to be read rather than guessed.

And the car's physics shipped with the tool: 380 files, 2.6 MB, unpacked out of
VRC's `data.acd`. That is their paid work, and passing it around inside this
project would be redistributing it. Nothing of theirs is shipped now; the data
is unpacked from the copy the user already owns, into a cache outside the
project, the first time the app runs.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths

#: Assetto Corsa on Steam.
APP_ID = "244210"

#: Files `carphysics.load` and the setup writer need out of `data.acd`. The
#: rest of the archive is unpacked too -- it costs nothing and the cache is
#: outside the project -- but these are the ones whose absence is a problem
#: worth naming.
REQUIRED = ("car.ini", "engine.ini", "drivetrain.ini", "tyres.ini", "setup.ini")


def app_dir() -> Path:
    """Where this tool keeps things that are not part of the project."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "FA26 Optimizer"


def _config_path() -> Path:
    return app_dir() / "install.json"


def _config() -> dict:
    try:
        raw = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_config(**changes) -> None:
    """Merge, never overwrite: there is more than one setting in here now."""
    data = _config()
    data.update(changes)
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True),
                    encoding="utf-8")


def saved_root() -> Path | None:
    """An Assetto Corsa folder the user pointed us at by hand."""
    root = _config().get("assetto_corsa")
    return Path(root) if root and is_ac_root(Path(root)) else None


def save_root(root: str | Path) -> None:
    _write_config(assetto_corsa=str(root))


def saved_documents() -> Path | None:
    """An Assetto Corsa documents folder the user pointed us at by hand.

    This is the one holding `setups` and, normally, the recorded laps --
    not the game install.
    """
    folder = _config().get("documents")
    return Path(folder) if folder else None


def save_documents(folder: str | Path | None) -> None:
    _write_config(documents=str(folder) if folder else "")


def saved_telemetry() -> Path | None:
    """A recorded-laps folder the user pointed us at by hand.

    Deliberately not checked for existence: it is set before the first lap
    is driven as often as after, and second-guessing a path somebody typed
    on purpose is how they end up back where they started with nothing on
    screen to explain it.
    """
    folder = _config().get("telemetry")
    return Path(folder) if folder else None


def save_telemetry(folder: str | Path | None) -> None:
    """Set the folder, or pass nothing to go back to finding it."""
    _write_config(telemetry=str(folder) if folder else "")


def is_ac_root(path: Path) -> bool:
    """Whether this folder really is an Assetto Corsa install."""
    return (path / "content" / "cars").is_dir()


def _steam_dirs() -> list[Path]:
    """Every Steam library on this machine, from Steam's own list.

    Steam records its libraries in `libraryfolders.vdf`, which lives beside
    the install rather than inside each library, so this finds drives the
    registry alone never points at.
    """
    roots: list[Path] = []
    try:
        import winreg
        for hive, key, name in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    roots.append(Path(winreg.QueryValueEx(handle, name)[0]))
            except OSError:
                continue
    except ImportError:
        pass
    for guess in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        roots.append(Path(guess))

    libraries: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        libraries.append(root)
        for rel in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
            vdf = root / rel
            if not vdf.is_file():
                continue
            try:
                text = vdf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for found in re.findall(r'"path"\s*"([^"]+)"', text):
                libraries.append(Path(found.replace("\\\\", "\\")))
    return libraries


def find_root() -> Path | None:
    """The user's Assetto Corsa folder, or None if it cannot be found."""
    saved = saved_root()
    if saved:
        return saved
    seen: set = set()
    for library in _steam_dirs():
        candidate = library / "steamapps" / "common" / "assettocorsa"
        if candidate in seen:
            continue
        seen.add(candidate)
        if is_ac_root(candidate):
            return candidate
    for guess in (r"C:\Program Files (x86)\Steam\steamapps\common\assettocorsa",
                  r"D:\SteamLibrary\steamapps\common\assettocorsa",
                  r"C:\Games\assettocorsa"):
        path = Path(guess)
        if is_ac_root(path):
            return path
    return None


def car_folder(car_id: str, root: Path | None = None) -> Path | None:
    root = root or find_root()
    if root is None:
        return None
    folder = root / "content" / "cars" / car_id
    return folder if folder.is_dir() else None


class MissingCarData(Exception):
    """Raised when the car's own files cannot be reached.

    Carries a sentence fit to show someone who has never seen this tool
    before, and a `kind` the front end can branch on.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def cache_dir(car_id: str) -> Path:
    return app_dir() / "cardata" / car_id


def _stamp(acd: Path) -> str:
    info = acd.stat()
    return "%d:%d" % (info.st_size, int(info.st_mtime))


def ensure_unpacked(car_id: str, root: Path | None = None,
                    force: bool = False) -> Path:
    """The car's data files on disk, unpacking them first if need be.

    Unpacked once and cached, keyed on the size and date of the `data.acd` it
    came from, so a mod update is picked up and an unchanged install costs a
    single `stat`. The cache lives under LOCALAPPDATA, not in the project, so
    zipping this folder up for someone else cannot carry VRC's files with it.
    """
    root = root or find_root()
    if root is None:
        raise MissingCarData(
            "no_ac",
            "Assetto Corsa could not be found on this computer. Point the "
            "optimizer at the folder that contains content\\cars.")
    folder = root / "content" / "cars" / car_id
    if not folder.is_dir():
        raise MissingCarData(
            "no_car",
            "The VRC Formula Alpha 2026 is not installed in %s. This tool "
            "only works with that car." % root)
    acd = folder / "data.acd"
    loose = folder / "data"
    target = cache_dir(car_id)
    marker = target / ".source"

    if acd.is_file():
        want = _stamp(acd)
        if not force and marker.is_file():
            try:
                if marker.read_text(encoding="utf-8").strip() == want:
                    return target
            except OSError:
                pass
        import acd as acdlib
        try:
            files = acdlib.unpack(acd, car_id)
        except Exception as exc:
            raise MissingCarData(
                "unreadable",
                "The car's data.acd could not be read (%s). If the car was "
                "updated recently, verifying the files in Content Manager "
                "usually fixes it." % exc)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        for name, blob in files.items():
            (target / name).write_bytes(blob)
        marker.write_text(want, encoding="utf-8")
    elif loose.is_dir():
        # Some installs keep the data unpacked already.
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(loose, target)
        marker.write_text("loose", encoding="utf-8")
    else:
        raise MissingCarData(
            "no_data",
            "No data.acd in %s, so the car's physics cannot be read." % folder)

    missing = [name for name in REQUIRED if not (target / name).is_file()]
    if missing:
        raise MissingCarData(
            "incomplete",
            "The car's data unpacked without %s, which this tool needs."
            % ", ".join(missing))
    return target


#: The logger that records the laps. A CSP Lua app, so `apps/lua`, not the
#: `apps/python` folder the stock game uses.
LOGGER_NAME = "fa26_baseline"


def logger_source() -> Path:
    return paths.resource_root() / "logger" / LOGGER_NAME


def logger_target(root: Path | None = None) -> Path | None:
    root = root or find_root()
    return None if root is None else root / "apps" / "lua" / LOGGER_NAME


def logger_installed(root: Path | None = None) -> bool:
    """Whether the in-game logger is in place.

    Without it there is never any telemetry, and the app would sit on
    "Waiting for laps" forever with nothing to say why.
    """
    target = logger_target(root)
    return bool(target and (target / "manifest.ini").is_file())


def logger_current(root: Path | None = None) -> bool:
    """Whether the installed logger is the one this build ships.

    Compared by content rather than by a version number in the manifest: the
    number is one more thing to remember to change, and getting it wrong here
    means a fix silently not reaching the people who need it.
    """
    target = logger_target(root)
    source = logger_source()
    if target is None or not target.is_dir() or not source.is_dir():
        return True
    try:
        for item in source.iterdir():
            if not item.is_file():
                continue
            mirror = target / item.name
            if not mirror.is_file():
                return False
            if mirror.read_bytes() != item.read_bytes():
                return False
    except OSError:
        return False
    return True


def install_logger(root: Path | None = None) -> Path:
    """Copy the logger into the user's Assetto Corsa.

    Two files into a folder of its own; it overwrites nothing else and is the
    one step a new user would otherwise have to be talked through.
    """
    root = root or find_root()
    if root is None:
        raise MissingCarData(
            "no_ac", "Assetto Corsa could not be found on this computer.")
    source = logger_source()
    if not source.is_dir():
        raise MissingCarData(
            "no_logger_source",
            "The logger is missing from this download (expected %s)." % source)
    target = logger_target(root)
    try:
        target.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            if item.is_file():
                shutil.copy2(item, target / item.name)
    except OSError as exc:
        # Rare, but this is the one step nobody can skip, and Windows reports
        # it as "Access is denied" -- true, and no use to anyone. Note that a
        # normal Steam install is NOT the cause: Steam grants Users full
        # control over its own folder, which is how it updates itself without
        # asking. The likely causes are the game holding the file open, or an
        # install placed somewhere by hand.
        #
        # Telling people to run an unsigned executable as administrator is not
        # an answer -- it is the exact thing a cautious user should refuse. So
        # the logger is put somewhere reachable instead, and they can move it
        # across in Explorer, which asks for whatever it needs on its own.
        spare = None
        try:
            spare = app_dir() / "logger" / LOGGER_NAME
            spare.mkdir(parents=True, exist_ok=True)
            for item in source.iterdir():
                if item.is_file():
                    shutil.copy2(item, spare / item.name)
        except OSError:
            spare = None
        raise MissingCarData(
            "logger_denied",
            "The logger could not be copied into %s (%s). If Assetto Corsa "
            "is open, close it and try again.%s"
            % (target, exc.strerror or exc,
               "" if spare is None else
               " Otherwise there is a copy in %s -- move the %s folder from "
               "there into the apps\\lua folder above, and Windows will ask "
               "for anything it needs." % (spare.parent, LOGGER_NAME))
            ) from exc
    return target


# ------------------------------------------------------------- is it running
#: The sim itself. Not the launcher and not Content Manager: those can sit
#: open for hours without a car ever being on track.
GAME_PROCESSES = ("acs.exe", "acs_x64.exe")

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_PATH = 260
#: FILETIME counts 100 ns intervals from 1601-01-01.
_FILETIME_EPOCH = 11644473600


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * _MAX_PATH),
    ]


def _kernel32():
    try:
        return ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError):
        return None


def _find_pid(names: tuple) -> int | None:
    k32 = _kernel32()
    if k32 is None:
        return None
    want = {n.lower() for n in names}
    snapshot = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return None
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(_ProcessEntry)
        ok = k32.Process32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.decode("latin-1", "replace").lower() in want:
                return int(entry.th32ProcessID)
            ok = k32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return None


def _started_at(pid: int) -> float | None:
    k32 = _kernel32()
    if k32 is None:
        return None
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        spare = (wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME())
        if not k32.GetProcessTimes(handle, ctypes.byref(created),
                                   ctypes.byref(spare[0]),
                                   ctypes.byref(spare[1]),
                                   ctypes.byref(spare[2])):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 1e7 - _FILETIME_EPOCH
    finally:
        k32.CloseHandle(handle)


def game_running() -> dict:
    """Whether the sim is on, and when it started.

    The start time is what makes "this session" mean anything. Without it the
    app called the newest folder on disk live, so laps driven hours ago and a
    reboot later still showed a green light -- which is exactly the state the
    map-order guard exists to catch, being announced as current.

    Costs about five milliseconds, so the page can ask on every poll.
    """
    pid = _find_pid(GAME_PROCESSES)
    if pid is None:
        return {"running": False, "since": None, "for_s": 0}
    since = _started_at(pid)
    return {"running": True, "since": since,
            "for_s": round(time.time() - since) if since else 0}
