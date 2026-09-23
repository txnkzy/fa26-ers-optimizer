"""Smoke test for the web front end: every endpoint, then a real write.

    python check_app.py

Runs the server in-process, solves the cheapest session it can find in your
own telemetry, and applies the result to a *copy* of one of your setups in a
temp folder -- never to the real one. Takes a couple of minutes, almost all of
it in the solver.

Exits non-zero on the first thing that does not hold, so it can gate a
release. It is deliberately end-to-end rather than a set of unit tests: every
bug this front end has had so far lived in the seams -- a Path where JSON was
expected, a CSS rule beating the browser's own `hidden`, a slider offering a
number the car clamps.
"""
import json
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import fa26_app as A

import tempfile
SCRATCH = Path(tempfile.mkdtemp(prefix="fa26_check_"))

# Every apply keeps a dated copy of the written setup beside the telemetry, so
# a recorded lap can be attributed to the map it was driven on afterwards.
# These checks apply to a scratch setup, and a snapshot of that is not a record
# of anything -- it is a file in the user's telemetry folder named after a test
# fixture, sitting among the real ones. Fourteen had accumulated there before
# anyone noticed. The checks keep their own folder.
A.SNAPSHOT_DIR = SCRATCH / "map-snapshots"
A.history.DIR = SCRATCH
PORT = 8739
BASE = "http://127.0.0.1:%d" % PORT

srv = ThreadingHTTPServer(("127.0.0.1", PORT), A.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.loads(r.read())


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read())
        raise AssertionError(payload.get("trace") or payload.get("error"))


fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + (("  " + detail) if detail else ""))
    if not ok:
        fails.append(name)


# --- static ---------------------------------------------------------------
for path, needle in (("/", b"<title>FA26"), ("/ui/app.js", b"function strip"),
                     ("/ui/styles.css", b"--accent")):
    with urllib.request.urlopen(BASE + path) as r:
        body = r.read()
    check("serves %s" % path, needle in body)
try:
    urllib.request.urlopen(BASE + "/ui/../fa26_app.py")
    check("refuses path traversal", False)
except urllib.error.HTTPError as e:
    check("refuses path traversal", e.code == 404, "HTTP %d" % e.code)

# The window must look the same with the network unplugged, so the page may
# not reach for anything it does not ship.
page = urllib.request.urlopen(BASE + "/").read().decode("utf-8", "replace")
check("page makes no external requests", "https://" not in page,
      [l for l in page.splitlines() if "https://" in l][:1])
faces = sorted((ROOT / "ui" / "fonts").glob("*.woff2"))
check("fonts are bundled", len(faces) == 8, "%d files" % len(faces))
for face in faces[:1] + faces[-1:]:
    with urllib.request.urlopen(BASE + "/ui/fonts/" + face.name) as r:
        blob, ctype = r.read(), r.headers["Content-Type"]
    check("  serves %s" % face.name,
          blob[:4] == b"wOF2" and ctype == "font/woff2", ctype)

# --- this session ---------------------------------------------------------
import recharge  # noqa: E402  (fa26_app already put core/ on the path)

data = get("/api/current")
check("car id present", data["car"].startswith("vrc_formula_alpha"))

# Nothing of VRC's may ship with this tool: the car's physics are unpacked
# from the copy the user already owns, into a cache outside the project.
ready = get("/api/ready")
check("Assetto Corsa found without being told where", ready["ok"],
      ready["problem"] or ready["root"])
import install  # noqa: E402
cache = install.cache_dir(A.CAR_ID)
check("car data is cached outside the project", ROOT not in cache.parents,
      str(cache))
# What matters is not whether a scratch folder is tidy but what would reach
# somebody else. Built from the real manifest so the two cannot drift.
sys.path.insert(0, str(ROOT))
import make_release  # noqa: E402
shipped = [name for _, name in make_release.collect()]
leaks = [n for n in shipped
         for needle, _ in make_release.FORBIDDEN if needle in n]
check("the release ships no car data, telemetry or setups", not leaks,
      str(leaks[:3]))
check("the release is small enough to send", len(shipped) < 200,
      "%d files" % len(shipped))
check("the logger is found where CSP keeps Lua apps",
      isinstance(ready["logger"], bool))
check("offers exactly one session -- the current one",
      data["session"] is not None and "revision" in data["session"])
detail = data["session"]
print("  using session: %s  (%d of %d laps)"
      % (detail["circuit"], detail["pooled"], detail["recorded"]))

rev = get("/api/revision")["revision"]
check("revision is cheap to watch and matches", rev == detail["revision"])

# "This session" means laps driven since the sim started. It used to mean the
# newest folder on disk, so a session from hours ago and a reboot later still
# showed a green live light.
check("the game's own state is reported", "running" in data["game"])
check("liveness is a fact about the session, not its age",
      isinstance(detail["live"], bool),
      "live=%s, game running=%s" % (detail["live"], data["game"]["running"]))
if not data["game"]["running"]:
    check("nothing is live while the game is closed", not detail["live"])

