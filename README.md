# FA26 ERS Deployment Optimizer

Works out where to spend battery around a circuit for the **VRC Formula Alpha
2026** in Assetto Corsa, from laps you have actually driven, and writes the
result into your setup. No deployment zones to work out by hand.

## How accurate it is

Replaying every recorded lap whose deployment map can be proven — 36 of them,
at 8 circuits — against a car fitted from the *other* laps of the same session:

| | mean | worst |
|---|---|---|
| **all 36 laps** | **1.13%** | 2.70% |
| Madrid, Monaco, Bahrain, Austin | 0.22 – 0.83% | 1.62% |
| Albert Park, Red Bull Ring, Monza, Suzuka | 1.31 – 1.71% | 2.70% |

The error scales with how fast the circuit is — it correlates with top speed at
r = +0.85 — so a single number across all of them would flatter the fast ones
and slander the slow ones. Run `python validate_model.py` for the current table
on your own laps.

Almost all of what is left is the model declining to invent speed. Given a
lap's own line it reproduces that lap to **0.69%, unbiased to +0.16%**; the
extra shows up only when it is asked to predict a lap *faster* than the session
it was fitted on. Laps slower than their session siblings come back unbiased at
−0.13%, laps faster at +1.16%. That is the behaviour you want from something
recommending deployment maps: it will not promise speed you have not shown it.

Earlier releases claimed 0.30% mean. That figure was measured against the wrong
maps — it assumed every lap was driven on STRAT 1 and picked the setup snapshot
by clock — and it does not survive checking which map the car was actually
running. `docs/MODEL_WORK_LOG.md` has the whole account.

---

## Running an unsigned executable

Windows will warn that the publisher is unknown, because the file is not
code-signed — that costs a yearly fee and this is free. So the source is
here in full, and everything below can be checked rather than taken on trust:

- **It makes no network connections.** The whole program is `fa26_app.py` and
  `core/`; the only network code in it serves the window to itself on
  `127.0.0.1`, which is why it works with the internet disconnected. Nothing
  outside that address is contacted, and the port refuses anything that is not
  this machine.
- **It writes to four places, and nowhere else.** Its own folder in
  `%LOCALAPPDATA%`, the logger inside Assetto Corsa when you press the button,
  and the setups you pick — each backed up to `.bak` first. The in-game logger
  writes lap files to `Documents\Assetto Corsa\fa26_baseline`.
- **It reads the registry, never writes it** — only to find where Steam is.
- **It collects nothing.** No analytics, no accounts, no telemetry leaving the
  machine, nothing written outside the paths above.

If you would rather not run the executable at all, `python fa26_app.py` runs
the same program from this source with Python 3.10 or newer and nothing else
installed.

## Using it

Download the release, run `FA26 Optimizer.exe`, and follow the first screen.
One file, no installer, no Python. See [READ ME FIRST](docs/READ%20ME%20FIRST.txt)
for the full walkthrough.

    drive a few clean laps  ->  run the optimizer  ->  save into a setup
                            ->  restart the session

You get three maps: **STRAT 1** qualifying, spending the whole battery over one
lap; **STRAT 2** race, ending the lap with the charge it started; **STRAT 3**
recharge, giving up lap time to put charge back in. Switch between them with
*STRAT Map* on the pit setup screen.

## Running from source

    python fa26_app.py

Needs Python 3.10 or newer and nothing else — the server is `http.server`, the
page has no framework and no build step.

    python check_app.py        # 58 checks: endpoints, a real solve, a real write
    python validate_model.py   # per-circuit accuracy, against laps you drove
    python build_exe.py        # the single-file build
    python make_release.py     # a source zip, audited for anything private

## How it works

| | |
|---|---|
| `fa26_app.py` | the application: a local server, and the page in `ui/` |
| `core/sim26.py` | the lap simulation |
| `core/optimise26.py` | the search over deployment maps |
| `core/calibrate.py` | fits drag, braking and traction from your laps |
| `core/profile.py` | pools a session into one speed and throttle profile |
| `core/recharge.py` | per-circuit recharge allowances, from FIA notices |
| `cars/fa26.py` | the car: power curves, limits, map encoding |
| `core/provenance.py` | which map a recorded lap was actually driven on |
| `logger/` | the in-game app that records laps |

`docs/FA26_FINDINGS.md` is the engineering record — what was measured, what was
tried and rejected, and why each number is what it is.

## What it does to your machine

Reads the car's physics from **your own copy** of the mod and caches them in
`%LOCALAPPDATA%\FA26 Optimizer`. Writes the logger into Assetto Corsa's
`apps\lua` when you ask it to. Writes deployment maps into setups you pick,
after backing them up. Reads lap files from
`Documents\Assetto Corsa\fa26_baseline`. It never connects to the internet.

## Versions

Every release is on the [releases page](https://github.com/txnkzy/fa26-ers-optimizer/releases);
the newest is the one to download. Versions not listed here were built and
tagged but never published.

| | |
|---|---|
| **1.1.0** | When recovery is being thrown away into a full battery, the search tries spending earlier to make room for it: Madrid 0.40 s quicker, Monaco 0.10 s, no circuit worse. Wasted recovery is only reported when the allowance would still have allowed it. Qualifying maps now spend the battery: they were leaving it unused at six of fourteen circuits, worth up to 3.0 s. The card also says when to put STRAT 1 on for the lap you are about to start. |
| **1.0.9** | Finds the recorded laps wherever Windows actually keeps Documents, and every folder the program uses can be set by hand. A lap whose distances cannot be right is refused rather than producing a map whose zones all collapse onto one point. Lap times match the game's timing screen, and the app notices when the logger inside Assetto Corsa is older than the build. No change to how the maps are calculated. |
| **1.0.8** | Found the laps on machines where Documents is redirected into OneDrive, which left the app waiting forever while the logger recorded perfectly. |
| **1.0.7** | The Assetto Corsa folder can be changed after setup, not only when the game is not found. |
| **1.0.6** | The window failed to open on the second and later runs, showing the browser's connection error instead of the page. Also: an installation sheet in the bundle, a version number in the app, a *Copy diagnostics* button, and the window itself stopped from talking to anything on the network. |
| **1.0.2** | The app reports its own version, and one button copies everything needed to answer a question about a run. |
| **1.0.1** | The search can decline the car's 200 kW floor, which makes "turn this zone off" reachable. Quicker at 8 circuits of 10 and slower at none; confirmed on track at Madrid, 0.66 s. |
| **1.0.0** | First release. |

## Licence

Free to download and use; **not** free to redistribute, fork publicly, or
modify and publish. The source is here so you can see what an unsigned
executable does before running it, not as an invitation to take it. See
[LICENSE](LICENSE) for what that allows in full, and open an issue if you
want to do something it does not.

## The car

This repository contains **no part of the VRC Formula Alpha 2026**. The car's
physics are unpacked at runtime from the copy you already own, into a cache
outside the project. `make_release.py` and `.gitignore` both enforce that, and
`check_app.py` fails the build if anything of VRC's, any telemetry, or any of
your setups would ship.

Bundled fonts are Barlow (SIL OFL 1.1) and JetBrains Mono (Apache 2.0); see
`ui/fonts/LICENSES.txt`.
