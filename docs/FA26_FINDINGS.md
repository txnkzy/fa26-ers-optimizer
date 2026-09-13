# FA26 measured energy model

## Authoritative sources

Everything in this file is measured against one of these, not inferred.

| Source | Where | Covers |
|---|---|---|
| Car manual | `content/cars/vrc_formula_alpha_2026_csp/MANUAL.pdf` | Deployment maps p.9-11, ERS and power-unit rules p.21-27 |
| Unpacked car data | `out/fa26_unpacked/` | `setup.ini` declares 2233 setup items; the `.lut` files define the enumerations |
| FIA event notices | `data/fa26_fia_events.json`, originals under `docs/sources/` | Per-round recharge, reduction rate, power curves and the C5.12.4 / C5.12.5 / C5.12.7 / C5.2.8iii sector tables |
| — R14 Spain (Madrid) | `docs/sources/R14_Spain_Madrid_Power_Unit_Information.pdf` | Added 11 Sep 2026. **The only notice held as the original PDF**, which is what let the sector tables be decoded at all -- see below |
| Telemetry | `Documents/Assetto Corsa/fa26_baseline/*.csv` | 58 laps, 10 circuits, ~118 Hz |
| Map snapshots | `Documents/Assetto Corsa/fa26_baseline/maps/` | The map in force when a lap was recorded |
| VRC authored setups | `setups/vrc_formula_alpha_2026_csp/` | Known-good maps; Barcelona and Red Bull Ring are the two with proven provenance |

The physics `script.lua` is obfuscated and is not read.


The 2026 car's physics Lua is obfuscated, so everything here is measured from
telemetry rather than read from source. Five laps at Albert Park, ~128 Hz.

## Channels

| Channel | Verdict |
|---|---|
| `kers_charge` | **live** -- energy store, 0-1 of 4 MJ |
| `kers_current_kj` | **live** -- cumulative energy *deployed* this lap, resets at the line |
| `kers_load` | `kers_current_kj / 4000`; not an independent channel |
| `kers_input` | driver Boost/overtake request, **not** the deploy command (reads 0 while deploying 390 kW) |
| `mguk_recovery` | constant 5 -- selected regen level |
| `mguk_delivery` | constant 0 -- selected strat |
| `drs_active` | constant 0 (no DRS in 2026) |
| `kers_deployed`, `battery_charge`, `ers_power_kw`, `engine_torque` | not present on this build |

`kers_current_kj` is the important find. Integrating store loss over a lap and
comparing gives a ratio of **0.999 across three laps** with deployment ranging
from 2.1 to 7.8 MJ, so differentiating it yields exact deployment power. It is
the FA26 equivalent of the FA25's `ac.setKERSCurrentKJ`, and it counts
deployment, not harvest.

Harvest is not directly instrumented; it comes from store gains.

## Measured limits

Windowed over 0.25 s to defeat frame-timing jitter. Per-sample differentiation
gives nonsense (484 kW deploy, 430 kW harvest) and should not be used.

| Quantity | Measured | Manual |
|---|---|---|
| Max deploy power | **354 kW** | 350 kW |
| Max harvest power | **352 kW** | -350 kW |
| Typical braking harvest | ~303 kW | -- |
| Full-throttle floor below ~200 kph | **200.0 kW**, very tightly held | 200 kW minimum, held 1 s |
| Deploy per lap | up to **7.8 MJ** | no stated deploy cap |
| Harvest per lap | ~6.9 MJ | 7-8.5 MJ race, 4-8.5 quali |

The 200 kW floor is the clearest signature in the data: below about 200 kph at
full throttle the deploy power sits at exactly 200.0 kW with almost no scatter,
which is the regulation floor the manual describes.

## The taper

Full 354 kW remains available to about 330 kph, then falls -- 260 kW by
330-340 kph. That matches the **overtake** curve (ramp from ~337, zero at 355)
rather than the base curve (ramp from ~290, zero at 345), which is consistent
with these being practice laps: the manual states overtake is always active in
Practice and Qualifying.

Albert Park tops out around 331 kph, so the taper is only caught at its very
start. Measuring it properly needs a faster circuit -- Monza.

## Caveat on this data set

`DEPLOYMENT_MAP_1` is **empty** (all zeros) in the recorded setup. Everything
above is therefore the car's automatic fallback behaviour, not a strategy. That
is useful for calibration -- it exposes the floors and ceilings cleanly -- and
it also means there is a lot of headroom: the optimiser's job is to author a map
that beats the default, and the default is currently doing all the work.

## Monza session (5 laps, harvest cap set to 4 MJ)

Confirmations from a second track:

- **The per-lap harvest cap is enforced and equals the setup value.** Laps 4
  and 5 harvested 3.991 and 3.993 MJ against the 4 MJ configured. Laps that did
  not reach it harvested freely, so the cap binds rather than the car pacing
  itself toward it.
- **Deployment has no per-lap cap.** 6.15 MJ deployed on lap 4, drawing the
  store from 3.78 to 1.62 MJ on top of what was harvested.
- Strats 0-9 were cycled mid-lap, which is what produced the deployment spread
  used for fitting.

### Active aero is visible in the data

Deployment power is known exactly, so the force balance can be solved for drag:

    F_drag = F_engine + F_ers - m*a

Binning the resulting `k = F_drag / v^2` by track position gives two clean
levels rather than a spread:

| Track position | k | Mode |
|---|---|---|
| 0-600 m, 3000-3600 m | 0.57-0.63 | straight-line, low drag |
| elsewhere | 0.83-1.04 | normal |

Low-drag mode is worth roughly **30% of drag**. The two regions correspond to
the main straight and the back straight -- the 2025 DRS zones, which is what
this build of the track is wrongly using for 2026 straight-line mode.

The practical consequence is good: **the aero state does not need a telemetry
channel**. `drs_active` reads zero throughout and does not track it, but the
zones can be recovered from the drag residual, which means the simulator can
carry two drag coefficients and switch between them by track position.

The caveat is that a track with wrong zones produces a correct model *of that
track as it currently is*. Calibration should be redone if the track is fixed.

### Still not measured

Monza topped out at 317 kph here, so the deploy taper is still only caught at
its start. It needs sustained running above ~340 kph, or a track whose
straight-line zones are correct.

## Barcelona: format confirmed against a VRC-authored map

`Barcelona.ini` ships a real deployment map, which makes it possible to check
the decoded split format against what the car actually does.

### Structure

Splits **tile the lap contiguously** -- each split's end is the next one's start,
covering 0 to 4612 m. They are not isolated zones the way FA25's were, so the
optimiser is partitioning a lap rather than placing islands.

In every one of the 26 authored splits, **`clipStart == deployEnd`**. A split is
really three positions:

    start --[deploy at deploy_kW]--> deployEnd --[harvest at clip_kW]--> splitEnd

which halves the effective action space: the middle two fields are one boundary,
not two.

