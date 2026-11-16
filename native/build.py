"""Build the compiled lap integration and put it where the program looks.

    python native/build.py

Needs Rust. It is installed on E: rather than on the PATH, so this points
cargo at it; set CARGO_HOME / RUSTUP_HOME yourself to use another install.
The library lands in core/fa26sim.dll, which `core/sim26_native.py` loads.
Without it the program runs exactly as before, on the Python integration.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CRATE = ROOT / "native" / "fa26sim"


def main() -> int:
    env = dict(os.environ)
    env.setdefault("CARGO_HOME", r"E:\Rust\cargo")
    env.setdefault("RUSTUP_HOME", r"E:\Rust\rustup")
    cargo = Path(env["CARGO_HOME"]) / "bin" / "cargo.exe"
    if not cargo.is_file():
        found = shutil.which("cargo")
        if not found:
            print("cargo not found -- install Rust, or set CARGO_HOME")
            return 2
        cargo = Path(found)
    env["PATH"] = str(cargo.parent) + os.pathsep + env.get("PATH", "")
    done = subprocess.run([str(cargo), "build", "--release"], cwd=CRATE, env=env)
    if done.returncode:
        return done.returncode
    built = CRATE / "target" / "release" / "fa26sim.dll"
    target = ROOT / "core" / "fa26sim.dll"
    # A running copy of the app holds the library open and Windows will not
    # replace it. If what it holds is already this build there is nothing to
    # do; if not, say so rather than kill somebody's window.
    if not (target.is_file() and target.read_bytes() == built.read_bytes()):
        try:
            shutil.copy2(built, target)
        except PermissionError:
            print("%s is in use -- close the FA26 Optimizer window started "
                  "from this folder and run this again." % target)
            return 1
    print("built core/fa26sim.dll (%d kB)" % (built.stat().st_size // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