import install as _install  # noqa: E402
_real_game = _install.game_running
try:
    _install.game_running = lambda: {
        "running": True, "since": detail["newest_lap"] - 600, "for_s": 600}
    A._DETAIL_CACHE.clear()
    check("laps driven after the game started are live",
          A.current()["session"]["live"] is True)
    _install.game_running = lambda: {
        "running": True, "since": detail["newest_lap"] + 3600, "for_s": 10}
    A._DETAIL_CACHE.clear()
    check("laps from a previous run of the game are not",
          A.current()["session"]["live"] is False)
finally:
    _install.game_running = _real_game
    A._DETAIL_CACHE.clear()
check("fuel read from telemetry", detail["fuel_l"] is not None,
      "%.1f L" % (detail["fuel_l"] or 0))
check("charge read from telemetry", detail["charge"] is not None)
check("car limits offered", detail["limits"]["qualify"]["max"] == 90
      and detail["limits"]["race"]["max"] == 85, str(detail["limits"]))
check("no lap picker needed", isinstance(detail["laps"], list))

# --- stopping -------------------------------------------------------------
# Done before the real solve, because a stop that leaves anything behind would
# poison everything measured after it.
check("cancelling when idle reports nothing to stop",
      post("/api/cancel", {})["stopping"] is False)
post("/api/solve", {"key": detail["key"], "circuit": detail["circuit"],
                    "qualify": detail["qualify"], "race": detail["race"],
                    "unlimited": False, "write_alloc": True})
time.sleep(8)
mid = get("/api/poll?since=0&have=99")
check("a solve is running to stop", mid["running"])
check("the stop is accepted", post("/api/cancel", {})["stopping"])
asked = time.time()
while time.time() - asked < 40:
    mid = get("/api/poll?since=0&have=-1")
    if not mid["running"]:
        break
    time.sleep(0.2)
check("stopping actually stops it", not mid["running"],
      "%.1fs after asking" % (time.time() - asked))
check("a stop is not an error", mid["cancelled"] and not mid["error"])
# A setup carrying one new map and two old ones looks finished and quietly
# does something else, so a stop keeps nothing.
check("a stop keeps no half-finished maps", not mid.get("summary"),
      "%d strategies" % len(mid.get("summary") or []))

# --- solve ----------------------------------------------------------------
t0 = time.time()
post("/api/solve", {"key": detail["key"], "circuit": detail["circuit"],
                    "qualify": detail["qualify"], "race": detail["race"],
                    "unlimited": False, "write_alloc": True})
since = 0
summary = []
polls = sent = 0
seen_progress = []
phases = []
every_line = []
while True:
    snap = get("/api/poll?since=%d&have=%d" % (since, len(summary)))
    since = snap["since"]
    polls += 1
    every_line.extend(snap["lines"])
    seen_progress.append(snap["progress"])
    if snap["phase"] and snap["phase"] not in phases:
        phases.append(snap["phase"])
    if "summary" in snap:
        summary = snap["summary"]
        sent += 1
    if not snap["running"]:
        break
    time.sleep(0.4)
snap["summary"] = summary
# One delivery per strategy, plus the first empty one and the last poll --
# not one per poll, which over a two-minute solve was tens of megabytes.
check("summary is not resent on every poll", sent <= 6 < polls,
      "%d polls, %d carried a summary" % (polls, sent))
check("progress only ever moved forwards",
      all(b >= a - 1e-9 for a, b in zip(seen_progress, seen_progress[1:])),
      "%d readings" % len(seen_progress))
check("progress reached the end", seen_progress[-1] >= 0.999,
      "%.3f" % seen_progress[-1])
# Not every stage: fitting the car and building the recharge map are each
# faster than the poll interval, so a passing run need not observe them.
check("the slow stages named themselves", len(phases) >= 3, " / ".join(phases))

# The plain lines are the ones shown by default, so they are the ones that
# have to read as English. The engineering vocabulary belongs behind the
# technical-details toggle.
# "deployment zone" is the thing the tool produces and belongs in plain
# text; what must not leak is the read-out vocabulary.
JARGON = ("MGU", "C5.", " kJ", " kW", "harvest", "taper", "bins", "median",
          "Drag profile", "Traction", "splits", "STRAT Map", "k 0.",
          "store ")
plain = [l["text"] for l in every_line
         if l["kind"] in ("line", "result", "warn")]
jargon = [x for x in plain if any(w in x for w in JARGON)]
check("plain lines avoid the engineering vocabulary", not jargon,
      jargon[0][:70] if jargon else "")
check("technical lines are still kept",
      sum(1 for l in every_line if l["kind"] == "detail") >= 8,
      "%d detail lines" % sum(1 for l in every_line if l["kind"] == "detail"))
check("solve finished without error", not snap["error"], snap["error"] or "")
check("three strategies produced", len(snap["summary"]) == 3,
      "%.0f s" % (time.time() - t0))