### Commanded versus delivered

Comparing map 1 against a lap driven on STRAT 1 (median over full-throttle
samples in each region):

| Region | cmd deploy | observed | cmd clip | observed |
|---|---|---|---|---|
| 0-601 | 250 | 249.7 | 0 | - |
| 601-803 | - | - | 350 | 340.2 |
| 803-924 | 150 | **199.2** | 0 | - |
| 924-1543 | 150 | 150.0 | 0 | - |
| 1543-1671 | - | - | 250 | 250.9 |
| 1671-2068 | 150 | 151.2 | 100 | 100.4 |
| 2068-2376 | 200 | 199.9 | 0 | - |
| 2376-2501 | - | - | 200 | 200.7 |
| 2501-3321 | 250 | 249.9 | 0 | - |
| 3321-3445 | - | - | 350 | 328.6 |
| 3445-3811 | 0 | **158.5** | 0 | - |
| 3811-4243 | 200 | 199.9 | 0 | - |
| 4243-4612 | 250 | 249.8 | 0 | - |

Eleven of thirteen match the commanded value within 0.3 kW, so **deploy and
super-clip power are delivered as commanded** and the simulator can treat the
map as authoritative.

The two exceptions are the regulation constraints, measured rather than assumed:

- **803-924 commanded 150, delivered 199.2** -- the 200 kW floor after getting
  back on the throttle overrides a lower commanded value.
- **3445-3811 commanded 0, delivered 158.5** -- power cannot cut instantly, so
  the ramp-down and floor keep it deploying through a split asking for nothing.

Both matter for the optimiser: a plan that commands less than 200 kW at throttle
application, or an abrupt cut, will not be obeyed, and the solver has to model
that or it will systematically overestimate how much energy a plan saves.

## Plan correction: super-clipping belongs in v1

The FA26 plan proposed deferring super-clip to v2. The authored maps show that
is wrong. VRC uses it heavily -- several splits are pure harvest at 250-350 kW
with zero deploy -- and with harvest capped per lap and deployment uncapped,
**harvesting is the constraint that governs how much can be deployed**. A
deploy-only optimiser would be unable to generate the energy its own plan needs
and would be beaten by the shipped maps.

## Simulator, validated against the authored Barcelona map

Replaying VRC's map 1 through the model, against the lap it was driven on:

| | Modelled | Measured |
|---|---|---|
| Lap time | 75.67 s | 76.88 s (**-1.6%**) |
| Deployed | 7.56 MJ | 7.67 MJ |
| Harvested | 7.56 MJ | 7.85 MJ |
| Store at the line | 2513 kJ | 2592 kJ |
| Store minimum | 964 kJ | never empties |

The energy figures are the meaningful test: the store ends within 3% after a
full lap of deploying and harvesting, so the flows are right rather than merely
summing to the right total. Lap time remains optimistic by the usual margin --
the model drives the recorded line perfectly and brakes at the calibrated limit.

Step size converges: 1 to 10 m moves lap time by 0.24 s and the totals by under
2%. The default is 5 m.

### Three modelling errors found by validating

Each of these looked plausible and was wrong:

1. **Store limiter in the wrong units.** The cap on drawing from the store was
   written `store * 3600`, mixing kJ with kW, so it never bound. The model
   reported balanced deploy and harvest totals while the store drained to zero.
2. **Deploying and harvesting in the same step.** A split's super-clip section
   returns a deploy command of zero, and the 200 kW floor was then applied to
   that zero while the clip harvest also ran. The car was modelled doing both
   at once.
3. **Harvesting only under braking.** Measured by throttle state, coasting is
   the largest single source of energy on a lap -- 3.9 MJ against braking's
   1.5 MJ -- because the car regenerates at about 336 kW whenever the throttle
   is closed, not only when the brake is applied. Missing it left the model two
   thirds short on harvest.

### The 200 kW floor is transient, not permanent

Holding the floor everywhere at full throttle made the car deploy 8.19 MJ
against a measured 7.67. The regulation requires 200 kW to be *held for about a
second* after the throttle opens, after which power may fall at the ramp rate.
Modelling it that way, with a 100 kW/s ramp-down limit matching
`ERS_POWER_REDUCTION_RATE`, reproduces both the totals and the 158.5 kW
mid-ramp reading seen where the map commands zero.

### Performance

A whole lap simulates in about 10 ms, roughly four orders of magnitude faster
than the FA25 per-segment approach. The optimiser can therefore evaluate whole
candidate maps directly rather than precomputing a table of segment outcomes.

## Reference corpus: six VRC-authored maps

`data/fa26_reference_maps.json` holds the deployment maps VRC ships for
Barcelona, Australia, Austria, Belgium, Great Britain and Miami -- 71 splits in
total. `data/fa26_recharge_vrc.json` holds their recharge allocations.

Structure confirmed across all six:

- splits **tile the lap contiguously** in every map, with no gaps
- lap lengths match the real circuits (Australia 5240 m, Austria 4298,
  Belgium 6944, Great Britain 5803, Miami 5351)
- every setup defines two maps, so a strat pair rather than a single strategy
- super-clip is used in 3 to 6 splits per map, confirming it belongs in v1

Power values used: 0, 50, 100, 110, 150, 200, 250, 300, 350 kW. Not a coarse
grid -- 110 kW appears at Spa -- so the optimiser's power steps should not be
too widely spaced.

### The four split positions are independent

69 of 71 splits have `clipStart == deployEnd`, which invites collapsing them
into three positions. Two Albert Park splits show that would be wrong:

    start 1607  deployEnd 1607  clipStart 1708  end 1817   deploy   0  clip 250
    start 3802  deployEnd 3802  clipStart 3940  end 4111   deploy 350  clip 150

Both leave a deliberate neutral gap -- coasting before harvesting, and coasting
after deploying at full power. The gap is a control VRC uses, so it stays.

### Recharge: published table versus VRC

| Circuit | VRC qualify | Published | |
|---|---|---|---|
| Albert Park | 7.0 | 7.0 | match |
| Red Bull Ring | 6.0 | 6.0 | match |
| Miami | 8.0 | 8.0 | match |
| Barcelona | 7.0 | 7.0 | match |
| Spa | 7.0 | 8.0 | differs |
| Silverstone | 6.5 | 7.5 | differs |

Four of six agree exactly. Spa and Silverstone are both 1.0 MJ lower in VRC's
setups, and those same two also carry higher race values (8.5 rather than 8.0),
so they look like a different authoring revision rather than a deliberate
choice.

The solver therefore reads the cap from **the setup file being optimised**,
which is what the game will actually enforce, and uses the published table only
to report when that value looks wrong.

---

# Model rebuild, 11 September 2026

Audited against the manual, the FIA event notices and 47 recorded laps, then
rebuilt. Everything below was measured, not reasoned from the code.

## Validation set: the only laps whose map is proven

