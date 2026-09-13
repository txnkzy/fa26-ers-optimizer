"""Where the files that ship with this tool live.

Frozen into a single executable, PyInstaller unpacks everything that ships --
the page, the fonts, the recharge tables, the logger -- into a temporary
folder whose location it puts in `sys._MEIPASS`. Running from source they sit
beside the code. Anything resolved from `__file__` gets the second case right
and the first case wrong, so both go through here.

Nothing writable belongs under this root: the unpacked folder is deleted when
the app exits. Per-user state goes in `install.app_dir()` instead.
"""

from __future__ import annotations

import sys
from pathlib import Path


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """The folder holding `ui/`, `data/` and `logger/`."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parent.parent
