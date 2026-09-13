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
print("FAILED: %s" % ", ".join(fails) if fails else "ALL PASS")
srv.shutdown()
sys.exit(1 if fails else 0)