A replay is only evidence if the car was actually running the map being
replayed. Scored as mean |effective command - delivered| kW over every
full-throttle sample, against the same lap measured on an empty map:

| Circuit | Map | Laps | Fit | Skill over an empty map |
|---|---|---|---|---|
| Barcelona | VRC `Barcelona.ini` map 1 | 7 | 13-38 kW | +77 to +91% |
| Red Bull Ring | VRC `Austria.ini` map 1 | 2 | 8.7-8.9 kW | **+93 to +94%** |
| Monza | anything on disk | 0 | best 29 kW | +79%, and 34 of 44 laps score +0% |

**No Monza lap on disk is a valid replay case.** `monza.ini` is overwritten by
this tool, and its mtime is *after* the laps being replayed against it, so the
old "+3.1% race / +13.7% recharge" figures compared a simulated map against a
lap driven on a different one. `fa26.write_strategies(telemetry_dir=...)` now
keeps a dated snapshot beside the telemetry, and `fa26.snapshot_for()` finds
the one in force for a given lap. Monza stays unvalidated until laps are
recorded with a snapshot.

Red Bull Ring is the better reference of the two and was not being used at all.

## Where the model stands

Nine laps, both circuits, VRC's own maps:

| | before | after |
|---|---|---|
| mean abs lap error | 0.82% | **0.30%** |
| worst lap | 2.51% | **0.95%** |
| mean deploy error | -1.57 MJ | +0.52 MJ |
| harvest model, per-lap cap lifted | -1.62 MJ | **+0.04 MJ** |
| illegal power increases per written map | 4-8 | **0** |

## What was wrong

1. **The drag fit absorbed full-throttle harvest into the aero coefficient.**
   `drag_samples` had a term for deployment and none for regen, so super-clip
   retarding force -- present in 28% of Monza's samples, p90 202 kW -- was
   attributed to aerodynamics. Monza's median k was 0.898 against a true 0.623,
   and the inflated bins are exactly where the sim lost time. A measured-power
   replay went +2.31% -> -0.13%. It also double-counted, since `sim26` applies
   the regen force again, so every super-clip decision was mispriced.

2. **The no-increase rule latched delivered power, not demand.** Delivery is
   already cut by throttle, taper, store and the expiry of the 200 kW floor, so
   the latch ratcheted: once it touched zero it could never rise until the
   driver lifted. 393 of 964 full-throttle steps on a Monza lap delivered
   nothing against a live command -- 7.04 MJ discarded, dead stretches up to
   530 m. It also fired on Barcelona laps 1, 2 and 7 (deploy 4.2-6.3 MJ against
   7.6-8.4 measured); only lap 6, the one being validated against, was clean.

3. **The demand reset only on a full lift.** On Barcelona lap 7 at 4231 m the
   driver lifts to 0.35 throttle -- never below any "off throttle" test -- and
   the car immediately re-establishes demand and goes to 251 kW. The reset
   belongs at the arbitration knee (`fa26.DEMAND_RESET_GAS`, 0.60), which is
   also where harvest stops and where the manual puts torque arbitration.

4. **`seed_splits` cut at the 0.99 throttle crossing**, above the knee, so every
   seed boundary was an illegal power increase. Once candidates are held to
   what the car can execute that capped whole maps at zero. Cutting at the knee
   is what the manual advises (p.9) and was worth 2.2 s on a Monza solve.

5. **The ceiling carried a 2% margin** over measured corner speed, in a module
   whose own docstring says the measured speed *is* the ceiling. Removing it
   took the bias from -1.28% to -0.04%.

6. **5 m was not a converged step, it was a compensating one.** The model
   converges to about -1.3% optimism, and at 5 m discretisation error cancelled
   most of it; at 7.5-10 m it over-cancelled into a false +0.2%. That
   cancellation is a property of one track and map shape. Now 2.5 m throughout.

7. **`ERS_POWER_REDUCTION_RATE` does not govern delivered power.** Measured from
   the raw counter over 243 full-throttle reductions above 210 kph, the car
   falls at 100-215 kW/s with a hard floor at 101 -- including at Monza, whose
   setups were written with the item at 50. The rate is still written to the
   car; the model uses the measured 100 kW/s. Applying it *after* throttle
   scaling was also wrong -- lifting is not a demand reduction -- and cost
   0.57 MJ a lap.

8. **Harvest was credited between 0.6 and 0.99 throttle**, where the car
   generates nothing (measured: zero by 0.68). Worth 0.67 MJ a lap at
   Barcelona, 9% of modelled harvest -- energy the plan could never bank.
   `HARVEST_OFF_THROTTLE_KW` was swept and left at 336; it was right. The
   cutoff was not: the real knee is 60 kph, not 80.

9. Smaller: `map_command_at` harvested through the neutral coast gap;
   `Powertrain.overtake` was a dead field; the fit metric could not fail;
   `validate26` inherited class defaults instead of the event's parameters.

## Open, and what to watch on the next run

- **Deploy runs +0.52 MJ a lap high** on authored maps while harvest is right,
  so a settled lap is not energy-neutral. Settling to a fixed point instead of
  one lap was tried and is worse -- the model converges onto an empty store
  (1.18% mean error against 0.30%) and then limit-cycles. The overshoot is the
  thing to fix; it is part-throttle, and neither the floor parameters, the
  reset knee, the throttle gate nor the part-throttle curve move it.
- **Reset zones are entered by hand.** The notice text interleaves the C5.12.4,
  C5.12.5, C5.12.7 and C5.2.8iii tables with no delimiter and the column order
  varies by round, so which article a window belongs to cannot be recovered
  automatically. Only Monza's 2100-2800 is entered, because both columns name
  the same window. `recharge.candidate_zones()` lists the rest for
  transcription against the notice.
- **Two unexplained full-throttle power rises at Monza**, 4100 m and 4500 m,
  with `kers_input` at 0 and no strat change. The other five clusters are Boost.

## Solve time, same day

A Monza run of all three strategies took about eleven minutes. Two causes,
both fixed; it is now **38 seconds**, and the maps are unchanged.

**The search was reseeding from its own previous answer.** `_reference_maps`
scanned every `.ini` in the track's setup folder for extra starting points, and
on Monza that meant the three maps the tool itself wrote last time -- so
`optimise()` ran four full hill climbs per strategy instead of one, most of it
re-deriving its own output. An extra seed is worth having when the map really
was authored elsewhere (VRC's Spa map simulated 0.85 s faster than the solver
reached alone), and worthless when it is ours. A file this tool has written is
identifiable: `write_values` leaves a `.bak` holding the pre-write content, so
where a `.bak` exists the `.bak` is the authored original and the `.ini` is our
own. The shipped corpus in `fa26_reference_maps.json` is preferred, the cap is
2, and the GUI now says how many climbs it is about to run. Monza has no
authored map, so it runs one climb; Barcelona has one, so it runs three.

