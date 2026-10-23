# FA26 ERS Deployment Optimizer

Works out where to spend battery around a circuit for the **VRC Formula Alpha
2026** in Assetto Corsa, from laps you have actually driven, and writes the
result into your setup. No deployment zones to work out by hand.

## How accurate it is

Replaying every recorded lap whose deployment map can be proven — 36 of them,
at 8 circuits — against a car fitted from the *other* laps of the same session:

| | mean | worst |
|---|---|---|
| **all 56 laps** | **0.82%** | 2.52% |
| Monaco, Austin, Albert Park, Bahrain, Suzuka | 0.41 – 0.95% | 1.63% |
| Monza, Red Bull Ring | 1.43 – 1.46% | 2.52% |

The error scales with how fast the circuit is — it correlates with top speed at
r = +0.85 — so a single number across all of them would flatter the fast ones
and slander the slow ones. Run `python validate_model.py` for the current table
on your own laps.

Overall bias is **+0.18%** — the model is neither optimistic nor pessimistic
across the corpus. Almost all of what is left is it declining to invent speed:
given a lap's own line it reproduces that lap to **0.69%**; the
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

Windows SmartScreen will warn that the publisher is unknown, and will keep
warning: the file is not code-signed. **This is a known limitation and it is
not going away.** An Organisation Validation certificate is roughly $200–400 a
year and an Extended Validation one more, both require a registered business
identity, and since 2023 both require the key to live on hardware or in a
cloud HSM — there is no free or cheap tier, and nothing that works for a
person rather than a company. Azure Trusted Signing is the closest thing at
about $10 a month, and it too is restricted to verified organisations.

So instead the source is here in full, and everything below can be checked
rather than taken on trust:

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
| `<lap>.map` | the maps that lap ran, written beside it by the logger |
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
| **1.1.4** | **The last FA26 feature release.** The app says when a newer version is out and where to get it, and says plainly when a VRC car update has changed the setup items it relies on instead of failing part way through a save. FIA race-weekend data keeps arriving automatically until 25 December 2026, when the watch switches itself off. From here the FA26 optimizer is in maintenance. See [the release notes](docs/RELEASE_1.1.4.md). |
| **1.1.3** | **Circuit data updates itself.** Each Grand Prix's FIA Power Unit Information notice is read automatically within hours of publication and reaches every copy of the app when it starts, with no new version. All seventeen 2026 notices so far were checked against the app's figures: sectors filled in at nine circuits, Monaco's and Singapore's new Rev 1 curves added (Monaco's model error 0.21% -> 0.16%), Singapore's published limits. Clearer notes on short tracks, a warning when a plan relies on more recovery than the laps showed. See [the release notes](docs/RELEASE_1.1.3.md). |
| **1.1.2** | **Strategies for every PU mode, and a race map that keeps its power to the line.** Works with the car's v1.1 update. Saving a map now sets up the wheel's PU modes: Attack (AD1, planned for Overtake), Defend (AD2), Push (K2) and Save (ES) each get their own map and SoC Target and use the car's SoC Adaptation; RACE holds its own level with adaptation off, because on track adaptation fought it and cost time. The race map no longer runs dry before the main straight: at Sepang it had been empty by the final hairpin and crossed the line at 241 kph instead of 291, and now reaches the last sector with charge in hand. Maps avoid lifts in fast sweepers, the search runs on every core and re-climbs each answer from shaken starts, and qualifying maps are planned for the charge they really start with. See [the release notes](docs/RELEASE_1.1.2.md). |
| **1.1.1** | **Its own window, and laps you can trust.** The app opens in a window of its own on Windows' built-in WebView2 instead of an Edge window, which fixes "127.0.0.1 refused to connect" and "Cannot reach the optimizer" — both Edge passing the window between its own processes. Sepang (R16) and Baku are in from their FIA notices, and saving a map sets the whole Power & Energy Limits page to the notice — every recharge limit, the reduction rate and all four curves — while leaving the driver's Power and Energy Limits selector alone. The logger cuts laps at the game's start/finish line with the game's lap time, drops a lap the car was moved in (a hotlap restart once produced a 0:51.9 "best lap" against a real 1:07.1), and says when Windows' ransomware protection stops it saving. Model error over 85 laps is **0.62%**, from 0.75%: garage laps no longer push real laps out of a session, a lap is no longer matched to a setup saved after it, and the corner-speed ceiling takes each stretch's mean rather than its fastest instant. A Baku map from the new model was 0.16 s quicker than 1.1.0's, back to back. |
| **1.1.0** | **Three things were measured on the car rather than argued about.** Super-clipping is worth +0.936 s at Albert Park, measured best-to-best over a clip/no-clip/clip stint. Holding a quarter of the battery back every lap costs **0.99 s** across seven circuits, so the floor is gone. And deployment through a corner taken flat out is worth **0.460 s and sixteen kph** at Austin — the model scored it at zero, because a speed ceiling pinned such corners to the speed the driver managed. That is fixed, and with it the maps stop leaving a third of the battery unspent. Nothing had ever deployed on the run from the line to the first corner, on any circuit: the car arrives flat out and power demand cannot be raised under throttle, so an opening split is granted only if the last split of the lap asks for more. Every map now leaves it able to fire — seven seconds of full throttle and 2.7 MJ of capacity that used to be thrown away. Model error over 70 laps is **0.75%**, from 1.47%, and 33 of those laps carry the map the logger wrote beside them. The switch that keeps searching between laps is reachable before the first lap instead of after it, the activity log is gone, and when to put STRAT 1 on is given as a landmark you can use at speed — crossing the line, or before the final corner — instead of a distance. The logger no longer discards a lap because the one before it was invalid, cannot be replaced while the game is running, and cannot lose a lap by failing to write its sidecar. |
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
[LICENSE](LICENSE) for what that allows in full, and ask on
[Discord](https://discord.gg/E7kKCrwJ8a) if you want to do something it does
not. Questions and bug reports go there too; GitHub issues are not watched.

## The car

This repository contains **no part of the VRC Formula Alpha 2026**. The car's
physics are unpacked at runtime from the copy you already own, into a cache
outside the project. `make_release.py` and `.gitignore` both enforce that, and
`check_app.py` fails the build if anything of VRC's, any telemetry, or any of
your setups would ship.

Bundled fonts are Barlow (SIL OFL 1.1) and JetBrains Mono (Apache 2.0); see
`ui/fonts/LICENSES.txt`.
