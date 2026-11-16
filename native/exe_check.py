"""Check the built application is running the compiled lap integration.

    python native/exe_check.py [path to "FA26 Optimizer.zip"]

The first packaged build loaded nothing and ran on the Python integration,
twenty times slower and otherwise identical, so nothing looked wrong. This
extracts the bundle the way a user would, starts the executable without a
window, and asks it which integration it loaded.

With no argument it looks where `build_exe.py` writes: FA26_BUILD_OUT, or
E:\\Claude Code\\FA26 Optimizer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from tempfile import mkdtemp

PORT = 48631
PATIENCE = 60.0


def main() -> int:
    if len(sys.argv) > 1:
        bundle = Path(sys.argv[1])
    else:
        bundle = Path(os.environ.get("FA26_BUILD_OUT") or r"E:\Claude Code\FA26 Optimizer") \
            / "FA26 Optimizer.zip"
    if not bundle.is_file():
        print("no bundle at %s" % bundle)
        return 2
    folder = Path(mkdtemp(prefix="fa26_exe_"))
    with zipfile.ZipFile(bundle) as archive:
        archive.extractall(folder)
    proc = subprocess.Popen([str(folder / "FA26 Optimizer.exe"), "--headless",
                             "--port", str(PORT)], cwd=folder)
    try:
        ready, began = None, time.time()
        while time.time() - began < PATIENCE:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/ready" % PORT, timeout=5) as r:
                    ready = json.loads(r.read())
                break
            except Exception:                       # noqa: BLE001
                time.sleep(0.3)
        if ready is None:
            print("  FAIL the executable never answered")
            return 1
        integration = ready.get("integration", "not reported")
        ok = integration == "compiled"
        print("  %s the packaged app runs the compiled integration  v%s, %s"
              % ("ok  " if ok else "FAIL", ready.get("version"), integration))
        return 0 if ok else 1
    finally:
        # The bootloader starts the real process; kill the tree, not the stub.
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