**Position-dependent work was being redone inside every evaluation.** The
speed ceiling, throttle classification, grip left under lateral load, drag
coefficient and both throttle curves are functions of track position alone, and
a Monza race solve spent 63 of its 64 seconds inside `simulate` recomputing
them a thousand times over. They are now built once and cached on the profile,
keyed by step size and zones and checked against the calibration object's
identity so a refit cannot be served a stale ceiling; the map's commands are
read once per solve rather than once per lap of the settle-and-measure pair.
Worth 2.2x. Verified bit-identical against the previous implementation over 180
comparisons spanning both circuits, five maps each, both settle modes and both
step sizes -- lap time, all four energy totals and the full speed trace.

Hoisting attribute lookups out of the inner loop on top of that was worth
almost nothing (8.1 -> 7.6 ms), so what remains is the loop arithmetic itself.
The next real lever would be running `Evaluator.many()` across cores.

---

# Madrid added, and the sector tables finally decoded

The R14 Spain notice arrived as the original PDF rather than as extracted text,
and that settled a question the text dumps could not.

## Which column is which article

`exceptions_raw` interleaves four sector tables with no delimiter, so the
earlier note here said the article a window belongs to "cannot be recovered
automatically". With the PDF, the word coordinates say it outright:

| x | Article | Madrid |
|---|---|---|
| 114 | C5.2.8iii, alternative power curve, Sprint & Race | 1500-5100 |
| 297 | C5.12.4, power reduction over 150 kW permitted | 3600-3800, 4100-4300, 4600-4800, [5100-5300] |
| 479 | **C5.12.5, reset of MGU-K power reduction** | **[5200-5400]** |

The extracted text preserves that left-to-right order, so the run of "350kW"
values -- the "maximum PU power reduction permitted" column, which only C5.12.4
has -- marks the end of the middle table, and the windows after it are the
reset zones. Two traps: the C5.12.4 *heading* contains "greater than 150kW",
and the rate limit reads "100 kW/s", so anchoring on the longest run of
consecutive kW values rather than the last one is what makes it work. The
parser reproduces the coordinate-derived answer for Madrid exactly, and
independently reproduces Barcelona's three 260 km/h speed-threshold windows
that had been transcribed by hand.

**Monza's reset zone was wrong here.** It was entered as 2100-2800, which is
actually its C5.12.4 and C5.2.8iii window; the reset zone is [5300-5800]. The
telemetry never supported 2100-2800 either -- the 2,053 power rises in that bin
were partial-throttle pickup, median throttle 0.57, not resets.

**Every reset zone published so far is bracketed as SQ and Q only.** So race
and recharge maps get none, and `Allocation.reset_zones_for(session)` applies
the scope. A race map had been allowed a power increase the race car refuses.

## Two bugs Madrid exposed

**The track never resolved.** `normalise("madrid_street_circuit_2026")` gives
"madridstreetcircuit", which matched no table entry, so every Madrid lap
silently got the generic baseline allocation. The published figures were also
wrong where they existed -- qualify 9.0 against the notice's 7.5.

**The seed dropped the end of the lap.** `seed_splits` tiled at a fixed
granularity and returned `splits[:24]`. Madrid wants 29 features against the
car's 24 map slots, so the last **778 m -- 15% of the circuit, including the
run to the line -- carried no command at all**, and the solver's own qualifying
map came out 2.6 s slower than the lap the driver had driven on the same track.
It now raises the merge length until the tiling fits, keeping full coverage and
spending slots on the longest features. Worth 0.83 s at Madrid and nothing
anywhere else, because Madrid is the only circuit recorded where the budget
binds.

## Zone positions are scaled

Windows are quoted against the notice's centreline, and the AC spline rarely
agrees -- Madrid is 5416 m published against 5340 m measured, Monza 5793
against 5750. A window near the line lands up to 80 m out if used as published,
so `reset_zones_for`/`speed_zones_for` take the lap length and scale.

## Madrid, as configured

Recharge 8.5 MJ race / 9.0 with Overtake / 7.5 qualifying / 9.0 practice and
out laps. Reduction rate 100 kW/s written to the car. Power-limited distance
3206 m. Curves Base - Standard, Base - Overtake, Alt 1. Reset [5200-5400] in
qualifying only, none in the race. No speed-threshold sectors.

Physics checks out independently of any map: replaying three of the four
195624 laps with their own measured ERS trace gives +0.33%, +0.60% and +0.65%.
A full three-strategy solve takes 78 s.

---

# Modelling a session instead of a lap

A driver is not a robot, and fitting one lap treats one draw from a noisy
process as the truth. Measured, that is exactly what was happening.

## The overfitting, measured

Build a qualifying map from the single best lap of a session, then check
whether the car could execute it on each of the *other* laps of that same
session:

| Map built from | Power steps the car refuses, per lap |
|---|---|
| Single best lap | Madrid `0 3 4 5 4 4 4`, Barcelona `0 3 4 4 4 3 1 3` |

Zero on the lap it was fitted to, three to five on every other lap. The map is
tuned to that lap's throttle noise. The consequence is that the lap time the
tool reports is fiction: on Barcelona it claims 76.476 s and delivers 78.391 s
averaged over the session -- **1.9 s of optimism** that never shows up as a
warning.

The cause is where boundaries land. A slot's throttle classification flips
between laps on 6-18% of a lap, contested zones are 8-10 m wide, and split
boundaries land almost entirely inside them -- 19 of 21 at Barcelona, 22 of 23
at Madrid -- because boundaries *are* throttle transitions and transitions are
where a human varies.

## What shipped

`core/session.py` groups laps by the session stamp already in the filename, so
"the laps I just drove here" needs no input and cannot mix circuits. Laps
within 3% of the session best are kept, with a fallback to the single best lap
when a session is too short or too scattered. `calibrate.fit_longitudinal` and
`Profile` both take a lap or a list; given one lap they are bit-identical to
before.

The aggregation rules differ by quantity, deliberately:

- **Drag, braking, traction** pool outright. They are car properties, and one
  lap does not fill the bins: Madrid fits 53 of 107 drag bins from one lap and
  67 from seven, Monaco 33% against 48%.
- **The speed ceiling takes the median.** Taking the minimum -- the obvious
  generalisation of what a single lap already does across its own samples -- is
  a disaster, because across laps it picks the slowest lap everywhere and
  compounds into a lap nobody drove: 3.15% error against 0.50%.
- **Throttle and brake state average**, and `Profile.consensus` records the
  fraction of laps on the power at each slot.

Fuel now comes from the telemetry too. The box defaulted to 45 L while a Monza
push lap ran at 6.9 -- 28 kg out. It barely moves the replay, because the drag
fit absorbs whatever mass it was fitted with, but it is one less thing to type.

