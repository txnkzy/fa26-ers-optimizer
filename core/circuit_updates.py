"""Keeping the circuit table up to date without a new version of the program.

Each FIA Power Unit Information notice is read into the circuit table in the
project's repository within hours of publication (tools/fia_watch.py). At
start-up the program fetches that one file, checks it, and keeps it beside
the copy it was built with; `recharge` lays it over the built-in table entry
by entry. Nothing is sent: it is a single download of a public file.

Offline, or if the file is unreachable, unreadable or fails its checks, the
last good download stays in place -- or, if there never was one, the table
the program was built with -- and nothing else changes.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import urllib.request

import recharge

URL = ("https://raw.githubusercontent.com/txnkzy/fa26-ers-optimizer/main/"
       "data/fa26_recharge.json")
TIMEOUT_S = 8

#: What happened at the last check, for the diagnostics page.
STATUS = {"checked": "", "result": "not checked yet", "circuits": 0}


def check(url: str = URL) -> str:
    """Fetch, check and keep the circuit table. Returns what happened."""
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "FA26-ERS-Optimizer"})
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except Exception as exc:                  # offline, blocked, bad JSON
        return _done("could not download it (%s); using the copy on this "
                     "computer" % type(exc).__name__)
    if not isinstance(raw, dict):
        return _done("the download was not a circuit table; ignored")
    good = {k: v for k, v in raw.items() if k != recharge.META
            and isinstance(v, dict) and recharge.valid_entry(v)}
    if not good:
        return _done("the download held no usable circuits; ignored")
    skipped = len(raw) - len(good) - (recharge.META in raw)
    good[recharge.META] = raw.get(recharge.META) or {}
    path = recharge.DOWNLOADED_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(good, indent=2), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        return _done("could not save it (%s)" % type(exc).__name__)
    recharge.reload()
    count = len(good) - 1
    updated = str(good[recharge.META].get("updated", ""))
    if updated < recharge.built_in_stamp():
        return _done("the published table is older than this program's own; "
                     "using this program's", count)
    return _done("up to date: %d circuits, published %s%s" % (
        count, updated[:10], "" if not skipped else ", %d ignored" % skipped),
        count)


def check_in_background() -> None:
    threading.Thread(target=check, name="circuit-updates", daemon=True).start()


def _done(result: str, circuits: int = 0) -> str:
    STATUS.update(checked=dt.datetime.now().strftime("%d/%m %H:%M"),
                  result=result, circuits=circuits)
    return result