for s in snap["summary"]:
    check("  STRAT %d has a map" % s["strat"], len(s["map"]) >= 1,
          "%s, %d splits" % (s["lap"], len(s["map"])))
    check("  STRAT %d has a speed trace" % s["strat"],
          len(s["speed"]) > 50 and s["peak_kph"] > 200,
          "peak %.0f kph" % s["peak_kph"])
check("qualifying is the fastest of the three",
      snap["summary"][0]["lap_s"] < snap["summary"][2]["lap_s"])

# --- apply ----------------------------------------------------------------
import setupfile  # noqa: E402
import fa26  # noqa: E402

src = next(iter(Path(A.docs_dir() / "setups" / A.CAR_ID).glob("*/*.ini")))
target_ini = SCRATCH / "e2e_setup.ini"
for stale in (target_ini, target_ini.with_suffix(".ini.bak")):
    stale.unlink(missing_ok=True)
shutil.copy(src, target_ini)

res = post("/api/apply", {"path": str(target_ini), "write_alloc": True})
check("apply returns JSON-safe fields",
      isinstance(res["backup"], str) and isinstance(res["active"], int))
check("apply made a .bak", target_ini.with_suffix(".ini.bak").exists())

text = target_ini.read_text(encoding="utf-8", errors="replace")
mapping = setupfile.item_index_map(A.setup_ini())
values = setupfile.read_values(text)
check("STRAT Map points at 1", values.get(mapping["STRAT_MAP"]) == 1,
      str(values.get(mapping["STRAT_MAP"])))
check("unlimited mode written off",
      values.get(mapping["ERS_UNLIMITED_MODE"]) == 0,
      str(values.get(mapping["ERS_UNLIMITED_MODE"])))
check("recharge written", values.get(mapping["ERS_REGEN_MAX_MJ_Q"]) == detail["qualify"],
      "%s vs %s" % (values.get(mapping["ERS_REGEN_MAX_MJ_Q"]), detail["qualify"]))
for n in (1, 2, 3):
    splits = fa26.read_map(text, mapping, n)
    live = [s for s in splits if s.deploy_kw > 0 or s.clip_kw > 0]
    check("  map %d written" % n, len(live) >= 1, "%d live splits" % len(live))

# --- unlimited round trip -------------------------------------------------
alloc = recharge.lookup("monza")
up = A.apply_limits(alloc, 90, 85, True)
check("unlimited allocation clamps to the car's range",
      up.qualify == 90 and up.race == 85 and up.race_overtake == 85,
      "%d/%d/%d" % (up.qualify, up.race, up.race_overtake))

A.save_user_limits("__e2e_test__", 62, 71, False)
check("user limits persist", A.load_user_limits()["__e2e_test__"]["qualify"] == 62)
A.forget_user_limits("__e2e_test__")
check("user limits forgettable", "__e2e_test__" not in A.load_user_limits())

print()

def _crossing_branch_is_guarded() -> tuple[bool, str]:
    """Nothing in the branch that saves a lap may touch the game directly."""
    import re
    source = (Path(A.__file__).resolve().parent / "logger" / "fa26_baseline"
              / "fa26_baseline.lua").read_text(encoding="utf-8")
    start = source.index("if crossedLine then")
    branch = source[start:source.index("pendingSave = {", start)]
    # comments and strings first: this checks code, not prose
    branch = re.sub(r"--\[\[.*?\]\]", "", branch, flags=re.S)
    branch = re.sub(r"--[^\n]*", "", branch)
    branch = re.sub(r'"[^"\n]*"', '""', branch)
    bare = re.findall(r"\bac\.\w+", branch)
    if bare:
        return False, ("the crossing branch reaches for %s directly; put it in "
                       "sidecarExtras and let the pcall cover it"
                       % ", ".join(sorted(set(bare))))
    if "pcall(sidecarExtras)" not in branch:
        return False, "the crossing branch no longer calls sidecarExtras through pcall"
    return True, "nothing but a single guarded call"


_ok, _why = _crossing_branch_is_guarded()
check("losing the sidecar cannot lose the lap", _ok, _why)


# --- the logger is never replaced under a running game ----------------------
# CSP watches these files and hot-reloads a Lua app the moment one changes.
# A reload resets every local the script holds -- whether it is recording, the
# samples so far, how many laps it has saved -- so writing this file
# mid-session throws away the lap in progress. It happened: a 1:38.867 at
# Austin, the file written at 11:58:18, the script reloaded at 12:00:46, and
# the lap never reached disk.
def _logger_install_guarded() -> tuple[bool, str]:
    import install as _install
    real = _install.game_running
    _install.game_running = lambda: {"running": True, "since": 0, "for_s": 0}
    try:
        _install.install_logger()
        return False, "it installed the logger while the game was running"
    except _install.MissingCarData as exc:
        if exc.kind != "game_running":
            return False, "refused for the wrong reason: %s" % exc.kind
    except Exception as exc:
        return False, "refused with the wrong error: %r" % exc
    finally:
        _install.game_running = real
    return True, "refused, and `force` is there for a caller that has asked"