| | one lap | session |
|---|---|---|
| Out-of-sample error, leave-one-out | 0.57% | **0.44%** |
| Worst lap | 1.59% | **0.98%** |
| Claimed vs achieved, Barcelona | 1.92 s | **1.07 s** |
| Achieved lap time, Barcelona / Madrid | 78.391 / 100.934 | **78.224 / 100.839** |

## What did not ship, and why

Placing boundaries at the *earliest* pickup across a session rather than the
typical one, so that no lap ever has a step refused. The argument for it was
strong: the asymmetry is real and measured -- shifting VRC's Barcelona map 20 m
early is free and slightly quicker, 20 m late costs 0.68 s and 1.25 MJ, because
the car refuses the whole step rather than taking it late.

It does not pay. Holding the pooled profile and calibration fixed and varying
only the boundary rule, on lap time achieved across the session:

| Boundary rule | Madrid | Barcelona | Steps refused |
|---|---|---|---|
| Typical pickup | **100.839** | 78.224 | 13-21 |
| Earliest fifth | 101.085 | **78.047** | 13-16 |
| Near-unanimous | 102.133 | 79.845 | **0** |

Driving the refusals to zero costs more than the refusals do: moving every
boundary early also moves the power delivery early, into where it is worth
less. The middle setting is a wash -- better at Barcelona, worse at Madrid --
so the typical pickup stands and the idea is recorded here rather than shipped.

---

# Cold start: what the car knows before the first lap

The car's own deployment map really is inert. Every split of
`DEPLOYMENT_MAP_1` defaults to `start=0 deployEnd=0 clipStart=50000 end=0`, so
with no setup loaded there is no map at all and the car runs its automatic
fallback -- the 200 kW floor at throttle application and nothing else.

**VRC ships authored maps for eight circuits** as Optional Setups: Australia,
Austria, Barcelona, Belgium, Great Britain, Miami, Monza and Suzuka. The
reference corpus held six of them and was missing **Monza and Suzuka** -- the
most-driven track in this project had a VRC-authored map the solver never saw.
Adding it is worth **0.62 s** immediately as an extra search seed (84.513
against 85.133 on the latest Monza session). VRC names those folders by
country, so `belgium` and `greatbritain` now alias onto `spa` and
`silverstone` rather than duplicating them.

## The bootstrapping penalty is real, and proportional to the deploy change

Calibrating on laps driven with little deployment and then running a map that
deploys properly takes the car outside the speeds the drag fit ever saw.
Measured by calibrating on one session and predicting another with the
measured-power replay:

| | slow session | fast session | predicting the fast laps |
|---|---|---|---|
| Monza | 2.74 MJ, vmax 317 | 7.04 MJ, vmax 333 | **+1.75%** vs +1.09% fitted on itself |
| Madrid | 6.31 MJ, vmax 294 | 5.75 MJ, vmax 320 | +0.55% vs +0.60% -- no penalty |

So it is not about lap time, it is about the deploy level and therefore the top
speed: Monza's drag fit saw p95 294 kph and the final map runs at 333. Madrid's
two sessions deployed about the same and the penalty vanishes. The cost is
roughly 0.6 s at Monza, and it is self-correcting -- drive the map, re-solve.

## What a true zero-lap map would need

Nothing position-dependent exists before the first lap: no speed ceiling, no
drag, no throttle profile. Two routes rather than three:

- **VRC's authored map** where one exists. That is the educated guess, made by
  people who know the car, and it now covers eight circuits.
- **The track's AI line.** `content/tracks/<track>/ai/fast_lane.ai` is present
  for every track here and carries a detail block with per-point speed, gas and
  brake -- structurally the same inputs `Profile` takes. It would give a cold
  start anywhere, at the cost of modelling the AI's line rather than the
  driver's. Not attempted.

## Cold start, built

`core/ailine.py` reads `content/tracks/<track>/ai/fast_lane.ai` -- version 7,
little-endian: a header, the ideal line at 20 bytes a point, the count
repeated, then 72-byte detail records carrying speed, throttle and brake.
Several files append further blocks, which are ignored.

The line is only as good as whoever authored it, and several stock tracks ship
lines topping out near 100 kph -- Monza, Barcelona, Red Bull Ring and Spa among
them. Using one of those would silently author a map for a car that does not
exist, so a line has to clear 250 kph and 15% full throttle before it is
accepted, and is refused by name otherwise. Those circuits are exactly the ones
VRC ships authored maps for, so the two cold starts complement each other.

Evaluated properly -- build a Madrid qualifying map, then simulate it against
each of the seven real laps:

| Map | Achieved | Worst lap |
|---|---|---|
| No map, the car's default | 104.034 | 104.595 |
| **Cold start from the AI line, no laps at all** | **100.910** | 101.460 |
| Built from the seven real laps | 100.839 | 101.355 |

Within 0.07 s of the map built from real telemetry, and 3.1 s better than
driving with nothing. Its *claimed* time is not to be believed -- 97.930
against 100.910 achieved -- because an AI line carries no ERS trace, so the
force balance attributes none of the car's thrust to deployment and the fitted
drag comes out low. The GUI says so.

## Telling the user when to solve again

`Longitudinal` now records the speed band the drag fit actually saw, and the
GUI reports when a finished map runs the car past it. It fires exactly where
intended: calibrated on the early Monza laps (2.7 MJ deployed, fit to p95
295 kph) a solved map runs to 340, so it asks for another run; calibrated on
the later laps (7.0 MJ, p95 322) the same solve stays inside the band and it
says nothing.

---

# Spa: where the human map was quicker

A lap on VRC's own Spa setup came back 0.3 s faster than one on the solver's.
Only the VRC lap was recorded, and the two runs used different car setups as
well as different maps, so the on-track comparison cannot be attributed. What
can be compared is the maps, replayed against the same lap with the same
calibration -- and the solver had seeded from VRC's map, so the split
boundaries are identical and only the power assignment differs.

## Two causes, both real

**The seeds were duplicates.** `_reference_maps` collects the shipped corpus
entry and then everything in the track's setup folder, capped at two. For Spa
the corpus entry is byte-identical to `Belgium.ini` map 1 -- they come from the
same file -- so both slots went to the same map and **VRC's map 2 was dropped**.
That is the faster of the two. De-duplicating by shape and lifting the cap to
three recovers it, worth **0.20 s**: the solve goes from 108.460 to 108.263 at
a full store.

**The qualifying map assumed a full battery.** `starting_store_kj` returns the
full 4 MJ for a qualifying lap, because a qualifying lap is meant to follow a
recharge lap. The recorded Spa lap started at 3123 kJ -- 78%. Both maps lose
time when the store is short, but not equally:

| Map | @4000 | @3500 | @3123 | @2500 | spread |
|---|---|---|---|---|---|
| Solver, best case only | 108.263 | 108.570 | 108.831 | 109.327 | 1.063 |
| VRC Belgium map 2 (human) | 108.421 | 108.511 | 108.718 | 109.143 | 0.722 |
| **Solver, scored across charges** | **108.395** | **108.395** | **108.474** | **108.876** | **0.481** |

