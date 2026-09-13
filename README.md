# FA26 ERS Deployment Optimizer

Works out where to spend battery around a circuit for the **VRC Formula Alpha
2026** in Assetto Corsa, from laps you have actually driven, and writes the
result into your setup. No deployment zones to work out by hand.

The simulation reproduces real laps to **0.30% mean, 0.95% worst** across nine
laps whose map provenance is proven, at Barcelona, Madrid and Suzuka.

---

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

## Licensing and the car

This repository contains **no part of the VRC Formula Alpha 2026**. The car's
physics are unpacked at runtime from the copy you already own, into a cache
outside the project. `make_release.py` and `.gitignore` both enforce that, and
`check_app.py` fails the build if anything of VRC's, any telemetry, or any of
your setups would ship.

Bundled fonts are Barlow (SIL OFL 1.1) and JetBrains Mono (Apache 2.0); see
`ui/fonts/LICENSES.txt`.
