"""Grouping recorded laps into the session they were driven in.

The logger names every file
`<track>__<layout>__<car>__<YYYYMMDD_HHMMSS>__lapNN_<time>.csv`, where the
stamp is the session rather than the lap, so everything before `__lapNN` is a
session key. Grouping on it gives exactly "the laps I drove at this track just
now", and cannot mix circuits or cars together by construction -- both are in
the key.

Which laps of a session to keep still needs judgement, because sessions
improve: Madrid ran 113.9, 107.3, 105.2, 102.9 as the driver learned it, and a
profile built from all four would be anchored to the warm-up. The rule here is
to keep laps within a tolerance of the session's best and to insist on a
minimum count, falling back to the single best lap when a session is too short
or too scattered to average.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

#: `...__lap07_1m40.505s.csv` -> session prefix, lap number, lap time
NAME_RE = re.compile(r"^(?P<prefix>.+)__lap(?P<num>\d+)_(?P<time>[\dm.]+)s(?P<tag>.*)\.csv$",
                     re.IGNORECASE)

#: Keep laps no slower than this multiple of the session best.
TOLERANCE = 1.03
#: Below this many kept laps, use the single best lap instead of averaging.
MIN_LAPS = 3

#: A tighter consistency window was tried here -- keep only laps within 1.5%
#: of the best, on the reasoning that a session driven while the circuit is
#: still being learned describes a driver who no longer exists. It scored well
#: and was wrong, and the way it was scored is the lesson.
#:
#: It was judged by replaying each session's best lap against a profile fitted
#: to that same session. That is an in-sample test, and it rewards fitting
#: fewer laps for the same reason any curve fits its own points better the
#: fewer of them there are. Pooling exists to stop exactly that: measured
#: out-of-sample, it took this model from 0.57% to 0.44%.
#:
#: On track the tighter rule rewrote 21 of 23 zones at Bahrain and came back
#: 0.236 s slower, same setup, same conditions, back to back. Any future
#: change here needs an out-of-sample measure -- fit one session, score
#: against a different one.


def parse(path) -> tuple[str, int, float] | None:
    """(session key, lap number, lap time in seconds) from a logger filename."""
    match = NAME_RE.match(Path(path).name)
    if not match:
        return None
    if match.group("tag"):          # `_pit` and anything else appended
        return None
    text = match.group("time")
    if "m" in text:
        minutes, seconds = text.split("m", 1)
        value = float(minutes) * 60.0 + float(seconds)
    else:
        value = float(text)
    return match.group("prefix"), int(match.group("num")), value


def group(paths) -> dict[str, list[Path]]:
    """Every session found, newest lap order preserved within each."""
    out: dict[str, list[Path]] = {}
    for path in paths:
        parsed = parse(path)
        if parsed is None:
            continue
        out.setdefault(parsed[0], []).append(Path(path))
    for key in out:
        out[key].sort(key=lambda p: parse(p)[1])
    return out


def select(paths, tolerance: float = TOLERANCE,
           minimum: int = MIN_LAPS) -> list[Path]:
    """The representative laps of one session, best first.

    Returns a single lap when there is not enough consistent running to
    average, so the caller never has to special-case a short session.
    """
    timed = [(parse(p)[2], Path(p)) for p in paths if parse(p)]
    if not timed:
        return [Path(p) for p in paths][:1]
    timed.sort()
    best = timed[0][0]
    kept = [p for t, p in timed if t <= best * tolerance]
    return kept if len(kept) >= minimum else [timed[0][1]]


def for_lap(path, folder=None, **kwargs) -> list[Path]:
    """The laps to model with, given one lap the user picked or the newest one."""
    path = Path(path)
    folder = Path(folder) if folder else path.parent
    parsed = parse(path)
    if parsed is None:
        return [path]
    siblings = [p for p in folder.glob("*.csv")
                if parse(p) and parse(p)[0] == parsed[0]]
    return select(siblings, **kwargs)


def latest(folder, track: str | None = None) -> list[Path]:
    """The most recent session in a folder, optionally restricted to a track."""
    sessions = group(Path(folder).glob("*.csv"))
    if track:
        sessions = {k: v for k, v in sessions.items() if k.startswith(track)}
    if not sessions:
        return []
    newest = max(sessions, key=lambda k: k.split("__")[-1])
    return select(sessions[newest])


def snapshots(telemetry_dir, track: str | None = None) -> list:
    """Map snapshots written beside the telemetry, oldest first.

    Named `<stamp>__<track>__<setup>.ini` since this guard was added, and
    `<stamp>__<setup>.ini` before it. Either way the circuit is recoverable,
    because a setup file is named after its track closely enough for
    `recharge.normalise` to land on the same key -- `suzuka.ini` and
    `rt_suzuka` both reduce to `suzuka`, `madring.ini` and
    `madrid_street_circuit_2026` both to `madrid`.
    """
    import recharge
    folder = Path(telemetry_dir) / "maps"
    if not folder.is_dir():
        return []
    want = recharge.normalise(track) if track else None
    out = []
    for path in folder.glob("*.ini"):
        parts = path.name.split("__")
        if not re.match(r"^\d{8}_\d{6}$", parts[0]):
            continue
        if want is not None:
            here = {recharge.normalise(p) for p in parts[1:]}
            if want not in here:
                continue
        out.append((parts[0], path))
    return [p for _, p in sorted(out)]


def map_order_warning(laps, telemetry_dir) -> str | None:
    """Whether these laps can be attributed to the map now in the setup.

    The tool overwrites the setup in place, so a lap recorded before the last
    write was driven on a map that no longer exists anywhere. Comparing its
    lap time against a replay then compares a simulated map against a lap
    driven on a different one -- which is how a month of Monza figures came to
    be quoted before anyone noticed, and how Madrid and Spa went the same way.

    Two things make this less obvious than it sounds. The stamp in a
    filename belongs to the *session*, not the lap, so a session spanning an
    apply would be condemned whole -- Suzuka opened at 11:22, the map was
    written at 11:43, and the laps that mattered were saved at 11:49 and
    11:54. File modification time gives the save time per lap instead. And
    the car reads a setup when it loads, so a file written mid-session may
    not have reached it until a restart; that is why a partial overlap is
    reported as something to check rather than as a verdict.
    """
    times = []
    for path in laps:
        try:
            times.append(Path(path).stat().st_mtime)
        except OSError:
            continue
    if not times:
        return None
    track = Path(laps[0]).name.split("__")[0]
    written = []
    for snap in snapshots(telemetry_dir, track):
        try:
            written.append(snap.stat().st_mtime)
        except OSError:
            continue
    if not written:
        return None
    newest = max(written)
    before = [t for t in times if t < newest]
    if not before:
        return None
    clock = time.strftime("%H:%M on %d/%m", time.localtime(newest))
    if len(before) == len(times):
        return ("all %d of these laps were saved before this track's setup was "
                "written at %s, so they were driven on a different map. Lap "
                "times here cannot be compared with what you just drove."
                % (len(times), clock))
    return ("%d of these %d laps were saved before this track's setup was "
            "written at %s. The car only reads a setup when it loads, so check "
            "whether the session was restarted after that write."
            % (len(before), len(times), clock))