## The philosophy worth taking

VRC's map is not quicker in the best case -- the solver beats it there once the
seeds are fixed. It is quicker *when the battery is not full*, because it does
not stake everything on arriving with 4 MJ. It harvests on the pit straight and
into the Bus Stop, where the car is already fast and deployment buys little
force, and spends on Kemmel where the car is accelerating hard out of Eau Rouge
and the same kilowatt is worth far more.

That robustness is expressible as an objective rather than as a rule of thumb:
`Objective.charge_fractions` scores a candidate at the planned charge *and* at
three-quarters of it, and optimises the mean. It costs one extra simulation per
candidate. The resulting map gives up 0.13 s at a full store and takes back
0.36 s at the charge the lap actually started with -- **beating the human map
at every charge level tested**, and halving the sensitivity that made the
original worse than VRC's in the first place.

## Track names: nine circuits were falling through to the baseline

A Suzuka solve reported the circuit as not on the published allocation list,
despite Suzuka being round 3 in the event table. `normalise` strips a fixed
list of folder prefixes -- `fn_`, `rj_`, `acu_`, `csp_`, `ks_` -- and the
folder is `rt_suzuka`, so it resolved to `rtsuzuka` and matched nothing. It was
not alone. Nine circuits on the 2026 calendar were silently taking the generic
baseline instead of their published figures:

| Folder | Resolved to | Should be |
|---|---|---|
| `rt_suzuka` | rtsuzuka | suzuka |
| `rt_hungaroring` | rthungaroring | hungaroring |
| `miami_f1` | miamif | miami |
| `monaco_2019_CHQ` | monacochq | monaco |
| `shanghai_v2`, `shanghai_v2_25` | shanghaiv | shanghai |
| `jeddah_2021_chq` | jeddahchq | jeddah |
| `vhe_interlagos` | vheinterlagos | interlagos |
| `vrc_mexico` | vrcmexico | mexico |

Suzuka was planning to the 7.0 MJ fallback against a published 8.0, and
Hungaroring to 7.0 against 9.0 -- a whole extra megajoule a lap unspent.

Chasing prefixes one at a time never keeps up, since each is an author's
initials or a layout tag. `normalise` now falls back to the longest circuit
name *contained* in the folder name, which needs no list to maintain. Names
shorter than five characters are excluded from that fallback -- `spa` and
`baku` appear inside unrelated words, and both already resolve exactly or
through an alias. Checked across all 93 track folders installed: 41 resolve,
every one correctly, and the other 52 are genuinely not on the calendar.

One thing left alone: `ks_monza66` and `ks_silverstone1967` resolve to the
modern circuits, since stripping digits has always done that. They are
different layouts sharing a name, and the allocation belongs to the event
rather than the tarmac, so the modern figure is the defensible default.

---

# Suzuka: the first properly-provenanced comparison

Applied before driving for once, so both laps are attributable. Lap 6 followed
VRC's shipped Suzuka map at **+91% skill** -- a clean validation case, the third
after Barcelona and Red Bull Ring. Lap 5 followed the solver's map at +71%, and
that shortfall turned out to be the finding.

| | solver map (lap 5) | VRC map (lap 6) |
|---|---|---|
| Lap | **1:30.426** | 1:31.471 |
| Deployed / harvested | 9.51 / 7.18 MJ | 8.83 / 8.97 MJ |
| Store | 3018 -> 684 (spent) | 2793 -> 2924 (neutral) |
| Commanded vs delivered | **-9%** | +2% |
| Model error on its own lap | +1.16% | **+0.02%** |

## The bug: the lap-wrap boundary was never checked

`illegal_increases` and `make_executable` both opened with `if d <= 0:
continue`, so the split that starts the lap was never compared against the one
that ends it. Where the start/finish line sits on a straight the driver crosses
it flat, the demand carries over from the previous lap, and a step up there is
refused like any other mid-throttle increase.

Suzuka is exactly that case and the telemetry is unambiguous. The solver's map
commanded **200 kW before the line and 350 after**; the car held **100 kW** for
the whole 317 m. VRC's map commands **250 on both sides** and the car delivers
250. By the end of the straight VRC was doing **314 kph against 297**.

Fixed by wrapping the comparison. Re-solving Suzuka, the search now arrives at
**250 kW on both sides of the line by itself** -- the same answer VRC reached by
hand. Replayed at the charge the lap actually started with, the new map is
**90.922** against 91.476 for the map that was driven and 91.581 for VRC's, and
it is flat across starting charge (0.03 s spread against 0.69).

## Where the second was actually won and lost

Segment times, solver map minus VRC map, negative meaning the solver's was
quicker:

    0-600 m     +0.356   the refused straight
    1800-2400   +0.301
    1200-1800   -0.178
    2400-3000   -0.282
    3000-3600   -0.142
    4200-4800   -0.512
    4800-5400   -0.377
    5400-6000   -0.323
    total       -1.045

The solver won the last third of the lap decisively and gave a third of a
second back on the one straight it got wrong. Fixing the wrap keeps the former
and removes the latter.

## What the model still gets wrong

Replayed against the lap driven on it, VRC's map predicts to **+0.02%** while
the solver's predicts to +1.16% -- pessimistic by 1.05 s. The difference is the
refused region: the model gives roughly nothing there, and the car held a
steady **100.0 kW** for 300 m. That is suspiciously exactly the figure the
manual names as the threshold below which power may cut out at once, so the
hypothesis is that a refused increase settles at 100 kW rather than collapsing.
One observation is not enough to model it; it wants a deliberate test.

## Correction, and the fix that followed

The note above said the model "gives roughly nothing" in the refused region.
That was wrong and not checked before it was written. Traced, the model awards
the **full 350 kW** across the line -- it is optimistic there, not pessimistic.

The cause is worth more than the 100 kW question. `simulate` began every lap
with `demand_kw = 0` and `throttle_time = 0`, as though the car arrived at the
line freshly picking up the throttle. It does not: where the line sits on a
straight the driver crosses it flat, having been flat for some time, so the
demand is already latched at whatever the last split asked for. And because a
qualifying solve runs one-shot, with no settling lap to carry state over, the
search never saw the refusal at all -- which is how a 350 kW command came to be
written into a split the car answers with 100.

`simulate` now walks the last 600 m of the lap before the measured pass,
tracking demand, level and throttle time only, touching neither the store nor
any total. The Suzuka lap it mispredicted goes from **+1.16% to +0.83%**, VRC's
stays at +0.02%, and the nine-lap regression is unmoved at 0.30% / 0.95%.

The 100 kW question stays open, deliberately. Scanning every lap whose map is
known produced exactly **one** instance of a refused increase at sustained full
throttle -- the Suzuka one. A model parameter fitted to a single observation is
not a model, so nothing was changed for it. What would settle it is a map with
a deliberate step up on a long straight, driven once.

