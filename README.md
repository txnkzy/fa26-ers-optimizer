# FA26 ERS Deployment Optimizer

Works out where to spend battery around a circuit for the **VRC Formula Alpha
2026** in Assetto Corsa, from laps you have actually driven, and writes the
result into your setup. No deployment zones to work out by hand.

The simulation reproduces real laps to **0.30% mean, 0.95% worst** across nine
laps whose map provenance is proven, at Barcelona, Madrid and Suzuka.

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
| **1.1.1** | Pools only laps driven at one pace, and stops giving the race car Override power on circuits whose power curve the table does not name. Mean replay error across every recorded session falls 16%. |
| **1.1.0** | Finds the recorded laps wherever Windows actually keeps Documents, and every folder the program uses can now be set by hand. A lap whose distances cannot be right is refused rather than producing a map whose zones all collapse onto one point. Lap times match the game's timing screen. The app notices when the logger inside Assetto Corsa is older than the build. |
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
