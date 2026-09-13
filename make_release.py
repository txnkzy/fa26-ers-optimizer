"""Build a zip fit to hand to someone else.

    python make_release.py

Writes `FA26_Optimizer_<date>.zip` beside this file containing only what this
project actually wrote. Three kinds of thing are deliberately left out:

* **VRC's car data.** The physics used to live in `out/fa26_unpacked` -- 380
  files unpacked from their `data.acd`. That is their paid work. The app now
  unpacks it from the copy each user already owns, so it never needs to
  travel.
* **Your telemetry and your setups.** Lap CSVs, map snapshots and `.bak`
  copies are yours and say where and how you drive.
* **Working files.** Backups, caches, logs, and the `out/` folder.

The manifest is a list of what ships rather than a list of what does not: a
new folder appearing in the project should be absent from the zip until
someone decides it belongs there, not present until someone notices.
"""

from __future__ import annotations

import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent

#: Files shipped as they are.
FILES = [
    "fa26_app.py",
    "fa26_gui.py",
    "check_app.py",
    "make_release.py",
    "Run FA26 Optimizer.bat",
]

#: Folders shipped whole, minus `SKIP_SUFFIX` and `__pycache__`.
FOLDERS = [
    "core",
    "cars",
    "ui",
    "logger",
    "data",
    "docs",
]

SKIP_SUFFIX = {".pyc", ".pyo", ".bak", ".csv", ".log", ".zip"}
SKIP_DIRS = {"__pycache__", ".git", "sources"}

#: Anything matching these must never ship, whatever the manifest says. The
#: check runs over the finished zip, so a mistake in the manifest is caught
#: rather than published.
FORBIDDEN = (
    ("data.acd", "VRC's packed car data"),
    ("fa26_unpacked", "VRC's unpacked car physics"),
    ("fa25_unpacked", "unpacked car physics"),
    # Telemetry, not the logger that records it -- `logger/fa26_baseline/` is
    # the app itself and has to ship.
    (".csv", "recorded telemetry"),
    (".bak", "a setup backup"),
    ("backup_pre", "a working backup folder"),
)


def wanted(path: Path) -> bool:
    if path.suffix.lower() in SKIP_SUFFIX:
        return False
    return not any(part in SKIP_DIRS for part in path.parts)


def collect() -> list[tuple[Path, str]]:
    items: list[tuple[Path, str]] = []
    for name in FILES:
        path = ROOT / name
        if path.is_file():
            items.append((path, name))
        else:
            print("  missing, skipped: %s" % name)
    for name in FOLDERS:
        folder = ROOT / name
        if not folder.is_dir():
            print("  missing, skipped: %s/" % name)
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file() and wanted(path.relative_to(ROOT)):
                items.append((path, str(path.relative_to(ROOT)).replace("\\", "/")))
    return items


def audit(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    problems = []
    for needle, what in FORBIDDEN:
        hits = [n for n in names if needle in n]
        if hits:
            problems.append("%s (%s): %s" % (needle, what, hits[:3]))
    return problems


def main() -> int:
    items = collect()
    target = ROOT / ("FA26_Optimizer_%s.zip" % date.today().strftime("%Y%m%d"))
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, name in items:
            archive.write(path, "FA26 Optimizer/" + name)

    problems = audit(target)
    if problems:
        target.unlink()
        print("\nNOT SHIPPED -- the archive held things it must not:")
        for line in problems:
            print("  " + line)
        return 1

    size = target.stat().st_size
    print("\n%s" % target.name)
    print("  %d files, %.1f MB" % (len(items), size / 1e6))
    print("  no car data, no telemetry, no setups")
    print("\nWhoever opens it needs Python 3.10+, Assetto Corsa with the VRC")
    print("Formula Alpha 2026, and to run \"Run FA26 Optimizer.bat\".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