---

# Workflow guards

Three things the tool now catches, all of them mistakes made repeatedly while
building it.

**Applying after driving.** `session.map_order_warning` compares each lap's
save time against the map snapshots written for that circuit, and says so
before solving. Two subtleties made the naive version wrong. The stamp in a
filename belongs to the *session*, not the lap, so a session spanning an apply
would be condemned whole -- Suzuka opened at 11:22, the map was written at
11:43, and the two laps that mattered were saved at 11:49 and 11:54. File
modification time gives the save time per lap instead. And the car reads a
setup when it loads, so a file written mid-session may not reach it until a
restart; a partial overlap is therefore reported as something to check rather
than as a verdict. Snapshots are matched to a circuit through
`recharge.normalise`, which lands `suzuka.ini` and `rt_suzuka` on the same key.

Against the real telemetry: the Monza session is told plainly that all eight
laps predate the map now in the setup; Madrid is told that one of seven does
and to check whether the session was restarted; Suzuka, Barcelona and Red Bull
Ring pass clean.

**Planning a qualifying map on a flat battery.** A qualifying lap is planned
for a full store because it is meant to follow a recharge lap. The Suzuka laps
began at 75% and the Spa lap at 78%, which cost 0.48 s there. The solver now
says so before it plans.

**Not knowing which strategy is live.** Applying now states which STRAT is
selected, in the log and in the dialog, alongside the existing reminders that
the pit screen changes it and that the session must be restarted.

One more, found while testing: picking a lap outside 3% of the session best
silently substituted the best lap instead. It still does -- that is the right
behaviour -- but it now says which lap it used and why.

## One file

`build_exe.py` freezes the whole thing with PyInstaller into
`E:\FA26 Optimizer\FA26 Optimizer.exe` -- 8.6 MB, no installer, no Python on
the machine that runs it. The page, its fonts, the recharge tables and the
in-game logger all travel inside; `make_icon.py` draws the .ico from the same
three bars as the page's mark, writing the format by hand so the build needs
no image library.

Three things break when freezing, and each had to be found rather than
guessed:

* **Data resolved from `__file__`.** Bundled modules live in a temp folder
  PyInstaller names in `sys._MEIPASS`, so `parent.parent / "data"` lands
  outside it. `core/paths.py` resolves the shipped root for both cases; the
  two places that needed it were the recharge table and the logger.
* **Modules reached through a `sys.path` insert.** PyInstaller follows
  imports, not path manipulation, so all fourteen are declared as hidden
  imports. `profile` is the dangerous one -- it shares its name with a
  standard library module, and taking the wrong one kills the solver the
  moment it builds a speed profile. The frozen app returns lap times
  identical to the source build, which is what proves it took the right one.
* **Anything written at runtime.** The unpack folder is deleted on exit, so
  the user's saved recharge limits moved to `install.app_dir()`.

The logger install button exists because of this: with no folder beside the
executable there is nothing for anyone to copy, so the app writes the two
files into `apps/lua` itself, from its first screen.

The bundle is audited rather than assumed. Listing the archive's own table of
contents showed the first build quietly carrying the *FA25* logger and a dead
user-limits file, because the folders had been taken whole; the manifest now
names individual files. No part of the VRC car is in there.

## Making it shareable

Two things meant this could only ever run on the machine it was written on.

**The install path was a constant.** `AC_ROOT` pointed at one Steam library.
`core/install.py` reads Steam's own `libraryfolders.vdf` instead, which is the
only way to find a game on a drive the registry never mentions -- on this
machine Steam is on C: and Assetto Corsa is on D:. Failing that, the first
screen asks for the folder and remembers it.

**The car's physics shipped with the tool.** `out/fa26_unpacked` held 380
files, 2.6 MB, unpacked out of VRC's `data.acd` -- their paid work, in a folder
anyone could be handed. It is now unpacked from the copy the user already owns,
on first run, into `%LOCALAPPDATA%\FA26 Optimizer\cardata`, keyed on the size
and date of the source so a mod update is picked up and an unchanged install
costs one `stat`. Verified byte-identical to the copy the model was validated
against: 379 of 380 files match, and the one that does not (`setup_live.ini`)
is referenced nowhere.

**`make_release.py`** builds the zip that gets sent: 49 files, 0.4 MB. Its
manifest lists what ships rather than what does not, so a new folder is absent
until someone decides it belongs; and it audits the finished archive for car
data, telemetry, `.bak` setups and backup folders, deleting the zip rather than
publishing one that holds any of them. `check_app.py` runs that same audit over
the manifest, so the two cannot drift.

The logger is a CSP Lua app and lives in `apps/lua`, not the `apps/python`
folder the stock game uses -- the first version of the check looked in the
wrong place and reported it missing on a machine where it was working. Its
absence is now a notice rather than a blocker, since existing telemetry can
still be solved.

## The web front end

`fa26_app.py` replaces the Tk window. The UI is HTML served from `ui/` over
`127.0.0.1`, but it runs as an application, not in a browser tab: the page
opens in Chromium's app mode -- its own window, its own taskbar button and
icon, no address bar and no tabs. Edge ships with Windows, so this needs
nothing installed, which matters more for a tool meant to be handed to
strangers than a slightly better window frame would. Where pywebview happens
to be installed it is used instead and the page is embedded in a genuine
native window; `--browser` forces a plain tab. The server runs on a daemon
thread and is shut down when the window closes, so closing the window ends the
process and releases the port -- verified, along with the absence of orphans.

The launcher runs under `pythonw` so no console sits behind the app, which
also means a startup failure has nowhere to print; failures are written to
`fa26_error.log` and raised in a message box.

Typography is bundled rather than fetched. Pulled from Google Fonts the window
rendered in Barlow online and Segoe UI offline, which is two different
products; 192 KB of woff2 in `ui/fonts` ships instead and the page now makes
no external request at all. Both families are redistributable (OFL 1.1 and
Apache 2.0).

`fa26_gui.py` is left in place and still works, but it is no longer what
`Run FA26 Optimizer.bat` launches.

**Live means the game is on.** The first version called the newest folder on
disk "this session" and lit a green light beside it, so laps driven hours
earlier -- across a reboot -- were announced as current. That is precisely the
stale state the map-order guard exists to catch, being presented as the
opposite.

`install.game_running` finds `acs.exe` and reads its creation time, through
`CreateToolhelp32Snapshot` and `GetProcessTimes` in ctypes: no dependency, and
4 ms a call, so the page can ask on every poll. A session is live only when the
sim is running and its newest lap was written after the sim started, with ten
seconds of slack because file timestamps and process creation time are not read
off the same clock. Neither the launcher nor Content Manager counts -- both sit
open for hours with no car on track.

With nothing live the rail says so, the light stops pulsing and the Run button
is disabled. The last session is still offered, named and dated, behind a
*Use it anyway* button, because closing the window by accident should not mean
driving the laps again; taking it up adds a notice that these laps were not
driven in the session now running and may have been on a different map.

