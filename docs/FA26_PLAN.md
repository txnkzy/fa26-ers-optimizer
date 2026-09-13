# FA26 rework plan

The FA25 tool was the proof of concept. This is the plan to build the real one
for the Formula Alpha 2026, which changes the control problem rather than just
the numbers.

## What changed in the car

| | FA25 | FA26 |
|---|---|---|
| MGU-K | 129 kW | **350 kW** |
| MGU-H | yes (Red = turbo assist) | **gone** |
| Deployment choice | 3 modes: Cyan / Red / White | **continuous 0-350 kW** |
| Harvest under throttle | no | **yes -- "super-clipping"** |
| Per-lap deploy cap | 4 MJ | none stated |
| Per-lap regen cap | 2 MJ | **7-8.5 MJ race, 4-8.5 quali**, per track |
| Store | 4 MJ | 4 MJ |
| Mass | 803 kg | 770 kg |
| Deploy limit | rpm < 12500 | **speed taper: full to ~290 kph, zero at 345** |

Losing the MGU-H removes the Cyan/Red trade that the FA25 solver was built
around. In its place come three harder things: a continuous power level, a
speed-dependent power ceiling, and rate-of-change constraints.

## Setup file layout (decoded, verified)

`setup.ini` inside `data.acd` declares 2233 items. All of them resolve against a
real saved setup, so the existing read/write machinery works unchanged.

    DEPLOYMENT_MAP_{1..12}_SPLIT_{1..24}_{1..6}

with the six fields named by VRC themselves:

| Field | Name | Range |
|---|---|---|
| 1 | Split Start | 0-50000 m |
| 2 | Deploy End | 0-50000 m |
| 3 | Super-clip Start | 0-50000 m (50000 = off) |
| 4 | Split End | 0-50000 m |
| 5 | Deploy Power | 0-350 kW |
| 6 | Super-clip Power | 0-350 kW |

Supporting items, per strat:

    STRAT_{1..12}_DEPLOYMENT_MAP        which map this strat uses
    STRAT_{1..12}_POWER_RAMP_RATE       observed 2000
    STRAT_{1..12}_BASE_REGEN_KW         observed -350
    STRAT_{1..12}_POWER_REDUCTION_STYLE observed 0
    ERS_SESSION_LIMITS, ERS_POWER_REDUCTION_RATE (observed 100)

Fourteen PU modes each select a strat (`PU_MODE_{n}_STRAT_MAP`), so the chain is
PU mode -> strat -> deployment map -> 24 splits.

## The one hard constraint

**FA26's physics Lua is obfuscated** (`script.lua`, 314 KB of base64 chunks).
Unlike FA25 there is no readable source for the energy model, and it will not be
attacked -- it is content protection on a paid mod.

This matters less than it sounds. The FA25 experience was that reading the Lua
produced energy rates about 20% wrong, and that measuring them from telemetry
was what made the tool trustworthy. FA26 simply forces that approach from the
start.

## Architecture

Split the car-specific parts out rather than growing the FA25 files, so the
working FA25 tool keeps working:

    core/
      acd.py          archive reader          (verified already works on FA26)
      setupfile.py    ID map + surgical write (generalised from stratmap.py)
      telemetry.py    lap loading, SpeedCap, drag/brake/traction calibration
      sim.py          segment integration, with a pluggable powertrain
      solver.py       DP + refinement, with a pluggable action space
    cars/
      fa25.py         3-mode energy model, HK/H/K zone layout
      fa26.py         continuous-power model, 6-field split layout
    app/
      cli.py  gui.py

## Phases

### Phase 1 -- car-agnostic refactor
Extract `setupfile.py` from `stratmap.py`; define `Powertrain` (deploy power for
a speed and commanded level, regen power, store accounting) and `ActionSpace`
(candidate actions per segment, and how an action becomes split values).
Regression-test FA25 against the numbers already recorded: Vegas replay within
0.4%, Spa within 0.61%.

### Phase 2 -- FA26 logger
New channels beyond the FA25 set: commanded and actual ERS power, store charge,
per-lap regen total, overtake and boost state.

**Risk:** FA25 handed over its per-lap counter for free via
`ac.setKERSCurrentKJ`. Whether FA26 exposes an equivalent is unknown and is the
first thing to check, because the whole calibration depends on observing energy
flow. Fallback is the car's CAN bus, as used for FA25's other channels.

Then record 3-5 clean laps at one track, ideally Monza, which already has an
FA26 setup folder.

### Phase 3 -- measure the energy model
With no readable Lua, everything is fitted from telemetry:

- deploy power against speed, to recover the 290-345 kph taper
- regen under braking, and under throttle (super-clip)
- store round-trip efficiency
- the per-lap regen cap actually enforced, and how the session setting moves it
- ramp-rate behaviour: the 100 kW/s reduction limit and the 200 kW floor

This is the step that decides whether the tool is trustworthy. Budget real time
for it.

### Phase 4 -- FA26 simulator
Longitudinal model reuses the calibrated drag, braking and traction from
Phase 1. The powertrain becomes ICE torque plus MGU-K power as a function of
speed and commanded level, minus super-clip regen, subject to:

- the speed taper
- no power increase under throttle except Boost
- reduction limited to 100 or 50 kW/s
- a 200 kW floor held for at least one second

### Phase 5 -- action space and solver
Per split the raw action is six numbers, which is too many to enumerate. Reduce:

- deploy power on a coarse grid (150 / 200 / 250 / 300 / 350 kW)
- window edges as before, in metres
- super-clip off in v1 (see below)

DP state changes: the deployed-energy dimension disappears with the deploy cap,
replaced by regen used against the per-lap harvest cap. Store charge stays.
The window search and paired-move refinement carry over directly.

### Phase 6 -- writer and validation loop
Write six values per split into `DEPLOYMENT_MAP_{n}`, touching nothing else.
Then the same drive-validate-fix loop that FA25 needed.

## Recommendation: leave super-clipping out of v1

Harvesting at full throttle is the genuinely novel part of the 2026 car and the
most interesting optimisation in it -- deliberately giving up speed now to bank
energy for a more valuable place later. It also doubles the action space and
couples the segments far more tightly than deployment alone.

Get a validated deploy-only optimiser working first, then add super-clip as v2
with the earlier version as the benchmark to beat. Trying both at once risks not
being able to tell which half is wrong.

## What could go wrong

1. **Telemetry may not expose enough.** Phase 2's risk. If neither the AC API
   nor the CAN bus reports ERS power and per-lap regen, the energy model cannot
   be calibrated and the approach needs rethinking. Check this first.
2. **Special zones.** The manual describes per-track Alternative Power, Power
   Reduction, Power Reset and Speed Threshold zones. Where these are defined is
   not yet known, and they change what a deployment map is allowed to do.
3. **The 200 kW floor may dominate.** If the car must deploy at least 200 kW for
   a second whenever the throttle opens, the achievable variation between
   strategies may be smaller than in FA25, which would cap the tool's value.
   Worth measuring early, since it affects whether this is worth building out.
