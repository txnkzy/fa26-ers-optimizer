"""Check the built application opens its window, twice running.

    python window_test.py

`check_app.py` drives the app over HTTP and `exe_test.py` drives the frozen
build the same way, both with `--headless`. Neither ever opened a window, so
neither could see the one thing every user does first -- and a build shipped
in which the second run showed the browser's own connection error instead of
the page, because the browser handed its command line to a process that
already owned the window profile and exited, taking the server down with it.

So this runs the real executable the way a person does: from an extracted
copy, with no arguments, twice, without closing anything in between.
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from tempfile import mkdtemp

SRC = Path(__file__).resolve().parent
BUNDLE = Path(r"E:\FA26 Optimizer\FA26 Optimizer.zip")
#: Long enough for PyInstaller to unpack itself and bind, on a cold cache.
PATIENCE = 30.0

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + name + ("  " + detail if detail else ""))
    if not ok:
        fails.append(name)


def _kill_tree(proc) -> None:
    """Kill the launcher and the application it spawned.

    PyInstaller's single-file build runs a bootloader that starts the real
    process, and killing the bootloader leaves that one running -- still
    holding its port, still answering, and still carrying whichever build it
    came from.
    """
    if proc is None:
        return
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   capture_output=True)
    try:
        proc.kill()
    except OSError:
        pass


def listening() -> set:
    """Loopback ports with something behind them, from the system's view."""
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                         capture_output=True, text=True).stdout
    ports = set()
    for line in out.splitlines():
        if "LISTENING" in line and "127.0.0.1:" in line:
            try:
                ports.add(int(line.split("127.0.0.1:")[1].split()[0]))
            except (IndexError, ValueError):
                pass
    return ports


def page_at(port: int) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/" % port,
                                    timeout=2) as answer:
            return answer.status == 200 and b"<html" in answer.read(4000).lower()
    except Exception:
        return False


def run_once(label: str, folder: Path) -> subprocess.Popen | None:
    """Launch the executable with no arguments and wait for a live page."""
    print("\n%s" % label)
    before = listening()
    proc = subprocess.Popen([str(folder / "FA26 Optimizer.exe")],
                            cwd=str(folder))
    port = None
    deadline = time.time() + PATIENCE
    while time.time() < deadline:
        time.sleep(0.5)
        fresh = listening() - before
        if fresh:
            port = sorted(fresh)[0]
            break
        if proc.poll() is not None:
            break
    check("  the server came up", port is not None,
          "" if port else "the application exited after %.0fs, serving nothing"
          % PATIENCE)
    if port is None:
        return proc

    check("  it binds loopback only", True, "127.0.0.1:%d" % port)
    # The regression was not a failure to start -- it was a server that went
    # away while the window was still loading. So look twice, a beat apart.
    check("  the page loads", page_at(port))
    time.sleep(4)
    check("  and is still there once the window has settled", page_at(port))
    check("  the process is still running", proc.poll() is None)
    return proc


def main() -> int:
    if not BUNDLE.is_file():
        print("no bundle at %s -- run build_exe.py first" % BUNDLE)
        return 1

    folders = []
    for label in ("FIRST RUN, from a fresh extract",
                  "SECOND RUN, nothing closed in between"):
        folder = Path(mkdtemp(prefix="fa26_window_"))
        with zipfile.ZipFile(BUNDLE) as archive:
            archive.extractall(folder)
        folders.append((label, folder))

    running = []
    for label, folder in folders:
        running.append(run_once(label, folder))

    for proc in running:
        _kill_tree(proc)
    # Leave the machine as it was found: these are the app's own windows.
    subprocess.run(
        ["powershell", "-NoProfile", "-c",
         "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
         "Where-Object { $_.CommandLine -like '*FA26 Optimizer*' } | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
         "-ErrorAction SilentlyContinue }"], capture_output=True)

    print()
    print("FAILED: %s" % ", ".join(fails) if fails else "ALL PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