_ok, _why = _logger_install_guarded()
check("the logger is not replaced mid-session", _ok, _why)


# --- super-clip stays reachable --------------------------------------------
# Super-clipping was in the action space and in no starting map, which on an
# energy-neutral search is the same as not being there: one zone of harvest
# with nothing to spend it on is rejected before the deployment that would pay
# for it is ever tried. Measured, it is now chosen in 5 of 14 solved maps and
# worth 0.02 to 1.08 s where it is chosen. Both halves of what made that
# possible are checked here, because both were once absent without anything
# noticing.
def _superclip_reachable() -> tuple[bool, str]:
    import optimise26 as _opt
    import fa26 as _fa26

    # Enough of a profile for the two helpers these moves go through: a lap
    # that is flat out everywhere, which is the permissive case and so cannot
    # be what hides a missing move.
    class _Shape:
        length_m = 5000.0
        ds = 2.5
        gas = [1.0] * 2001

        def _slot(self, distance_m):
            return min(int((distance_m % self.length_m) / self.ds),
                       len(self.gas) - 1)

        def full_throttle_at(self, _distance_m):
            return True

    seed = [_fa26.Split(0, 400, 400, 1000, 200, 0),
            _fa26.Split(1000, 1400, 1400, 2500, 200, 0),
            _fa26.Split(2500, 3000, 3000, 5000, 200, 0)]
    shape = _Shape()
    family = _opt._family(seed, shape)
    seeded = sum(1 for m in family if any(s.clip_kw > 0 for s in m))
    if not seeded:
        return False, "no starting map in the family carries super-clip"
    # Turning it on from nothing is `_more_harvest`; `_level_clip` only trades
    # an existing one's power against its width, and the single-split sweep
    # reaches it through `_variants`.
    started = _opt._more_harvest(seed[0])
    if started is None or started.clip_kw <= 0:
        return False, "the search cannot turn super-clip on"
    variants = _opt._variants(seed[0], shape)
    if not any(v.clip_kw > 0 for v in variants):
        return False, "no single-split move introduces super-clip"
    return True, ("%d of %d starting maps carry it, and %d of %d single-split "
                  "moves introduce it" % (seeded, len(family),
                                          sum(1 for v in variants if v.clip_kw > 0),
                                          len(variants)))


_ok, _why = _superclip_reachable()
check("super-clip is seeded and reachable", _ok, _why)


# --- the fast path is the same path ----------------------------------------
# sim26 builds each step's command by walking the splits once, because reading
# the split list afresh for every step was 12.3 million linear scans in a
# single Spa race search. It has to be the same answer as the per-point lookup
# it replaced, on maps far stranger than any solver produces.
def _commands_agree() -> tuple[bool, str]:
    import random
    import sim26 as _sim
    import fa26 as _fa26
    random.seed(20260919)
    for _trial in range(120):
        count = random.randint(2, 9)
        length = random.choice([3271.0, 5750.0, 6943.0])
        points = sorted(random.sample(range(0, int(length)), count * 4))
        splits = []
        for k in range(count):
            a, b, c, d = points[4 * k:4 * k + 4]
            splits.append(_fa26.Split(a, b, c, d,
                                      random.choice([0, 100, 200, 350]),
                                      random.choice([0, 0, 150, 350])))
        step = random.choice([2.5, 5.0])
        steps = int(length / step)
        want = [_fa26.map_command_at(splits, i * step, length) for i in range(steps)]
        got = _sim._commands_along(splits, steps, step, length)
        if want != got:
            where = next(i for i, (a, b) in enumerate(zip(want, got)) if a != b)
            return False, "disagree at %.1f m: %s against %s" % (
                where * step, want[where], got[where])
    return True, "120 random maps, every step"


_ok, _why = _commands_agree()
check("the fast command lookup matches the slow one", _ok, _why)