**One session, the one being driven.** There is no picker at all. A map is
written into a setup and stays there, so the workflow runs once per circuit and
last week's laps are never wanted; the session list was forty rows of history
to scroll past on the way to the one row that mattered. The rail shows the
newest session and nothing else, and polls for new laps every four seconds, so
a lap finished in the game appears without anyone pressing anything. With no
telemetry at all it says *Waiting for laps* rather than showing an empty
control. Which lap to model was never a question with a useful answer either --
every lap within 3% of the best is pooled regardless -- so the card reports how
many were recorded and how many are being used, and says why when those differ.

**A real progress bar.** `optimise26.optimise` takes an `on_progress`
callback. The unit is a sweep of the hill climb, subdivided by position within
the two loops that make up a sweep: the work is one climb per starting point
plus one over the subdivided answer, and a climb that converges early gives up
the rest of its share. So the bar is a measurement, not an estimate, and can
only move forwards -- 138 readings over a 74 s Suzuka solve, largest gap 0.2 s.
The callback is behaviour-neutral, checked by solving the same problem with and
without it: identical map, identical lap time to six decimal places. The
smoothing between readings is a CSS transition on the width, so the number
stays honest and only the drawing is eased.

**Stopping a run.** A Python thread cannot be killed from outside, so the stop
button is cooperative: `Engine.tick` raises `Cancelled`, and because the
solver already calls the progress callback about twice a second the exception
unwinds the search from wherever it had got to. Nothing is written to disk
during a solve and every working structure is local to `optimise26.optimise`,
so unwinding part way leaves nothing to clean up; the stage boundaries that
report no progress of their own -- fitting the car, the sensitivity sweep --
carry an explicit check instead. Measured at three points, it stops within
0.0-0.3 s of being asked, and a run started afterwards produces the same maps
to the millisecond.

The light in the corner is green when ready, amber while working and red when
stopped. Red is not an error signal there -- the user asked for the stop -- but
green reads as "all good", and a run that did not finish is not.

A stop throws away the strategies that had already finished. That is
deliberate: a setup carrying one new map and two old ones looks finished and
quietly does something else, which is the failure this project has spent the
most time on. The page says so in those words rather than silently offering
one map to save.

**Plain language, with the read-out kept.** The commentary was written for
whoever built it: *Drag profile 67 bins, median k 0.949*, *if the lap starts
with more or less: 30% -> 1:42.86*. It now reads as English -- "Worked out how
your car accelerates, brakes and grips from those laps", "About 0.8 MJ of
recovered energy had nowhere to go because the battery was already full". The
engineering lines are not deleted; they are emitted as a separate kind and sit
behind a *Technical details* checkbox, because they are what makes a
surprising answer diagnosable and every number in them has been needed at
least once. `check_app.py` holds the default lines to that standard: it fails
if `MGU`, `kJ`, `kW`, `harvest`, `taper`, `bins`, `median` or `splits` appears
in a line shown by default, and separately fails if the technical lines stop
being produced.

**Fuel is shown, not asked.** It is a mean over the session's telemetry. The
old spinbox defaulted to 45 L while a Monza push lap ran at 6.9, which is 28 kg
of car that was not there.

**The "active on track" picker is gone.** It set an ordinary setup item that
the pit screen can change, and everyone using this already knows 1 is
qualifying. The written setup now always opens on STRAT 1, which is the one
whose lap time can be compared against a real lap, and the apply panel says so.

**Recharge limits are sliders.** Their range is read from the car's own
`setup.ini` at runtime rather than hardcoded -- qualifying 4.0-9.0 MJ, race
4.0-8.5 -- so a slider cannot offer a number the car would silently clamp. One
step past the top is "no cap at all": that is `ERS_UNLIMITED_MODE`, the switch
VRC added for circuits with no published allocation, and it is written into the
setup in both directions so it cannot carry over from the last circuit. On a
circuit the FIA has published, the sliders are locked to the published figures
behind an *Override* link; on one it has not, they are open and the page says
why the number matters. A circuit's setting can be remembered in
`data/fa26_user_limits.json`, which is kept apart from the shipped table on
purpose: that table is evidence and this is an assumption.

**The cold-start button is gone.** It needed `AC_ROOT` hardcoded to one
machine's Steam library, which was a release blocker on its own, and it existed
to solve a problem -- the first outing on an empty map -- that is better solved
by driving three laps.

**Setups are listed, not browsed for.** A file dialog opened three levels deep
inside Documents on a folder named after the track's internal id. The apply
panel lists the setups that exist for this circuit, tagging the ones this tool
has written before (a `.bak` beside a file is the marker).

**Fewer words.** The first pass explained itself: every line said what it did
and why. Read on screen rather than in a diff it was clutter, so the commentary
was cut to the fact -- "Worked out how your car accelerates, brakes and grips"
rather than three clauses on whose driving it was built from. The four warnings
that have each cost a real session are kept in full, because they are the only
lines that change what the user does next.

**Motion.** Hover states lift the intro cards, the setup rows and the buttons;
result cards stagger in; the ready light breathes; a lap landing while you
watch flashes the count. All of it is decoration over a state that is already
carried by colour, text or position, and all of it is switched off under
`prefers-reduced-motion`.

**The map is drawn.** Each strategy gets a strip of deploy against lap
distance, with the simulated speed trace behind it -- a row of bars over a bare
axis says nothing, but against the trace you can see the blocks land on the
straights. The harvest band below the line is only drawn when the map actually
clips, which on a qualifying or race map is almost never: explicit clipping
appeared on 1 split out of 21 across the three Madrid maps, so reserving a
third of the height for it was wasted.

**Three bugs the rewrite found.** `write_strategies` returns the backup path as a
`Path`, which `json.dumps` refuses -- so the very first apply to any setup
wrote the file correctly and then reported a 500. And `.check { display: flex }`
beat the browser's own `[hidden] { display: none }`, leaving a checkbox on
screen that the code had explicitly hidden; `[hidden] { display: none
!important }` now sits at the top of the stylesheet. And a CSS escape for the
warning glyph, written through a shell heredoc, reached Python as an octal
escape and left a raw 0x16 byte in the stylesheet -- the flagged lines now
carry the same 3px rail as the result lines and differ only in colour, which
is both font-proof and one visual language rather than two.

`check_app.py` is the smoke test: it runs the server in-process, walks every
endpoint, solves a real session out of your telemetry and applies the result to
a copy of one of your setups in a temp folder. Every bug this front end has had
lived in a seam between two layers, which is what an end-to-end pass catches
and a unit test does not.

Unrelated cleanup done at the same time: `core/session.py` carried an
84-line duplicate of `latest`, `snapshots` and `map_order_warning` (the last
under its older name `driven_before_current_map`). Python kept the second copy
of each, so nothing behaved differently; the copies are gone.
