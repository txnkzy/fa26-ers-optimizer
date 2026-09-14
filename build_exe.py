"""Build the single-file application.

    python build_exe.py

Produces `E:\\FA26 Optimizer\\FA26 Optimizer.exe` -- one file, no installer, no
Python needed on the machine that runs it -- and zips it with the read-me and
the installation sheet, which is the file to hand to anyone else. The page, its fonts, the recharge
tables and the in-game logger all travel inside the executable; the logger is
written into Assetto Corsa by the app itself, from its first screen, so there
is nothing for anyone to copy by hand.

Nothing of VRC's is included. The car's physics are unpacked at runtime from
the copy the user already owns -- see `core/install.py`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
OUT = Path(r"E:\FA26 Optimizer")
NAME = "FA26 Optimizer"


def version() -> tuple:
    """The version, taken from the newest git tag.

    Hardcoding it meant the 1.0.1 build shipped claiming to be 1.0.0 in its
    own file properties -- the code was right and the label was a lie, which
    is the worst way round for a file people are being asked to trust.
    """
    import re
    import subprocess
    try:
        tag = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"], cwd=SRC,
            capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return (0, 0, 0, 0)
    parts = [int(n) for n in re.findall(r"\d+", tag)][:3]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts) + (0,)


VERSION = version()

#: Modules reached by bare name after a `sys.path` insert. PyInstaller's
#: analysis follows imports, not path manipulation, so every one of these has
#: to be declared or the frozen app dies on the first import it cannot find.
HIDDEN = [
    "ailine", "calibrate", "carphysics", "acd", "fa26", "install", "lap",
    "optimise26", "paths", "profile", "recharge", "session", "setupfile",
    "sim26",
]

#: Left out on purpose. `gui` and `cli` belong to the older FA25 tool and drag
#: in tkinter, which would add megabytes to an app that has no use for it.
EXCLUDE = ["tkinter", "gui", "cli", "unittest", "pydoc", "doctest", "test"]

#: Only the FA26 logger, and only the tables that are actually read. The
#: first build quietly carried the FA25 logger and a dead user-limits file
#: because the folders were taken whole.
DATA = [
    ("ui", "ui"),
    ("data/fa26_recharge.json", "data"),
    ("data/fa26_fia_events.json", "data"),
    ("data/fa26_reference_maps.json", "data"),
    ("logger/fa26_baseline", "logger/fa26_baseline"),
]


def version_file(path: Path) -> Path:
    """Windows version resource, so the file's properties are not blank."""
    dotted = ".".join(str(n) for n in VERSION)
    path.write_text("""VSVersionInfo(
  ffi=FixedFileInfo(filevers=%(v)s, prodvers=%(v)s, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'FA26 ERS Deployment Optimizer'),
      StringStruct('FileVersion', '%(d)s'),
      StringStruct('InternalName', '%(n)s'),
      StringStruct('OriginalFilename', '%(n)s.exe'),
      StringStruct('ProductName', 'FA26 ERS Deployment Optimizer'),
      StringStruct('ProductVersion', '%(d)s'),
      StringStruct('Comments',
                   'Works out ERS deployment maps for the VRC Formula '
                   'Alpha 2026 from your own recorded laps.')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""" % {"v": str(VERSION), "d": dotted, "n": NAME}, encoding="utf-8")
    return path


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed.  python -m pip install pyinstaller")
        return 1

    # One source of truth. A build that misreports its own version makes
    # every bug report a guess about which build it came from, so a
    # disagreement stops the build rather than shipping quietly.
    sys.path.insert(0, str(SRC))
    import fa26_app
    tagged = ".".join(str(n) for n in VERSION[:3])
    if fa26_app.VERSION != tagged:
        print("version mismatch: fa26_app.VERSION is %s, the git tag says %s."
              % (fa26_app.VERSION, tagged))
        print("Update fa26_app.VERSION, or tag the commit you meant to build.")
        return 1

    print("icon")
    subprocess.run([sys.executable, "make_icon.py"], cwd=SRC, check=True)

    # Generated here rather than committed, so the version on the sheet is
    # always the version of the build it ships inside -- the same reason the
    # icon is drawn here and the executable's properties come from the tag.
    print("installation sheet")
    subprocess.run([sys.executable, "make_pdf.py"], cwd=SRC, check=True)

    work = SRC / "build"
    dist = SRC / "dist"
    for folder in (work, dist):
        shutil.rmtree(folder, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--noconsole", "--clean", "--noconfirm",
        "--name", NAME,
        "--icon", str(SRC / "ui" / "app.ico"),
        "--version-file", str(version_file(work / "version.txt")),
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(work),
    ]
    for folder in ("core", "cars"):
        args += ["--paths", str(SRC / folder)]
    for source, target in DATA:
        args += ["--add-data", "%s%s%s" % (SRC / source, ";", target)]
    for module in HIDDEN:
        args += ["--hidden-import", module]
    for module in EXCLUDE:
        args += ["--exclude-module", module]
    args.append(str(SRC / "fa26_app.py"))

    print("\nfreezing (a minute or two)")
    built = subprocess.run(args, cwd=SRC, capture_output=True, text=True)
    if built.returncode != 0:
        print(built.stdout[-3000:])
        print(built.stderr[-3000:])
        return 1

    exe = dist / (NAME + ".exe")
    if not exe.is_file():
        print("no executable was produced")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / exe.name
    shutil.copy2(exe, target)
    for name in ("READ ME FIRST.txt", "Installation.pdf"):
        shutil.copy2(SRC / "docs" / name, OUT / name)

    bundle = OUT / ("%s.zip" % NAME)
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(target, target.name)
        for name in ("READ ME FIRST.txt", "Installation.pdf"):
            archive.write(OUT / name, name)

    # The same bundle again, with its version in the name. The plain one is
    # always whatever was built last, which is convenient here and useless
    # everywhere else: once two downloads are sitting in someone's downloads
    # folder, or a release has to be re-uploaded, nothing about the file says
    # which build it is. Keeping both means the newest is always at a stable
    # name and every version is still on disk to go back to.
    dotted = ".".join(str(n) for n in VERSION[:3])
    stamped = OUT / ("%s v%s.zip" % (NAME, dotted))
    shutil.copy2(bundle, stamped)

    print("\n%s" % target)
    print("  %.1f MB" % (target.stat().st_size / 1e6))
    print("%s" % bundle)
    print("  %.1f MB  -- always the newest build" % (bundle.stat().st_size / 1e6))
    print("%s" % stamped)
    print("  %.1f MB  -- this is the file to upload"
          % (stamped.stat().st_size / 1e6))
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(dist, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