# --- the logger's own record ----------------------------------------------
# The logger now writes the deployment maps a lap was driven on into a file
# beside it, so attribution is a read rather than a reconstruction. Two things
# have to hold for that to be worth anything: the Lua has to parse, and the
# file it writes has to be the file Python reads.
def _lua_parses() -> tuple[bool, str]:
    """Two checks, because one of them let a broken logger ship.

    `luaparser` accepted a file Assetto Corsa refused outright -- a raw line
    break inside a double-quoted string, left behind by a shell that turned a
    \n escape into an actual newline. The game said "unfinished string near
    '\"'" and would not load the app; the Python parser said nothing. Four
    laps were driven with nothing recording them.
    """
    source = Path(A.__file__).resolve().parent / "logger" / "fa26_baseline" / "fa26_baseline.lua"
    sys.path.insert(0, str(Path(A.__file__).resolve().parent))
    import lua_lint
    issues = lua_lint.check(source)
    if issues:
        return False, issues[0]
    try:
        from luaparser import ast as _lua_ast
    except ImportError:
        return True, "lexed clean; luaparser not installed (pip install luaparser)"
    try:
        _lua_ast.parse(source.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    return True, "lexed and parsed"


def _lua_contract() -> tuple[bool, str]:
    """The two halves of the logger, and the Python that reads what it writes.

    Three ways this file has gone wrong that a parser cannot see:

      * `saveMaps` reads a field off the finished lap that the table handed to
        it never carried. nil, then a raise, then a pcall swallowing it, then
        no sidecar and no explanation.
      * a name used before it is declared -- `tostring(nil)` is "nil", written
        into the file, and nobody notices until the value is needed.
      * the keys the Lua writes drifting from the keys `provenance` reads.
    """
    import re
    source = (Path(A.__file__).resolve().parent / "logger" / "fa26_baseline"
              / "fa26_baseline.lua").read_text(encoding="utf-8")

    # every `lap.<field>` saveMaps reads, against the table it is handed
    body = source[source.index("local function saveMaps"):]
    body = body[:body.index("\nend\n")]
    wanted = set(re.findall(r"\blap\.([A-Za-z_]\w*)", body))
    handed = source[source.index("pendingSave = {"):]
    handed = handed[:handed.index("}")]
    carried = set(re.findall(r"(\w+)\s*=", handed))
    missing = sorted(wanted - carried)
    if missing:
        return False, "saveMaps reads lap.%s, which the saved lap never carries" % (
            ", lap.".join(missing))

    # every global-looking name saveMaps uses has to be declared above it.
    # String contents go first: the keys it writes are shouted too, and
    # "DOMINANT_STRAT=" is not a reference to anything.
    code = re.sub(r'"[^"\n]*"', '""', re.sub(r"'[^'\n]*'", "''", body))
    for name in sorted(set(re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", code))):
        declared = re.search(r"local\s+%s\s*=" % name, source)
        if not declared or declared.start() > source.index("local function saveMaps"):
            return False, "%s is used in saveMaps but never declared above it" % name

    # the keys it writes, against the keys provenance reads back
    written = set(re.findall(r'"([A-Z_]+)="', body)) | {
        m for m in ("MAP_", "SPLIT_") if m in body}
    sys.path.insert(0, str(Path(A.__file__).resolve().parent / "core"))
    import provenance as _prov
    reader = Path(_prov.__file__).read_text(encoding="utf-8")
    for key in sorted(written):
        if key not in reader:
            return False, "the logger writes %s and provenance never reads it" % key
    return True, "%d fields carried, %d keys matched" % (len(wanted), len(written))


_ok, _why = _lua_contract()
check("the logger's writer and reader agree", _ok, _why)

_ok, _why = _lua_parses()
check("the in-game logger is valid Lua", _ok, _why)


def _sidecar_round_trips() -> tuple[bool, str]:
    """A lap carrying its own map attributes with no snapshots in sight.

    The sidecar is written here exactly as `saveMaps` in the Lua writes it --
    the same format string, transcribed -- so a change to either side that
    leaves them disagreeing shows up as a failure rather than as laps quietly
    falling back to being matched by behaviour.
    """
    import provenance as _prov
    import lap as _laplib
    import fa26 as _fa26

    lap_path = None
    for candidate in sorted(A.baseline_dir().glob("*.csv")):
        try:
            driven = _laplib.load(candidate)
        except Exception:
            continue
        if not _laplib.problem(driven):
            lap_path = candidate
            break
    if lap_path is None:
        return True, "no usable lap to test with, skipped"

    scratch = SCRATCH / "sidecar"
    scratch.mkdir(parents=True, exist_ok=True)
    copy = scratch / lap_path.name
    shutil.copy2(lap_path, copy)
    want = [_fa26.Split(0, 300, 300, 500, 350, 0),
            _fa26.Split(500, 700, 700, 900, 150, 0)]
    other = [_fa26.Split(0, 100, 100, 200, 120, 0)]
    driven = _laplib.load(copy)
    slot = _prov.driven_strat(driven) or 1
    spare = 1 if slot != 1 else 2
    lines = ["[LAP]", "LOGGER=2", "TRACK_LENGTH_M=%.1f" % driven.length_m,
             "STRATS=%d,%d" % tuple(sorted((slot, spare))),
             "DOMINANT_STRAT=%d" % slot]
    for index, splits in sorted(((slot, want), (spare, other))):
        lines.append("[MAP_%d]" % index)
        for n, s in enumerate(splits, start=1):
            lines.append("SPLIT_%d=%d,%d,%d,%d,%d,%d" % (
                n, s.start_m, s.deploy_end_m, s.clip_start_m, s.end_m,
                s.deploy_kw, s.clip_kw))
    copy.with_suffix(_prov.SIDECAR_SUFFIX).write_text(
        "\n".join(lines) + "\n", encoding="utf-8")

    found = _prov.attribute(_laplib.load(copy), [], {})
    if not found:
        return False, "a lap with its own map was refused: " + found.reason
    if not found.recorded:
        return False, "the recorded map was not used"
    got = [(s.start_m, s.end_m, s.deploy_kw) for s in found.splits]
    expect = [(s.start_m, s.end_m, s.deploy_kw) for s in want]
    if got != expect:
        return False, "read back the wrong map: %s" % (got,)
    return True, "STRAT %d, %d splits, no snapshots needed" % (slot, len(got))


_ok, _why = _sidecar_round_trips()
check("a lap carrying its own map needs no snapshots", _ok, _why)


# --- the page actually runs ------------------------------------------------
# A page whose script throws on load still serves, still returns 200, and
# still contains every element the markup declares -- so every check that
# reads the HTML passes while the app does nothing at all. That is exactly
# what happened: one duplicated `const` inside a function stopped app.js
# executing, and the rail sat on its placeholder text through a full round of
# element-level testing.
#
# So the page is loaded in the browser the app itself uses, headless, and
# asked for something only the script can produce.
def _browser_renders() -> tuple[bool, str]:
    exe = A.find_browser()
    if not exe:
        return True, "no Chromium-based browser found, skipped"
    import subprocess as _sp
    profile = SCRATCH / "headless-profile"
    try:
        out = _sp.run(
            [str(exe[0] if isinstance(exe, tuple) else exe),
             "--headless=new", "--disable-gpu", "--no-first-run",
             "--user-data-dir=" + str(profile),
             "--virtual-time-budget=6000", "--dump-dom", BASE + "/"],
            capture_output=True, timeout=90).stdout.decode("utf-8", "replace")
    except Exception as exc:
        return True, "could not run headless (%s), skipped" % exc
    # `version` is written into the header by the script, never by the markup.
    stamped = 'id="version">' in out and "</span>" in out
    filled = out.split('id="version">', 1)[-1].split("<", 1)[0].strip() if stamped else ""
    if filled:
        return True, "script ran, stamped v" + filled
    return False, "the page loaded but its script did not run"


UI = ROOT / "ui"

_ok, _why = _browser_renders()
check("the page's script runs in a real browser", _ok, _why)


# --- the switch you set before driving is reachable before driving ---------
# It used to live inside the live estimate, inside the session card, which is
# hidden until a lap lands. Every check that asks "is the element there?"
# passed the whole time, because it was there -- three hidden ancestors deep,
# on a page that has just opened and has no laps yet.
def _toggle_is_visible() -> tuple[bool, str]:
    import html.parser

    class Walk(html.parser.HTMLParser):
        VOID = {"input", "br", "img", "hr", "meta", "link", "source"}

        def __init__(self):
            super().__init__()
            self.stack = []
            self.found = None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            frame = (tag, a.get("id"), "hidden" in a)
            if a.get("id") == "live-search":
                self.found = self.stack + [frame]
            if tag not in self.VOID:
                self.stack.append(frame)

        def handle_endtag(self, tag):
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    del self.stack[i:]
                    return

    w = Walk()
    w.feed((UI / "index.html").read_text(encoding="utf-8"))
    if not w.found:
        return False, "no between-laps switch on the page at all"
    buried = [(tag, ident) for tag, ident, hid in w.found if hid]
    if buried:
        return False, "hidden behind %s" % ", ".join(
            i or tg for tg, i in buried)
    return True, "reachable with no laps, %d levels deep" % len(w.found)


_ok, _why = _toggle_is_visible()
check("the between-laps switch is visible before the first lap", _ok, _why)


# --- energy commanded where the car will refuse it -------------------------
# The car crosses the line already flat out and power demand may not be raised
# under throttle, so a split starting at metre zero is refused until the driver
# first lifts. Seven Austin laps deployed exactly zero before the first lift
# while the map asked for 2.30 MJ there. The simulation does not model it, so
# the tool has to say so instead of quietly writing the map.
def _opening_energy_flagged() -> tuple[bool, str]:
    import lap as _laplib
    import profile as _prof
    laps = []
    for path in sorted(A.baseline_dir().glob("*.csv"))[:40]:
        try:
            one = _laplib.load(path)
        except Exception:
            continue
        if not _laplib.problem(one) and one.length_m > 1500:
            laps.append(one)
            break
    if not laps:
        return True, "no telemetry to check against, skipped"
    shape = _prof.Profile(laps)
    lift = A.first_lift_m(shape)
    if lift <= 0:
        return True, "this lap never lifts, skipped"
    # one map that piles everything into the opening, one that spends later
    bad = [fa26.Split(0, int(lift), fa26.CLIP_OFF, int(lift), 350, 0)]
    good = [fa26.Split(int(lift) + 200, int(lift) + 700, fa26.CLIP_OFF,
                       int(lift) + 700, 350, 0)]
    b = A.commanded_kj(bad, shape, 0.0, lift)
    g = A.commanded_kj(good, shape, 0.0, lift)
    if b <= 0:
        return False, "a split across the whole opening commands nothing"
    if g > 1.0:
        return False, "a split well after the first lift commands %.0f kJ before it" % g
    total = A.commanded_kj(bad, shape, 0.0, shape.length_m)
    if total <= 0 or b / total < A.Engine.OPENING_SHARE:
        return False, "an all-opening map is not over the threshold"
    return True, "first lift at %.0f m, %.2f MJ commanded before it" % (lift, b / 1000)


_ok, _why = _opening_energy_flagged()
check("energy commanded before the first lift is counted", _ok, _why)


# --- deployment in a corner taken flat is worth something -------------------
# It used to be worth exactly zero, because the speed ceiling pinned a flat-out
# corner to the speed the driver managed and no amount of power could move it.
# Measured on track at Austin over a controlled A/B with 2 ms of scatter, a
# megajoule through the long right-hander is worth 0.460 s and 16 kph of exit
# speed. The model need not match that, but it must not score it at zero.
def _flat_corner_pays() -> tuple[bool, str]:
    import lap as _laplib
    import profile as _prof
    import calibrate as _cal
    import carphysics as _cp
    import sim26 as _sim
    import fa26 as _fa26
    best = None
    for path in sorted(A.baseline_dir().glob("*.csv")):
        try:
            one = _laplib.load(path)
        except Exception:
            continue
        if _laplib.problem(one) or one.length_m < 3000:
            continue
        # a stretch taken flat, still gaining, and not the fastest on the lap
        shape = _prof.Profile([one])
        for d in range(400, int(one.length_m) - 600, 50):
            i0 = min(range(len(one.dist)), key=lambda k: abs(one.dist[k] - d))
            i1 = min(range(len(one.dist)), key=lambda k: abs(one.dist[k] - d - 400))
            seg = range(i0, i1)
            if not seg:
                continue
            if (min(one.gas[i] for i in seg) >= 0.99
                    and max(one.brake[i] for i in seg) < 0.01
                    and one.speed[i1] > one.speed[i0]
                    and max(one.lat_g[i] for i in seg) > 0.5 * shape.lat_max):
                best = (one, shape, float(d))
                break
        if best:
            break
    if not best:
        return True, "no flat-out corner in the telemetry, skipped"
    one, shape, d = best
    # The split has to open before the driver gets back on the power, or the
    # demand latch refuses it and the test measures the latch instead of the
    # corner. This is the tool's own advice to users, and it applies here.
    opens = d
    for x in range(int(d), 0, -5):
        slot = shape._slot(float(x))
        if shape.gas_mean[slot] < _fa26.DEMAND_RESET_GAS:
            opens = float(x)
            break
    car = _cp.load(A.car_data())
    car.fuel_liters = 45
    cal = _cal.fit_longitudinal([one], car)
    empty = [_fa26.Split(int(opens), int(opens), _fa26.CLIP_OFF, int(d) + 400, 0, 0)]
    full = [_fa26.Split(int(opens), int(d) + 400, _fa26.CLIP_OFF,
                        int(d) + 400, 350, 0)]
    pt = _fa26.Powertrain()
    out = []
    for m in (empty, full):
        r = _sim.simulate(m, car, cal, shape, pt, start_store_kj=4000.0,
                          harvest_cap_kj=9000.0, settle=False)
        at = lambda x: min(range(len(r.dist)), key=lambda k: abs(r.dist[k] - x))
        out.append(r.speed[at(d + 400)] * 3.6)
    gain = out[1] - out[0]
    if gain < 0.5:
        return False, ("350 kW from %.0f m through the flat-out corner at %.0f m "
                       "buys %.1f kph" % (opens, d, gain))
    return True, ("350 kW from %.0f m through the corner at %.0f m buys %.1f kph"
                  % (opens, d, gain))


_ok, _why = _flat_corner_pays()
check("deployment in a corner taken flat out is worth something", _ok, _why)


# --- the opening split can actually fire -----------------------------------
# A lap starts with the car already flat, so the demand it carries across the
# line is whatever the last split established, and demand cannot be raised
# under throttle. Measured on six maps at Austin: the opening delivers if and
# only if the last split of the lap asks for MORE than it. Five maps where they
# were equal deployed nothing over the first 525 m; the one where the last
# split was higher deployed 1364 kJ. A map of uniform power can never use its
# own opening, which is why every map written before this threw the first seven
# seconds of the lap away.
def _opening_can_fire() -> tuple[bool, str]:
    import fa26 as _fa26
    import optimise26 as _opt
    ds = 2.5
    flat = [_fa26.Split(0, 500, _fa26.CLIP_OFF, 500, 200, 0),
            _fa26.Split(1000, 1500, _fa26.CLIP_OFF, 1500, 200, 0),
            _fa26.Split(4000, 4500, _fa26.CLIP_OFF, 4500, 200, 0)]

    class Shape:
        length_m = 5000.0

    fixed = _opt.open_the_lap(flat, Shape())
    live = [s for s in fixed if s.deploy_kw > 0]
    if live[-1].deploy_kw <= live[0].deploy_kw:
        return False, ("a uniform map came back with final %d against opening %d"
                       % (live[-1].deploy_kw, live[0].deploy_kw))
    # and a map that already ends higher is left alone
    steep = [_fa26.Split(0, 500, _fa26.CLIP_OFF, 500, 200, 0),
             _fa26.Split(4000, 4500, _fa26.CLIP_OFF, 4500, 350, 0)]
    if _opt.open_the_lap(steep, Shape()) != steep:
        return False, "a map that already ends higher was changed anyway"
    # and one that does not start at the line is not its business
    late = [_fa26.Split(900, 1400, _fa26.CLIP_OFF, 1400, 200, 0),
            _fa26.Split(4000, 4500, _fa26.CLIP_OFF, 4500, 200, 0)]
    if _opt.open_the_lap(late, Shape()) != late:
        return False, "a map with no split at the line was changed"
    return True, ("uniform 200 kW becomes final %d, opening %d"
                  % (live[-1].deploy_kw, live[0].deploy_kw))


_ok, _why = _opening_can_fire()
check("the opening split is left able to fire", _ok, _why)


# --- the line: the opening is granted only below what was carried ------------
# The demand carried across the line is whatever the last corner established,
# and the opening split is granted only if it asks for strictly less. Every
# Austin lap with charge to spend: carried 300 or 350 against an opening of 200
# or 199 deployed 1.36-1.44 MJ; 190 against 190 deployed nothing. And below the
# 60% knee the MGU-K delivers nothing at all.
def _line_rule_holds() -> tuple[bool, str]:
    import fa26 as _fa26
    import sim26 as _sim
    import lap as _laplib
    import profile as _prof
    import calibrate as _cal
    import carphysics as _cp
    if _fa26.deploy_throttle_frac(0.5) > 0.0:
        return False, "the curve still pays out below the knee"
    one = shape = None
    lift = 0.0
    for path in sorted(A.baseline_dir().glob("*.csv")):
        try:
            cand = _laplib.load(path)
        except Exception:
            continue
        if _laplib.problem(cand) or cand.length_m < 3000:
            continue
        cand_shape = _prof.Profile([cand])
        cand_lift = A.first_lift_m(cand_shape)
        if cand_lift >= 300:          # a real run to the first corner
            one, shape, lift = cand, cand_shape, cand_lift
            break
    if one is None:
        return True, "no circuit with a run to the first corner, skipped"
    car = _cp.load(A.car_data())
    car.fuel_liters = 45
    cal = _cal.fit_longitudinal([one], car)
    end = int(shape.length_m)
    tail0 = end - 150

    def opening_mj(final_kw):
        m = [_fa26.Split(0, int(lift), _fa26.CLIP_OFF, int(lift), 200, 0),
             _fa26.Split(tail0, end - 1, _fa26.CLIP_OFF, end - 1, final_kw, 0)]
        with_opening = _sim.simulate(m, car, cal, shape, _fa26.Powertrain(),
                                     start_store_kj=4000.0, harvest_cap_kj=9000.0,
                                     settle=True)
        without = _sim.simulate(m[1:], car, cal, shape, _fa26.Powertrain(),
                                start_store_kj=4000.0, harvest_cap_kj=9000.0,
                                settle=True)
        return with_opening.deployed_mj - without.deployed_mj

    higher, equal = opening_mj(300), opening_mj(200)
    if higher < 0.3:
        return False, "an opening of 200 under a carried 300 delivered %.2f MJ" % higher
    if equal > 0.1:
        return False, "an opening of 200 under a carried 200 delivered %.2f MJ" % equal
    return True, ("200 under 300 delivers %.2f MJ, 200 under 200 delivers %.2f"
                  % (higher, equal))


_ok, _why = _line_rule_holds()
check("the opening is granted only below what was carried", _ok, _why)


# --- the driver is told a landmark, not a distance -------------------------
# "about 340 m before the line, roughly 4.2s out" is a number to read after
# the session. The two things a driver can actually do are flip it at the
# line or flip it on the way into the last corner, so the answer has to be
# one of those two and has to reach the page.
def _switch_is_actionable() -> tuple[bool, str]:
    note = snap.get("switch") or ""
    if not note:
        return False, "the solve produced no instruction for the driver"
    landmarks = ("while crossing the start/finish line",
                 "before the final corner")
    if not any(w in note for w in landmarks):
        return False, "names no landmark: " + note[:70]
    import re
    stray = re.search(r"\d+\s*m\b", note)
    if stray:
        return False, "still measured in metres: " + note[:70]
    page = (UI / "index.html").read_text(encoding="utf-8")
    script = (UI / "app.js").read_text(encoding="utf-8")
    if 'id="switch-where"' not in page or "paintSwitch" not in script:
        return False, "produced, but the page has nowhere to show it"
    return True, note[:72]


_ok, _why = _switch_is_actionable()
check("the driver is told a landmark, not a distance", _ok, _why)

print("FAILED: %s" % ", ".join(fails) if fails else "ALL PASS")
srv.shutdown()
sys.exit(1 if fails else 0)
