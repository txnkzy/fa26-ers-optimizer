# Work log: model accuracy, search, data handling

A running record of what was hypothesised, what was measured, and what came of
it. Every claim here is reproducible with `python validate_model.py`, which is
the harness the numbers come from.

---

## The measurement was wrong before anything else was

Every accuracy figure this project has published was produced by
`replay_numbers.py`, which picked, for each recorded lap, **the newest setup
snapshot written before it** and read **deployment map 1** out of it. Both of
those are assumptions, and both are wrong often enough to make the resulting
numbers meaningless.

**The strategy was never checked.** The logger records the live strategy on the
wheel in `mguk_delivery`, and the driver changes it constantly. Of the laps
that could be attributed to a map at all, a quarter were driven on STRAT 2 and
several on STRAT 3 — and every one of them was being scored against STRAT 1.
A Bahrain session driven on the race map, compared against the qualifying map,
measures nothing.

**The snapshot was chosen by clock.** Assetto Corsa reads a setup when the
session loads, so a map written mid-session does not reach the car until a
restart; and a user switching between setups leaves snapshots interleaved in
time order that have nothing to do with one another.

The evidence was in plain sight once looked for. Replaying with the old rule,
the simulation deployed 9.39 MJ on a Red Bull Ring lap where the car had
actually deployed 1.81, and 9.13 MJ on a Madrid lap that recorded **zero**.
Those laps were in the corpus the accuracy claim was computed from.

`cars/fa26.py` already contained `map_fit`, which scores a map against a lap by
how well its commands explain the power delivered, and reports the improvement
over an empty map — documented as scoring above +85% for a map the car really
followed and near zero for one it did not. **It had no callers.** It has one
now: `core/provenance.py` scores every snapshot against every lap on the
strategy the lap itself reports, and refuses the lap when nothing explains it.

Of 178 recorded laps, **36 survive** as honestly attributable. The rest:

| | |
|---|---|
| 91 | no recorded map explains the deployment |
| 24 | no map snapshot exists for the circuit |
| 13 | the strategy was changed during the lap |
| 5 | the lap recorded no deployment at all |
| 1 | the logger had the wrong track length |

**The README's "0.30% mean, 0.95% worst" was never a measurement of this
model.** On the 36 laps whose map is proven, the real figure at the start of
this work was **1.47% mean, 3.20% worst, and slow by 1.22% on average**.

---

## Holdout, not in-sample

`validate_model.py` fits drag, braking and traction from the *other* laps of a
session and never from the lap being replayed. `--in-sample` does the opposite,
and is what the old numbers used.

The gap matters, and so does its size:

| | mean | bias |
|---|---|---|
| in-sample | 1.14% | +1.01% |
| holdout | 1.47% | +1.22% |

Only 0.2 points of the pessimism is overfitting. **The rest is model form** —
fitting the model on the exact lap it then replays still leaves it a full
percent slow. That is what made it worth chasing rather than tuning.

---

## Where the time went: not drag, and not braking

The first hypothesis was drag, from the observation that Bahrain's corner
minima match within 1 km/h while mid-speed acceleration is 16–35 km/h down.
Grouping the speed error by band supported it — the deficit grows monotonically
with speed, −0.3 kph below 30 m/s rising to −3.1 kph above 80.

Three tests killed it.

**Scaling drag.** A uniform scale on the fitted drag improves the error, but
only gradually, and even at 0.90 — a 10% error in the aerodynamics of a car
whose drag is *solved for* rather than guessed — the bias is still +0.47%. A
knob that has to be turned that far to half-fix something is not the cause.

**The braking integration.** The backward pass evaluates deceleration at the
slower end of each step, which understates the decel available. Over a 2.5 m
step that is worth nothing at all: evaluating at the midpoint instead moved the
mean by 0.01 points. Scaling braking capability outright by 1.5 moved the bias
by 0.09 points. Not braking.

**The force balance, checked forward.** For every full-throttle sample of every
verified lap, the acceleration the simulator's own force balance gives at that
speed, lateral load and measured deployment, against what the car did:

```
speed m/s           n     mean   median
30-50           14186   +0.886   +0.895
50-65           65954   +0.391   +0.337
65-80          109382   +0.067   +0.137
80-120          52192   +0.013   +0.084
```

The model **out-accelerates the real car** at matched conditions, everywhere.
Whatever was making laps slow, it was not a shortage of thrust.

---

## Where the time actually went: the speed ceiling, one operator

Measuring where the speed deficit is *created* rather than where it is felt:

```
phase      deficit made       per km    at the cap
brake          +1087.1 kph      +50.34         0%
corner          +451.6 kph      +21.95        30%
exit            -729.0 kph      -25.04        31%
straight        -749.5 kph       -6.58         3%
```

The model arrives at corners carrying *more* speed than the driver, and loses
all of it on the exit and down the straight. Corner exits are where it bleeds,
at 25 kph per kilometre, and they are capped 31% of the time.

`Profile.__init__` built the ceiling like this:

```python
value = NO_CAP if power_limited else speed[i] * margin
self.cap[slot] = min(self.cap[slot], value)
```

A slot is 2.5 m and the logger writes 127 samples a second, so a slot holds
three or four samples. Wherever speed is changing — every corner exit, every
braking zone — taking the **minimum** of those samples reads the speed at the
slot's *entry* rather than anywhere in it. The simulation is pinned to that
ceiling through exactly those slots and can never recover the metre or two of
speed the operator threw away.

`min` was never chosen for that question. It is the same operator that makes
any non-power-limited sample cap the slot, reused across samples by accident.

Taking the slot maximum instead:

| | mean | worst | bias |
|---|---|---|---|
| before | 1.47% | 3.20% | +1.22% |
| after | **1.14%** | **2.87%** | **+0.66%** |

Every circuit in the corpus improved. None was made worse. Raw speed against
smoothed made no difference (1.15% against 1.14%), which rules out the
smoothing window and leaves the operator as the whole of it.

---

## A lap is a loop, and the loop was not closed

With the ceiling fixed, the two worst replays in the corpus were both Bahrain
laps, in-sample, at +5.78% and +4.83% — on a map the model otherwise fits to
0.5%, and while deploying *more* energy than the car did. A simulation cannot
be slower with more energy unless something else is wrong.

It was starting them from a standstill. `Profile` fills an empty slot from the
slot before it, guarded by `elif i:` so index 0 is skipped — leaving
`self.speed[0]` holding the zero it was initialised with whenever the logger's
first sample lands past the line. Those two laps begin 2.7 m past it. The
simulation read a start speed of zero, set off at walking pace where the car
had crossed at 280 kph, and spent the lap recovering.

Slot 0's predecessor is the last slot. Two of 38 verified laps were affected,
and they were exactly the two worst.

| | in-sample | | holdout | |
|---|---|---|---|---|
| | mean | bias | mean | bias |
| at the start | 1.47% | +1.22% | — | — |
| ceiling fixed | 1.14% | +1.01% | 1.14% | +0.66% |
| loop closed | **0.69%** | **+0.16%** | **1.13%** | **+0.43%** |

---

## What the remaining error is, and why it should stay

In-sample bias is now +0.16%. Given a lap's own line and calibration, the model
reproduces that lap. The model form is sound.

The holdout residual of +0.43% is **not a defect**. Replaying a lap against a
profile built from the *other* laps of its session asks the model to reproduce
a lap it was not shown, and the laps in this corpus are 1.01% faster than their
siblings on average — they are the ones with proven map provenance, which
skews towards the laps someone was pushing on.

Splitting on that:

| | bias | n |
|---|---|---|
| laps **slower** than their session siblings | −0.13% | 14 |
| laps **faster** than their session siblings | +1.16% | 22 |

with r = −0.64 and a slope of 0.41. The 1.01% head start times 0.41 is 0.41% —
essentially the whole residual.

The speed ceiling is the driver's own line. A lap faster than the session it
was fitted on is a lap the model declines to invent, and that is the behaviour
wanted from a tool that recommends deployment maps: a model willing to promise
speed the driver has never shown would overstate every gain it reports.

Confirmed by trying to tune it away. Taking a higher percentile of the speed
ceiling across the session's laps does remove the bias, and makes the model
worse doing it:

| cross-lap ceiling | mean | bias |
|---|---|---|
| median (kept) | 1.13% | +0.43% |
| p75 | 1.09% | −0.58% |
| p90 | 1.11% | −0.91% |
| maximum | 1.33% | −1.13% |

The mean barely moves while the model turns optimistic. Median stays.

---

## Which circuits, and what the claim should be

Per-circuit bias correlates with top speed at r = +0.85 and inversely with the
fraction of the lap spent braking at r = −0.81: the faster the circuit, the
more of it is run free of the ceiling under the model's own force balance, and
the more room there is to differ. That is the structural variable the error
scales with, and it is why Suzuka and Monza sit at the top of the table and
Monaco and Madrid at the bottom.

It is also why a single global accuracy number should never have been quoted.

---

## Entry speed and entry charge were never two things

The simulator took the speed at the line and the charge at the line as separate
inputs. They are not separate: deploying out of the final corner buys the one
and spends the other, in a fixed exchange the map itself sets. Asked which is
worth more, the old code could only price each half against a frozen version of
the other, and the answer it built from that ratio was wrong twice — by a
factor of eighteen at Albert Park (0.56 s predicted, 0.030 s measured) and in
the wrong direction at Monza.

`simulate` can now run part of a lap from a known state and return the state it
leaves in. Three things travel: speed, charge, and the demand latch — without
the latch the second half of a run asks for power the first half has already
forfeited under the no-increase rule, which would quietly make every early
switch look better than it is.

`sim26.run_to_line` integrates the last 600 m under whichever map is live
there, so both halves of the state that crosses the line follow from where the
switch happened. Every switch point is then priced by the lap it produces:

| circuit | best switch | worth |
|---|---|---|
| Suzuka | 400 m out | 0.85 s |
| Monza | 50–100 m out | 0.37–0.56 s |
| Bahrain | 600 m out | 0.15 s |
| Monaco | 300 m out | 0.09 s |
| Albert Park | at the line | early costs 0.48 s |
| Red Bull Ring | at the line | early costs 0.29 s |
| Madrid | at the line | early costs 0.18 s |

**It is circuit by circuit**, which answers the question that prompted this.
Monza is the only circuit where both have been driven, and the early flip won
by about half a second there; the coupled model agrees, where the old one had
called it a coin flip and declined to pick a side.

---

## Pooling, settled with a measure rather than an argument

`core/session.py` carried a comment saying any future change to the pooling
tolerance needed an out-of-sample measure — fit one session, score against a
different one — after a tighter window scored well in-sample and cost 0.236 s
on track. That measure now exists. Fitting only the laps the rule selects and
replaying a lap it did not:

| tolerance | mean | laps fitted |
|---|---|---|
| 1% | 0.97% | 2.1 |
| 2% | 0.97% | 2.2 |
| **3% (kept)** | **1.01%** | 2.4 |
| 5% | 1.21% | 3.9 |
| none | 1.18% | 4.8 |

1–3% are within 0.04 points of each other and 5% and looser clearly lose. The
tighter window is not measurably better here, and it is 0.236 s worse on track,
which is the stronger evidence. **The tolerance stays at 3%.**

`MIN_LAPS` is a different question and moves the other way — it was discarding
data rather than keeping less of it:

| minimum | mean | worst |
|---|---|---|
| 2 | **0.95%** | **2.62%** |
| 3 (was) | 1.01% | 2.86% |

Of 46 recorded sessions, **13** had two consistent laps and were being modelled
on one of them: Monza, Monaco, Daytona, Suzuka, Madrid, Bahrain, Spa and the
Hungaroring. `MIN_LAPS` is now 2.

---

## The solver is a hill climb, and cannot be a dynamic program

`_climb` walks single-split moves and paired deploy/harvest moves until a sweep
improves nothing. That is a hill climb, not the dynamic program over track
position and energy state the design called for.

It cannot be one, for two reasons that are both about the car rather than the
code. **The control is not a power per metre.** It is 24 splits, each with four
positions and two powers, and a DP over (position, energy) produces a schedule
that in general no set of 24 splits can encode. **The admissible control
depends on history.** The no-increase rule latches the demand from the moment
the throttle is picked up until the driver lifts, so what a zone may ask for is
a function of everything since the last lift, not of where the car is and how
much charge it has. Add that lap time needs speed as a state and the harvest
cap is an integral constraint, and the separability a DP needs is gone.

So the climb stays, and what it cannot reach it has to be started from.

## Seeds were being named one at a time, which does not converge

`_all_out` was (the whole lap, 350 kW). `_front_loaded` was (the first 45%,
350 kW). Each was added after a circuit turned up where the climb got stuck,
and each carried a guard deciding when it was worth the extra climb. There was
no reason to think those two were the only shapes that mattered, and nothing
said which to try next.

The family is now enumerated: four coverages crossed with three power levels,
plus three that deploy early and super-clip the rest — fifteen maps. Each is
screened with **one simulation** and only the best three are climbed. A climb
is hundreds of simulations, so fifteen screens cost less than a tenth of one,
and widening the grid is nearly free where naming another shape was not.

### Super-clipping: a search blind spot, not a model bias

Asked directly whether super-clip is reachable:

- **It is in the action space.** `_level_clip` is among the single-split moves.
- **It was in no starting map.** Both named seeds set clip to zero everywhere.
- **The climb cannot get there on an energy-neutral map.** One zone of harvest
  with nothing to spend it on is pure loss, so it is rejected before the
  deployment that would pay for it is ever tried. The paired move exists for
  exactly that and pairs one zone with one other — a map wanting several zones
  of clip to fund one long deployment is out of reach from every direction.

And it is not the model undervaluing corner speed. Checked forward against
measured acceleration, the force balance is *optimistic* at low speed
(+0.89 m/s² at 30–50 m/s), which is where deployment out of a corner is worth
most. A model error of that sign pushes a solver **towards** super-clip, not
away from it. So the avoidance was the search, and `_clip_blanket` is now among
the screened starts.

## Speed: rank on a coarse grid, measure on a fine one

The climbs ran at 2.5 m, which is the resolution the reported lap time needs
and far finer than ranking candidate maps against each other needs. The coarse
climbs now run at 5 m and only the refinement at 2.5 m:

| | before | after | cost |
|---|---|---|---|
| Madrid, qualifying | 143 s | **88 s** | 0.006 s |
| Spa, qualifying | 199 s | **113 s** | 0.077 s |

Both costs are far inside the model's own error band.

## Making the wider search never worse

Screening on one simulation each was not enough on its own. Against the old
solver, five circuits, both strategies, the first version was worse at three of
five in each — and *never worse* was the property the search it replaced had
been built to have.

**A screen ranks maps as they stand, not by how well they climb.** For a
one-shot lap that difference has a known answer: spending the whole store is
the job. That start is now climbed whatever the screen thinks of it — Austin
−0.134 → +0.049, Suzuka −0.145 → +0.034.

**A full store refusing recovery is invisible to a screen**, because a map that
throws recovery away still gives a lap. The guarded climb from a spend-early
map is back, on the same threshold the app reports waste at. Monaco −0.271 →
−0.172.

**A coarse answer is never the answer.** Between 3.5 m and 3.634 m at Monaco
the map falls off a cliff — 73.555 s and 11.95 MJ to 74.672 s and 9.52 —
because at some step size a boundary the map needs stops being representable.
The cliff is sharp, circuit-specific, and no rule tying step size to lap length
dodges it. So the coarse pass only ranks starting points now, whatever it picks
is climbed again at 2.5 m before subdividing, and it runs only where there is
something to accelerate: qualifying searches above 4.5 km. Monaco solves in
under a minute at full resolution anyway, and ranking a *race* search's starts
on a 5 m grid picked the wrong one at Spa, 0.339 s worse, for a saving Spa did
not need.

Where it lands:

| | mean gain | best | worse at | time |
|---|---|---|---|---|
| qualifying | **+0.140 s** | +0.658 s | **0 of 5** | 251 s → 246 s |
| race | **+0.315 s** | +1.228 s | **0 of 5** | 146 s → 319 s |

Race searches are about twice as long. They were the fast ones — tens of
seconds, because the energy-neutral constraint rejects most moves outright —
and the extra starting points are what found Austin 1.23 s and Suzuka 0.25 s.

---

## The logger's lap-time fix, verified from the data

This was carried as unverified because verifying it seemed to need the game. It
does not. `saveLap` takes the game's own `previousLapTimeMs` **only when it
agrees with the logger's own measured `elapsed` to within a tenth**, and falls
back to `elapsed` otherwise. So the question is whether any recorded file is
named with a time that is not its own.

Across all **179 recorded files, none** has a filename time disagreeing with
its own recorded elapsed by more than half a second. The fix holds.

## Two laps, one lap

One duplicated lap time exists in the corpus: an Austin session with two files
both named 1:40.204. They are the same lap — their speed traces agree at 249 of
253 sampled points — and the second copy has its energy store frozen to the
kilojoule for all 15,001 samples.

Two things follow. A lap whose store never moves is refused: the car harvests
under braking whatever map it is on, so a frozen store is the logger having
stopped reading rather than a measurement. Six files go, and the smallest real
store movement among the rest is 269 kJ against a 5 kJ threshold. And two laps
of one session sharing a time to the millisecond are treated as one lap saved
twice — a duplicate does not merely add nothing, it double-weights that lap in
everything pooled from the session.

Removing them makes the measured error slightly **worse**, 1.13% → 1.21%,
because the duplicate was quietly contributing a second copy of a good speed
trace. **1.21% is the honest number.**

---

## The power curve: the data does not reach where the curves disagree

Article C5.2.8 caps deployment by speed on a curve named per circuit. Two are
implemented — Standard, full power to 290 kph and nothing by 345, and Overtake,
full to 337 and nothing by 355 — and ten circuits name neither and fall back to
Overtake. The brief was to fit a real curve from telemetry and use it
everywhere.

An earlier attempt at this was abandoned for a stated reason: "the ceiling it
measures is only ever the most the driver happened to ask for." That objection
is answerable now `core/provenance.py` knows which map each lap ran — a sample
counts only where the map commanded the car's **maximum** and the demand latch
had not already pulled the level down. `measure_power.py` is that measurement.

The answer is that the data does not reach far enough:

> no recorded map asks for full power above **290 kph**, so the curve is
> unconstrained by measurement from there upwards

290 kph is exactly where Standard's knee sits, and 47 kph below Overtake's.
**The corpus cannot distinguish the two curves**, because no map in it asks for
full power anywhere they disagree. Up to 290 kph the car delivers 350 kW and
both curves permit it.

One direct observation does exist, from a Suzuka qualifying lap whose map
commands 350 kW down the whole main straight:

| speed | delivered | Overtake permits |
|---|---|---|
| 334.7 kph | 350 kW | 350 kW |
| 338.3 kph | 323 kW | 337 kW |
| 340.1 kph | 292 kW | **292 kW** |

Overtake predicts 340 kph exactly. That is one lap at one circuit, but it is
the only measurement that reaches past the knee, and it supports the fallback
rather than contradicting it.

Confirmed by outcome as well as by physics: swapping the curves changes replay
accuracy by 0.03 points (1.13% against 1.16% for Standard everywhere), and the
fallback path is not exercised at all by the verified corpus. **No new curve is
fitted, because there is nothing to fit it to.** `measure_power.py` is in the
repo so this can be redone the moment a map asks for full power at 320 kph.

### The 200 kW floor, and where a lap starts

The floor is already modelled explicitly — `deploy_kw(..., full_throttle=...)`
holds 200 kW for a second after each throttle application, and the store cost
follows from it rather than being assumed.

The belief it was meant to explain — that a lap can never start above about
90% charge, because the floor spends 10% on the run to the line — does not hold
against the data. Of 175 recorded laps, **25 start at 95% or more and several
at exactly 100%**. What the charge at the line is depends on the strategy the
in-lap was driven on, which is the same coupling `run_to_line` now models. So
the qualifying map's assumption that it can start full is sound, and the
warning when a session starts below 90% is worth keeping.

---

## Left for you

Two things in the brief cannot be done from here, and one was asked to be left
alone.

**Another machine.** Everything here was tested on one. Two external reports
have surfaced two real bugs, which says the latent rate is higher than one
machine can find. This needs somebody else's Windows install, somebody else's
OneDrive redirection, somebody else's Assetto Corsa folder — and no amount of
testing here substitutes for it.

**The Austin on-track A/B.** The qualifying fix predicts about 3 s at Austin
and it is model-verified only. The model is now honest about its own error, and
this is exactly the kind of claim that error band does not cover: it is a
prediction about a map nobody has driven. Drive it both ways and the answer is
settled either way.

**VRC.** Flagged, not acted on, as asked. The repository is public and they
have never been contacted.

One thing is now verified that was not: the logger's lap-time handling, checked
against all 179 recorded files rather than against the game. See above.

---

# Round two

## Attribution written down, not worked out

Of 178 recorded laps only 36 could be honestly attributed to a map, and a
quarter of the failures were laps driven on one strategy and scored against
another. None of that was a modelling problem. It was the logger not writing
down what the car was doing at the time, and everything downstream trying to
infer it later from setup-file timestamps.

CSP exposes `ac.getSetupSpinnerValue(id, fallback)`, which returns the live
value of a setup item by its declared ID — and a deployment map *is* setup
items, `DEPLOYMENT_MAP_<n>_SPLIT_<s>_<f>`. There is nothing to infer.

So the logger reads them. At the moment a lap is captured it takes every
strategy slot the wheel sat on during that lap, reads that slot's 24 splits
out of the setup, and writes them beside the lap as `<lapname>.map`:

```
[LAP]
LOGGER=2
TRACK_LENGTH_M=5770.0
STRATS=1,2
DOMINANT_STRAT=2
[MAP_2]
SPLIT_1=0,317,317,454,350,0
...
```

`provenance.attribute` reads that first and consults no snapshot at all when it
finds it. The behavioural matching stays, for the 178 laps already on disk.

**Verified.** An existing lap — one driven on STRAT 2, which is exactly the
case that used to be misattributed — was copied, given a sidecar written byte
for byte the way the Lua writes it, and handed to the real `attribute()` with
**no snapshots**. It came back with the right strategy, the right map, marked
`recorded`, having ignored a second strategy's map deliberately placed in the
same file; and when snapshots *were* offered it still preferred what was
written down. That round trip is now a permanent check, as is parsing the Lua
— `luaparser` catches a deliberately broken `local function` and passes the
real file.

**Not verified.** Whether `ac.getSetupSpinnerValue` returns what it should
in-game. The function is in this CSP install's own API definitions and the call
is wrapped twice — a CSP without it loses the sidecar, not the lap — but the
first real proof is a lap driven with the new logger, whose `.map` file should
appear beside it and whose replay should report itself as recorded rather than
matched.

---

## Auditing what was decided on the contaminated corpus

Every constant in the model was chosen by measuring replay error on a set of
laps a quarter of which were scored against the wrong strategy's map. So each
is a claim that has to survive being made again, and each was made again.

| decision | verdict |
|---|---|
| Combined-grip envelope, n = 4 | **held up** — 1.21%, against 1.49% at n = 2 and 1.22% at n = 3, 6 and 8 |
| Speed ceiling margin, 1.00 | **held up** — 1.21%, against 1.88% at 0.98 and 2.12% at 1.05 |
| Speed smoothing, 15 samples | **held up** — indistinguishable from 5 to 41 |
| Drag bin width, 50 m | **held up** — indistinguishable from 25 m to 200 m |
| Samples per drag bin, 6 | **held up** — indistinguishable from 3 to 24 |
| Traction percentile, p97 | **held up** — 1.21%, p95 1.20%, p99 1.23% |
| Unloading the traction fit | **held up** — and the mismatch between the fit's circle and the simulator's n = 4 envelope costs nothing measurable |
| Harvest in the drag balance | **held up** — leaving it out costs 0.10 points and 0.19 of bias |
| Motor efficiency, 0.93 | **held up** — 0.88 scores 1.19%, which is inside noise and not a reason to move a physical constant |
| Power reduction, 100 kW/s | **held up** — 1.21%, against 1.30% at 50 and 1.35% unlimited |
| Instant-cut threshold, 210 kph | **held up** — 1.18% at 150, inside noise |
| No settling for a one-shot replay | **held up** — 1.21% against 1.75% with a settling lap |
| Harvest taper on slot mean | **held up** — 101% of measured harvest, against 98% on the slot maximum |
| **Lateral-load fraction, 0.7** | **looked changed, and was not** — see below |

### The one that looked like it changed

Raising `lat_fraction` from 0.7 to 0.9 improved the mean from 1.21% to 1.12%,
monotonically across 0.5 → 0.9. That is exactly the shape of a real effect, and
exactly the shape of the corpus's own bias: raising it treats fewer points as
cornering, which leaves fewer of them capped, which frees the model — and the
replayed laps are 1% faster than the sessions they are fitted on, so **anything
that loosens the ceiling flatters itself**.

Splitting the corpus on that:

| lat_fraction | laps faster than their session | laps slower |
|---|---|---|
| 0.7 | 1.29% | **1.10%** |
| 0.8 | 1.18% | **1.10%** |
| 0.9 | 1.14% | **1.10%** |
| 1.0 | 1.09% | 1.12% |

On the fourteen laps where the model is not being asked to invent anything, 0.7,
0.8 and 0.9 are **identical**, and the whole apparent gain sits on the other
half. It was the selection gradient. **0.7 stands.**

This is the check the pooling-tolerance mistake needed and did not get.

### Pooling, re-measured on the corpus as it now stands

| tolerance | mean | bias | laps fitted |
|---|---|---|---|
| 1.0% | 0.84% | −0.15% | 2.2 |
| 2.0% | 0.86% | −0.09% | 2.3 |
| **3.0% (kept)** | **0.90%** | **−0.01%** | 2.6 |
| 5.0% | 1.11% | +0.34% | 4.1 |

Same answer as before: 1–3% within 0.06 points of each other, 5% and looser
clearly worse, and 3% has the least bias of any of them. **3% stands**, and the
on-track result that contradicted the tighter window stands with it.

**`MIN_LAPS` needs a correction.** It was moved from 3 to 2 on a measured gap of
0.95% against 1.01%. That gap has since closed — 1, 2, 3 and 4 now score 0.90,
0.90, 0.89 and 0.87 — because the laps that were inflating the three-lap figure
were the corrupt duplicates now refused outright. The mean no longer separates
them. Two still has the best bias (−0.01% against −0.12%) and the best worst
case (2.52% against 2.86%), and thirteen of forty-six sessions had two
consistent laps and were being modelled on one, so it stays at two — but **the
number originally quoted for it no longer holds.**

### What this audit cannot reach

Decisions about the *search* — the extra starting points, subdividing a solved
map rather than seeding a fine one, VRC's own maps as seeds — are comparisons
between one simulated map and another. No lap attribution enters them, so the
contamination never touched them; what they do depend on is the model being
right, and the model has changed. The old-solver-against-new benchmark is what
re-validates them, and it says 0 of 5 circuits worse in either strategy.

One stated justification did not reproduce. The brake-based test for "was the
driver flat here" is argued in the code from a Monza race replay losing two
thirds of its harvest to the alternative. On the clean corpus the alternatives
are indistinguishable — all four score 1.21% and 100–101% of measured harvest.
The rule is not wrong, and the brake test is still the physically right
question; but the number behind it belongs to the old measurement.

---

## What a race search costs, and why it did not have to

The number first. Race searches, wall clock, on this machine:

```
circuit            length   race lap    deploy     evals      wall
cota_2022           5429m    102.417     7.37      9920       58s
fn_spa              6943m    112.355     7.77      8778       67s
rt_suzuka           5770m     93.209     7.09      8646       58s
monaco              3271m     74.099     8.45     17606       66s
fn_monza            5750m     86.590     5.22     10784       69s
madrid_street       5340m    101.671     8.48     14020       83s
fn_barcelona        4611m     78.075     7.04      9142       46s
fn_albertpark       5239m     81.203     6.01     12754       73s
```

46–83 s, median 66. Against the old solver on the five circuits measured both
ways, 146 s became 319 s — the extra starting points are what found Austin
1.23 s and Suzuka 0.25 s, and they cost roughly double.

**It was not a hard trade.** Profiling a Spa race search: 4,389 simulations,
644 million function calls, 198 s under the profiler.

```
   ncalls  tottime  cumtime  function
     4389   83.255  194.532  sim26.simulate
217271097   28.651   28.651  builtins.max
211621014   26.820   26.820  builtins.min
 22624048   12.559   24.391  fa26.Powertrain.deploy_kw
 24376506   10.328   13.006  carphysics._lookup
 24376506    9.366   24.953  carphysics.best_tractive_force
 12331669    7.555    7.555  fa26.map_command_at
```

`min` and `max` are **429 million of those calls and 55 of the 198 seconds**.
They are builtins, so each is a Python-level call; written as a conditional
expression they are not calls at all, and for two floats with no NaN involved
the result is identical by definition. And `map_command_at` scanned the split
list afresh for every one of a simulation's 2,800 steps — 12.3 million linear
scans over a list already in the order the steps visit it.

Thirteen edits, none of which change a number:

| | before | after |
|---|---|---|
| 14 solves, 7 circuits, both strategies | 822 s | **526 s** |
| race searches, mean | ~64 s | **~41 s** |

**Every one of the fourteen came back bit-identical** — same map hash, same lap
time to six decimals, same evaluation count. That is the whole claim, and it is
checked rather than asserted: each solve is fingerprinted before and after and
the two files are diffed. The one-pass command builder is checked against the
lookup it replaced on 120 randomly generated maps, every step of every one, as
a permanent test.

Against the old solver, then: 146 s became 210 s rather than 319 s, for maps up
to 1.23 s better and worse at none. There is more available — `best_tractive_force`
and `deploy_kw` are 24 million calls each and could be inlined the same way —
but the profile's shape has changed enough that it is worth re-measuring before
guessing again.

---

## Super-clip: is it chosen, or only allowed?

Unblocking it and having the solver pick it are different claims, and only the
first had been checked. So: where does super-clip appear in the maps the solver
actually produces, and at those circuits, does a solver *allowed* to clip beat
the same solver *forbidden* to clip?

Forbidding it means removing every clip-raising move, every clip-carrying
starting map, and the paired harvest move — so the two runs differ in exactly
one thing.

**It is chosen.** Five of fourteen solved maps carry it:

| circuit | strategy | clip zones |
|---|---|---|
| Albert Park | qualifying | 803–982 m @ 350 kW, 3889–3992 m @ 350 kW |
| Albert Park | race | 869–982 m @ 350 kW, 3880–3992 m @ 300 kW |
| Monza | race | 131–166 m @ 50 kW, 757–787 m @ 200 kW |
| Monaco | race | 2466–2572 m @ 50 kW |

**And it wins.** The same search with super-clip taken away:

| circuit | strategy | allowed | forbidden | clip is worth |
|---|---|---|---|---|
| Albert Park | race | 81.203 | 82.285 | **+1.082 s** |
| Monaco | race | 74.099 | 74.708 | **+0.609 s** |
| Monza | race | 86.590 | 87.127 | **+0.537 s** |
| Albert Park | qualifying | 79.366 | 79.382 | +0.016 s |

Three of the four are worth more than half a second. The fourth is not — Albert
Park's qualifying map chooses two 350 kW clip zones worth sixteen thousandths,
which is the solver being indifferent rather than being right, and is reported
as such.

The pattern is the one the mechanism predicts. Every substantial case is a
**race** map at a circuit with allowance left over — Albert Park harvests 6.05
of 8.0 MJ, Monza 5.22 of 7.0 — where braking alone cannot reach the cap and
clipping is the only way to put more energy in. Where the cap is already
saturated by braking (Austin at 8.00 of 8.0, Spa's qualifying at 7.00 of 7.0),
it is correctly not used.

So the earlier conclusion was right but under-tested: it was a search blind
spot, it is now open, and the solver walks through it. Both halves of what made
that possible — a clip-carrying starting map in the family, and a single-split
move that turns clipping on from nothing — are now permanent checks, because
both were once absent with nothing noticing.

---

## The logger, in the game at last

Four laps were driven and nothing recorded them. Assetto Corsa's log said why:

```
Failed to parse: fa26_baseline.lua:206: unfinished string near '"'
```

A shell heredoc had turned a `\n` escape into an actual line break, leaving a
short string spanning two lines. **`luaparser` accepted it.** Real Lua does not
allow a raw newline in a short string; the Python parser did not care, so the
check written specifically to stop this was blind to it. `lua_lint.py` is the
lexer that is not blind to it — on the rejected file it reports line 206, the
same line the game reported.

Reading the file properly turned up two more from the same half-applied edit,
one of them fatal on its own: **the finished lap never carried the strategy
tally**, so `saveMaps` read `lap.strats` off a table that did not have it,
`pairs(nil)` raised, the `pcall` around the call swallowed it, and no sidecar
would ever have been written even with the string repaired. Both halves of the
file are now checked against each other — every `lap.<field>` the writer reads
must be a key the saved lap carries — and removing those two fields again fails
the check by name.

### Then it wrote sidecars with an empty map

With the parse fixed, five laps produced five sidecars, each carrying the right
strategy — including lap 4, where the wheel was moved mid-lap and the tally
recorded `[1, 2, 12]` and picked 1. But every `[MAP_n]` block was empty:
`ac.getSetupSpinnerValue` returns zero for every deployment item on this build.

The map was never the hard part. **The optimiser can read a setup file
perfectly well, and always could — what it never knew was which file.** That is
the whole of the ambiguity, and `ac.onSetupFile` hands the name over as the
game loads it. So the logger records the name, and the reader follows it.

Verified on the five real laps, with the real setup on disk and no snapshots in
sight:

| lap | strat | deployed | `map_fit` skill | attributed |
|---|---|---|---|---|
| lap01 1:41.211 | 2 | 8.01 MJ | 0.86 | cota.ini |
| lap02 1:53.385 | 2 | 7.88 MJ | 0.85 | cota.ini |
| lap03 1:40.872 | 2 | 8.26 MJ | 0.84 | cota.ini |
| lap04 1:39.925 | 1 | 10.62 MJ | 0.77 | cota.ini |
| lap05 1:39.966 | 1 | 8.77 MJ | 0.51 | cota.ini |

The STRAT 2 laps get STRAT 2's map and the STRAT 1 laps get STRAT 1's, and
`map_fit` confirms each one explains the power the car actually delivered. The
name is followed but not trusted: this tool overwrites setups, so a lap naming
a file whose contents have since changed falls back to the old matching.

### And a logger fault worth naming

`lap06` of this session: 15,929 samples for a 1:40 lap against ~13,300 for the
others, the energy store frozen at 1404 kJ from first sample to last, zero
deployment. The same signature as an Austin lap on 19 September, and in both
cases it is **the last lap of the session** — the ERS channels stop updating
while position and speed keep coming. `lap.problem()` refuses it, which is the
guard added for exactly this, working on a case it had not seen.

---

## A lap lost to a file being replaced under a running game

A 1:38.867 at Austin was driven and never reached disk. Assetto Corsa's own log
has the whole story:

```
11:35:19  Lua: <App: FA26 Baseline Logger> loaded and ready to run
11:39-42  three laps written
11:58:18  the logger file was overwritten
12:00:46  Reloading Lua script [App: FA26 Baseline Logger]
12:02     session ended, nothing further written
```

CSP watches these files and hot-reloads a Lua app the moment one changes. A
reload resets **every local the script holds** — whether it is recording, the
samples gathered so far, how many laps it has saved, the session stamp it names
them with. The lap in progress is discarded and the recorder waits for a fresh
line crossing.

`install_logger` refuses while the game is running now, and says why. The page
offers to do it anyway, because between sessions that is exactly the right
moment; mid-lap it is not.

Worth being plain about how this happened: the file was written **by me**,
reinstalling the logger while the user was on track, because it did not occur
to me to check. The guard is the fix, and it protects anyone who clicks the
update button mid-session as well.

The same log caught something else — `Slow script: App: FA26 Baseline Logger,
31 ms`. The strategy read was allocating a closure per frame at 120 Hz. The
function is declared once now.

---

## `Laps saved: 0`, and nothing anywhere said why

A session loaded the logger cleanly, ran for fifteen minutes, and wrote nothing.
No parse error, no reload, no line in any log. The window said what it always
says.

`script.update`'s line-crossing branch runs **once per lap** and is the frame
that decides whether that lap is saved. It had this in it, outside any `pcall`:

```lua
if ok and config and config.filename then file = config.filename end
```

`config` is a CSP object, and indexing a field one of those does not have
**raises** rather than returning nil — which is the first thing this file's own
header explains, and which was ignored one function further down. The raise
takes out the branch on the one frame that matters, and the lap is gone with no
symptom at all.

Everything the sidecar wants is now in `sidecarExtras`, and the branch calls it
through one `pcall`. A check enforces that the branch touches nothing of the
game's directly: it strips comments and strings and fails on any bare `ac.`
reference, naming it. Run against the version that lost the laps, it says *the
crossing branch reaches for ac.INIConfig directly*.

**The lap is the thing worth having. The sidecar is worth having too, and it is
never worth the lap.**

That is three faults in the same file in one day — a mangled string that stopped
it parsing, a half-applied edit that would have stopped it writing, and an
unguarded field read that stopped it recording. Each one shipped because the
check that would have caught it did not exist yet. Each one now has it.

---

## The recorder does not get to decide what counts

`Only valid laps: ON` was the default, and it was eating the laps that mattered.
A push lap out of a recharge lap where a corner had been cut arrived with the
game still reporting the previous lap invalid, so the push lap — clean, quick,
the whole reason for the run — was discarded silently. Several 1:38s at Austin
went that way.

The switch is gone, and so is the button. `lap_valid` is already written per
sample, and the count of invalid samples now goes in the sidecar, so excluding
an invalidated lap is still possible — downstream, visibly, where the decision
can be seen and reversed. Nothing is lost by recording; a great deal is lost by
not.

## The probe found the key

`ac.getSetupSpinnerValue` returns **527** for `CUSTOM_SCRIPT_ITEM_1052183907`,
which is exactly what `cota.ini` holds for the end of the first split of map 1.
So the deployment values *are* readable — keyed by the **section name**, not
the `ID=` the car declares, which is why every attempt by declared ID came back
as the fallback.

Reading the whole map that way would make the sidecar immune to the setup file
being rewritten. It needs the ID-to-index map, which the app already computes
and could write beside the logger at install time. **Noted, not done** — one
change to that file at a time is the lesson of the last three sessions.

## Where Austin stands

Two push laps recorded on the new qualifying map, against what the model said
for the charge each actually started with:

| lap | battery | driven | model | model is |
|---|---|---|---|---|
| lap03 | 93% | 1:39.053 | 1:39.358 | +0.305 s |
| lap05 | 89% | 1:39.159 | 1:39.453 | +0.294 s |

**Consistently 0.3 s pessimistic**, which is close to the +0.63% bias the
harness reports for Austin and in the same direction. The 1:38s that were
thrown away would sharpen it; they are gone.

---

## Driving the protocol properly broke the harness

A qualifying run is a **mixed session** by construction — recharge lap, push
lap, recharge lap, push lap. The holdout rule fitted each lap on *all* the other
laps of its session, so a push lap was having its car built from the session's
1:55 recharge laps and was then asked to reproduce a 1:39. It came back 1:48,
a 9.6% error, on the two laps whose map the logger had recorded perfectly.

That is not the model being wrong. It is the harness pooling laps the app never
would, and it only appeared once somebody drove the protocol properly.

Two corrections, both making the harness match what the tool actually does:

**Fit through the pooling rule.** `session.select` is what the app uses, and the
harness now uses it too, so a push lap is fitted on push laps.

**Replay only laps the tool would model.** A recharge lap is deliberately slow —
it gives up time to put charge back — and replaying one against a push-lap
profile measures something nothing here has claimed. An Austin 1:53 came back
as a predicted 1:40, which is the model correctly observing that the lap could
have been driven faster, and not an error in it.

| | laps | mean | worst | bias |
|---|---|---|---|---|
| before | 36 | 1.21% | 3.03% | +0.56% |
| **after** | **39** | **0.93%** | **2.52%** | **+0.07%** |

More laps, less error, and a bias of seven hundredths of a percent — the model
is neither optimistic nor pessimistic across the corpus. Albert Park went from
1.56% to 0.93% and Suzuka from 1.71% to 0.95%; both were being fitted on laps
the app would have discarded.

### The first two laps attributed from the logger's own record

| lap | strat | driven | model | error | source |
|---|---|---|---|---|---|
| 1:39.053 | 1 | 99.053 | 99.362 | +0.31% | cota.ini |
| 1:39.159 | 1 | 99.159 | 98.591 | −0.57% | cota.ini |

Both from the setup the logger named, no snapshot consulted. The mechanism
works end to end on laps driven with it.

---

## Austin, on track

The logger finally recorded a full qualifying run: recharge lap, push lap,
recharge, push, with the maps written beside each one.

**Attribution by record, working on laps driven with it.** Both push laps came
back `STRAT 1 [recorded] from cota.ini` with **no snapshots offered at all**,
`map_fit` skill 0.58 and 0.61. That is the thing this whole round was for.

**Every push lap driven on the new map, against the model at its own charge:**

| lap | battery | driven | model | model is |
|---|---|---|---|---|
| 1:39.053 | 93% | 99.016 | 99.350 | +0.334 |
| 1:39.159 | 89% | 99.159 | 99.457 | +0.298 |
| 1:39.092 | 94% | 99.094 | 99.334 | +0.240 |
| **1:38.722** | 94% | 98.722 | 99.345 | +0.623 |

Four laps, all the same direction: the model is **0.24 to 0.62 s pessimistic**
here. It under-promises, which is the safe way round for something recommending
maps, and it is consistent, which is what matters for ranking one map against
another.

**Old map against new, same car, same laps, matched charge:**

| battery | old | new | new gains | energy |
|---|---|---|---|---|
| 100% | 99.729 | 99.210 | **+0.520** | 12.00 MJ both |
| 94% | 99.831 | 99.336 | +0.496 | 11.76 MJ both |
| 87% | 100.030 | 99.505 | +0.525 | 11.48 MJ both |

Identical energy, half a second apart — a pure distribution win rather than the
search finding more to spend.

**What this does not settle.** The only push lap recorded on the old map is a
1:39.925 at 87%, which charge-corrects to about 1:39.73 at 94% — a measured gap
near 1.0 s against a predicted 0.50. But the driver's own best on the old map
was a 1:38.867 that the valid-lap filter threw away, and if that stands the gap
is nearer 0.15 s. One recorded lap on one side is not an A/B, and the honest
statement is that **the new map is not slower and the driver's best ever lap is
on it.** Settling the size needs the old map driven again.

---

## The Albert Park super-clip A/B, on the user's disk

Super-clip being worth 1.082 s was measured inside the model: the solver
allowed to clip against the same solver forbidden to. That is a real result
about the *search*, and it has never been driven.

An A/B is the right test, because the model's own bias cancels — both maps come
out of the same model, off the same laps, and only the availability of
super-clip differs. Whatever the model is wrong about, it is wrong about both.

Built from Albert Park's largest usable session (13 September, 4 laps pooled,
best 1:19.577 — the newest session pools only two):

| | lap | deployed | recovered | clip zones |
|---|---|---|---|---|
| allowed to clip | **81.182** | 6.00 MJ | 6.01 MJ | 2 |
| forbidden | 82.305 | 5.10 MJ | 5.13 MJ | 0 |

**+1.123 s**, and the mechanism is visible in the energy: clipping recovers
0.88 MJ more than braking alone can, and spends it. The two zones are
850–982 m at 350 kW and 3880–3992 m at 250 kW.

Written as two new setups, `melbourne_clip.ini` and `melbourne_noclip.ini`,
both copied from the user's own `melbourne.ini`, which is untouched. Checked
item by item: **29 setup values differ between them and all 29 are in
deployment map 2.** Not one thing else — same STRAT 1, same STRAT 3, same
everything mechanical, and STRAT Map pointing at 2 in both.

Worth stating: those maps are built on a 13 September profile, slower than the
driver's current pace. Both sides share it, so the comparison holds; the maps
themselves are not what a fresh session would produce.

---

## Super-clipping, on track

The first claim from any of this work to be tested against a stopwatch rather
than against itself. Two setups differing in 38 values, all 38 in the race map,
driven in three stints so track evolution could not hide inside the answer:

| stint | | laps | mean | best |
|---|---|---|---|---|
| 15:09 | the driver's own map | 4 | 82.244 | 81.710 |
| 15:20 | **clip** | 4 | 81.652 | 81.329 |
| 15:59 | **noclip** | 4 | 82.232 | 82.055 |
| 16:09 | **clip** | 3 | 81.428 | 81.120 |

**The no-clip stint sits between two clip stints in time and is slower than
both.** The track was getting quicker across the session — the later clip stint
beat the earlier one by 0.22 s — which works against the effect rather than for
it, so if anything this understates it.

| | predicted | measured |
|---|---|---|
| best to best | +0.928 s | **+0.936 s** |
| mean to mean | +0.928 s | +0.676 s |
| clean laps only, mean | — | +0.708 s |
| extra energy deployed | +0.74 MJ | +0.50 MJ |

Best-lap to best-lap the prediction is right to eight thousandths of a second,
which is luck at this sample size but not the wrong kind of luck. On means the
model overstates the gain by about a quarter, and it overstates the extra energy
by a third — the mechanism is real and a little smaller than claimed.

So the earlier conclusion holds and is now more than an argument about the
solver's action space: **super-clipping was unreachable, it is reachable, and it
is worth about seven tenths of a second at Albert Park.** The 1.082 s figure
from the model-only test was the right order and the optimistic end of it.

### What this does not settle

The regulations describe super-clipping as the alternative to lift-and-coast,
which costs time *and* switches the active aero from Straight Mode to Corner
Mode. This model has no active aero and takes the driver's throttle trace as
given, so in it the alternative to clipping is simply harvesting less, which is
free. That asymmetry would bias the search against clipping wherever the car
genuinely needs energy braking cannot supply — and a qualifying lap, which is
exactly that case, has not been tested this way.

---

## Why the tool does not super-clip the way a real 2026 car does

The driver's objection was direct: real cars super-clip throughout a lap at
Albert Park, and this tool puts one 283 m zone on the run to the line. The
regulations are on his side —

> Super clipping is used to supplement traditional regenerative braking,
> especially at circuits with few heavy-braking zones like Albert Park…
> Drivers are expected to use their entire battery's capacity during a single
> lap, but it takes just under 20 seconds of regenerative braking to recharge
> it. The teams have to make up that deficit every lap, which is where super
> clipping fills that gap.

**The car in this mod has no such deficit.** Measured from the driver's own
telemetry, fifteen Albert Park laps:

| | |
|---|---|
| time off the throttle | 26–31 s a lap |
| recovered off the throttle | **5.2–5.8 MJ** |
| recovered at full throttle (super-clip) | 0.01–0.39 MJ |
| battery capacity | 4.0 MJ |

Lifting and braking alone return **more than a full battery every lap**. A real
2026 car gets roughly 4 MJ from just under 20 s of braking and is still short;
this one is not short at all, so the thing that forces real drivers to clip
constantly does not apply to it. That is a difference between the mod and
Formula 1, not between the model and the mod.

And the search is no longer blind to it. Started from a map clipping 75%, 55%
or 30% of the lap, with every move available, it walks back to **zero clip
zones** every time and lands on the same lap time. That is its answer, not an
artefact of where it began.

## What the same investigation did turn up

**A fragment can destroy a whole session.** `select` takes the session's best
time from the filename and keeps everything within 3% of it. One 35.525 s file
among seventeen Albert Park laps — a partial lap the logger wrote with the
wrong track length — set the bar at 36.6 s, and the session collapsed to that
one broken lap. `lap.problem` catches the file, but long after `select` has
thrown the session away. Anything a third quicker than the session's own median
is now treated as a fragment, and the session went from 1 usable lap to 16.

**The race map's battery floor costs more than super-clipping ever could.**
`min_store_fraction = 0.25` keeps a quarter of the battery in hand all lap, on
the reasoning that a repeating lap neutral at zero is still neutral and leaves
nothing for a defensive burst. On this session it is expensive:

| floor | lap | deployed | recovered | clip zones |
|---|---|---|---|---|
| 25% (in the code) | 82.274 | 4.96 MJ | 5.55 MJ | 0 |
| 10% | 81.813 | 5.52 MJ | 5.55 MJ | 0 |
| 0% | **81.612** | 5.55 MJ | 5.55 MJ | 0 |

**0.66 s**, and 0.6 MJ of recovered energy held back unspent. The car is
energy-limited at 5.55 MJ and the floor was stopping it reaching that. This is
a judgement call rather than a bug — a race map that empties the battery has
nothing to defend with — but it is a second and a half of the driver's own lap
time, and it is larger than the super-clip effect that prompted the question.

---

## The race map's battery floor, removed

`min_store_fraction = 0.25` refused any race map whose store dipped below a
quarter. Measured across seven circuits it costs **0.99 s a lap** — and not
because a quarter is too much:

| floor | mean lap | mean low | against 25% |
|---|---|---|---|
| **none** | **88.377** | 4% | **+0.987 s** |
| 5% | 89.260 | 19% | +0.104 s |
| 10% | 89.229 | 17% | +0.135 s |
| 15% | 89.245 | 22% | +0.119 s |
| 25% | 89.364 | 28% | — |

Five, ten and fifteen percent all land within 0.13 s of twenty-five. **The cost
is the constraint existing, not its size**, because it is a rejection rather
than a preference: a map that touches the floor once is discarded whole, and a
4 MJ battery cannot cycle 8 MJ of per-lap allowance without going low. The
floor was capping how much energy could move, not how much was held back.

Its own justification does not survive either. The worry was that a map neutral
at zero would give a quick first lap off a full battery and slow ones after —
true, and measured at 0.17 to 0.98 s. But the settling lap already makes the
reported time the repeating one, and **the repeating lap with no floor is still
a second quicker than the floored map**. What remained was a reserve for
attacking, and this tool hands the driver three maps: the reserve is STRAT 1.

One idea that did not work, recorded because it looked obvious: scoring the map
at several starting charges, the way the qualifying search does. It changes
nothing for a race map — 81.610 against 81.612 — because that search settles,
and settling erases the charge it started from. `charge_fractions` is a
one-shot mechanism and cannot do this job.

What the change costs is a lap arrived at with half the planned charge:
**+0.08 to +0.66 s**, once, before the store settles again. The card now says
when a map runs the battery to nothing, because a driver reaching for an
overtake at that point will find nothing there.

## Deployment through a corner taken flat out

Reported from the car, at Austin: the map spends nothing through the long right
in the final sector and saves it for the two short corners after, which looks
backwards. More time at full throttle should be worth more energy, not less.

It is worth nothing, and the reason is a ceiling rather than physics. Forcing
350 kW through the whole corner changes the simulated speed by **0.0 kph at
4500, 4600, 4700, 4800 and 4880 m**, and costs 0.033 s for the energy wasted.
In `core/profile.py` a slot is left uncapped only where

    power_limited = gas >= 0.99 and brake < 0.01 and not cornering

so a corner taken flat is capped at the speed the driver managed. Deployment
there cannot move the car by construction. The driver is not choosing that
speed; they are flat out and the car is going as fast as that much power takes
it round that radius. What the telemetry shows through the corner:

| dist | speed | gas | lat g | share of lat_max | grip left |
|---|---|---|---|---|---|
| 4400 | 211.0 | 1.00 | 2.26 | 0.55 | 0.96 |
| 4500 | 236.4 | 1.00 | 3.29 | 0.80 | 0.84 |
| 4600 | 236.3 | 1.00 | 3.46 | 0.84 | 0.86 |
| 4700 | 240.4 | 1.00 | 2.98 | 0.72 | 0.89 |
| 4800 | 246.4 | 1.00 | 2.31 | 0.56 | 0.94 |
| 4880 | 251.2 | 1.00 | 2.14 | 0.52 | 1.00 |

At 4800 m the tyre is at 56% of the lateral load it reaches elsewhere on the
lap. There is grip in hand, and the argument for deploying there is a good one.

### Two ways to let it move, both measured

**Gaining.** Lift the ceiling where the driver is flat *and still accelerating*,
and let the force balance decide. Adjacent samples cannot answer "still
accelerating": at 236 kph the logger writes one every half metre and a car
pulling away at half a g gains 0.01 kph in that distance, under the noise the
smoothing leaves. Ten samples, about four metres, does answer it — a first
attempt at 1.0002x per sample never fired at all and measured nothing.

**Ellipse.** The same, plus lateral load that answers to simulated speed:
`grip_left` reads the load straight off the lap, so a model running faster than
the driver through a corner is charged the driver's cornering load and none of
its own. Same radius, more speed, more lateral g, and grip runs out on its own.

Both make the model worse. On the holdout corpus, split by laps quicker and
slower than their own session — because loosening a ceiling always flatters a
corpus of quick laps, the trap that made a `lat_fraction` of 0.9 look like an
improvement:

| | all 59 | quicker than session | slower than session |
|---|---|---|---|
| capped, as it stands | **0.78%** (+0.19%) | 0.74% | **0.86%** |
| gaining: uncapped | 0.80% (+0.08%) | 0.74% | 0.92% |
| gaining + ellipse | 0.79% (+0.01%) | 0.71% | 0.95% |

The bias does improve, from +0.19% to near zero: the ceiling makes the model
slightly too slow, as the argument predicts. But the error *grows* on the half
the selection effect cannot flatter, 0.86% to 0.95%. That is a term with the
right sign and the wrong magnitude, and the freed model shows where it goes
wrong — through the same corner it invents **273 kph where the driver did
246**. The model is staying as it is.

### Left to the car

That answers whether to change the model, not whether the corner is worth
deploying in. `cota_longright.ini` carries both answers on the wheel, written
from `cota.ini` and leaving it untouched:

* **strat 1** — the map the search chooses now: 200 kW to 4477 m, nothing
  through the long right.
* **strat 2** — the map the freed model chooses: 250 kW to 4709 m, 232 m of
  deployment through the long right.

Both deploy **12.00 MJ** and both finish on an empty battery, so the only thing
that differs is where the energy goes. The model as it stands expects strat 1 to
win by **0.291 s**; the freed model expects strat 2 to win by about 0.2 s. Lap
to lap scatter in that session was 0.29 s, so this needs several laps each and
an A/B/A stint order, as the super-clip test did.

## The long corner, settled on track

Four laps at Austin, strat 1 / 2 / 2 / 1 -- the balanced order, so a track
rubbering in through the run adds the same to each pair and cancels. The logger
recorded the map for every lap and all four ran from `cota_longright.ini` with
no invalid samples. The maps did what they were built to do: **0.01 MJ** through
the long right on strat 1, **1.01 MJ** on strat 2.

Through the corner itself, 4477-4885 m:

| lap | map | enters | exits | time | deployed |
|---|---|---|---|---|---|
| 01 | A | 229.1 | 250.1 | 6.184 | 0.01 MJ |
| 02 | B | 231.8 | 265.8 | **5.720** | 1.01 MJ |
| 03 | B | 231.9 | 267.4 | **5.726** | 1.00 MJ |
| 04 | A | 228.1 | 251.0 | 6.182 | 0.01 MJ |

**Deploying through the corner is worth 0.460 s and sixteen kph of exit speed**,
with 2 ms of scatter inside each pair and no overlap between them. The model
said it was worth **zero**. It was wrong, and the report from the car was right.

Whole-lap time cannot be used here and is recorded only so nobody tries: the
battery drained across the run, arriving at the corner with 100%, 58%, 55% and
46%, so the -0.769 s whole-lap difference is mostly drift. The corner
measurement is immune to it -- both B laps arrived with *less* charge than both
A laps and were still half a second quicker through it.

### Which term was hiding it

The ceiling, and only the ceiling. Holding everything else and removing each
suspect in turn:

| | map A | map B | B - A |
|---|---|---|---|
| measured on track | 6.183 s, exits 250.6 | 5.723 s, exits 266.6 | **-0.460 s** |
| model as it stands | 6.504, exits 248.8 | 6.504, exits 248.8 | +0.000 |
| without the grip clamp | 6.435, exits 249.8 | 6.435, exits 249.8 | +0.000 |
| without the speed ceiling | 5.495, exits 265.7 | 5.396, exits 268.3 | -0.099 |

The friction ellipse is not binding in that corner at all -- removing it changes
nothing. The ceiling is doing all of it.

### Why neither answer is the fix

Taking the ceiling out does not fix the model, it breaks it differently. With
the ceiling **on**, map A is 0.32 s slow and map B is 0.78 s slow. With it
**off**, map A becomes 0.69 s *fast* -- the model reaches 265.7 kph on a lap
where the car managed 250.6 with barely any deployment -- while map B lands
near the measurement.

So the model responds far too weakly to power in that corner (+2.6 kph for the
same megajoule that gave the car +16) while its no-deployment baseline is far
too strong. The ceiling was hiding both errors by pinning the answer to the
driver's own lap, which is why lifting it made the corpus worse: it removed the
mask without fixing what was underneath. That is consistent with the holdout
result and with this one at the same time, and it means the real work is in the
force balance at 250 kph, not in the ceiling that covers for it.

This A/B is the regression case for that work: a corner, two maps, one
megajoule of difference, 2 ms of measurement noise, and a known answer of
-0.460 s.

## The controlled A/B, and what it caught in the model

One lap each with a recharge lap between them. The control worked: both timed
laps crossed the line on a full battery, **94.8% and 94.1%**, at **193.2 and
194.3 kph**, and the logger recorded `STRATS=1` and `STRATS=2` -- each timed lap
ran on one map start to finish, every switch confined to the lap between.

**Map B is 1.233 s quicker**, 98.794 against 100.027.

With one lap each that number cannot separate the map from the driver on its
own. Where the time appears can. Cut into 500 m pieces, against where the two
maps differ:

| metres | A | B | B - A | B's map |
|---|---|---|---|---|
| 1000-1500 | 7.221 | 7.245 | +0.023 | same |
| 1500-2000 | 9.933 | 9.944 | +0.011 | same |
| 2000-2500 | 7.826 | 7.567 | **-0.258** | +50 kW |
| 2500-3000 | 10.447 | 10.219 | **-0.228** | +100 kW |
| 3000-3500 | 5.870 | 5.609 | **-0.261** | +100 kW |
| 4000-4500 | 12.184 | 12.158 | -0.026 | same |
| 4500-5000 | 7.819 | 7.399 | **-0.421** | the long right |
| 5000-5429 | 9.035 | 8.993 | -0.043 | same |

**Where the maps agree, the two laps agree to about 0.02 s per 500 m.** That is
the driver, and it is the noise floor of this measurement. Every place the maps
differ is ten to twenty times it, and every one of them moves in the direction
the map does. This is the map, not the lap.

The corner on its own came out at **-0.435 s** with B exiting 13.5 kph faster --
an independent repeat of the -0.460 s and +16 kph measured the day before, from
a different battery state, on a different pair of laps.

### What it caught

The two maps were built to deploy the same energy and the model expected both to
spend about **11.8 MJ**. The car spent **8.79 MJ on A and 10.75 MJ on B**, and A
finished the lap still holding **74%** of its battery.

*(Corrected: these were first written up as 5.00 and 7.98 MJ. The logger's
deployment counter carries the previous lap's total for the first two to four
samples after the line before resetting, so subtracting `deployed_kj[0]` takes
a whole lap's deployment off the answer. The shortfall is 3.0 MJ on A and 1.0 on
B, not the factor of two first reported. Every other number here is measured
from differences later in the lap and is unaffected.)*

So three things are wrong at once, and only the first was known:

1. the corner is worth 0.44 s and the model scores it at zero;
2. map A is 3.0 MJ short of what the model expected it to deliver, and 2.3 MJ of
   that is one split that delivers nothing at all;
3. a qualifying map that ends the lap on 74% is not a qualifying map, and
   nothing in the tool noticed.

Part of B's 1.233 s is therefore simply that it spent two more megajoules than A
did, which is a real advantage of map B but not an answer to where energy should
go. The corner measurement is unaffected -- it is metres long, both laps had
charge in hand through it, and it repeats across two sessions -- but the
whole-lap number should be read as "B is the better map", not as "deploying in
the long right is worth 1.2 s".

## Why the maps under-deploy, and why that is the same question

Two causes, and the second is the one reported from the car.

### The first split of the lap delivers nothing

Over the first 519 m at Austin the driver is at full throttle, accelerating from
195 to 268 kph, for 7.7 seconds. Both maps command power there -- 300 kW on A,
200 kW on B. The car deploys **zero**:

| metres | gas | kph | deployed, A | deployed, B |
|---|---|---|---|---|
| 5 | 1.00 | 195 | 0.0 | 0.0 |
| 200 | 1.00 | 241 | 0.0 | 0.0 |
| 500 | 1.00 | 268 | 0.0 | 0.0 |
| 540 | 0.00 | 255 | 1.2 | 0.5 |
| 700 | 0.69 | 116 | **17.7** | **17.9** |
| 760 | 1.00 | 178 | **268.0** | **259.9** |

Deployment begins at 700 m -- after the driver lifts for turn one and gets back
on the power. This is the demand latch: power *demand* cannot be raised while
under throttle. The lap begins with the car already flat out, so the new lap's
map cannot take effect until the first lift, and the biggest uninterrupted
acceleration on the circuit is dead in every map the tool writes. It asked for
2327 kJ on A and 1558 kJ on B and got none of it.

The model does not know. `reset_zones` for Austin is **empty**, so the simulation
deploys from metre zero and counts energy the car will never spend.

### The long right is the second biggest place to put energy, and map A declines it

Ranking every stretch of continuous full throttle by how much energy it could
absorb at 350 kW:

| metres | seconds | MJ at 350 kW | covered by map A |
|---|---|---|---|
| 2611-3583 | 13.0 | 4.55 | all of it |
| **4289-4882** | **9.7** | **3.41** | **32%** |
| 1-519 | 7.7 | 2.70 | all of it, and latched dead |
| 721-1211 | 7.6 | 2.65 | 73% |
| 1990-2407 | 6.4 | 2.24 | all of it |
| 5005-5168 | 2.7 | 0.93 | 44% |
| 5326-5429 | 2.1 | 0.75 | 99% |

**The long right is the second largest energy sink on the lap and map A uses a
third of it.** With the third largest latched dead at the line, map A is left
without anywhere to put its battery, and finishes the lap on 74%. Map B takes
439 m of that stretch and spends 1.75 MJ there, and finishes on 24%.

So the two questions have one answer. The ceiling tells the search a flat-out
corner is worth nothing; the search declines the second biggest energy sink on
the circuit; and the map it writes then cannot spend the battery it was given.
The under-deployment is not a separate bug from the corner -- it is what the
corner error does to the map.

Map B is not the fix either: it stops deploying at 4709 m and leaves the last
173 m of that stretch, about a megajoule of capacity, unused.

## Taking on the two fixes, and what stopped one of them

### Teaching the model the latch makes the maps worse, on its own

The latch is real. Every one of seven Austin laps deployed **exactly zero**
before the first lift, and across the corpus **160 of 216 laps** deployed under
50 kJ there. The simulation primes the latch by walking the last 600 m of the
lap under the map being scored, so it starts the lap entitled to whatever that
map's last split asked for -- which is not how anyone arrives, because the lap
before a flying lap runs a different map that asked for nothing.

Two attempts at the variant were mislabelled before one of them bit, which is
worth recording. Deleting the priming loop does **not** make the car arrive with
nothing: it leaves `throttle_time` at zero, which the main loop reads as the
throttle having just been applied, and *grants* the opening split whatever it
asks for -- the opposite. And setting the demand before the priming loop is
wiped by the loop itself. Only a change after the loop does anything, and the
proof that it does is that the searched map changes at all.

Measured on the holdout, arriving latched is a small trade: energy error 1.18 ->
**1.09 MJ** and its bias +0.38 -> **+0.19 MJ**, against lap time 0.76% -> 0.78%.

But the map it writes is worse:

| | lap | deployed | ends on |
|---|---|---|---|
| believing the line deploys | 98.672 | 11.76 MJ | 0% |
| knowing it arrives latched | 99.421 | 11.08 MJ | **17%** |

Told it cannot spend at the line, and still told a flat-out corner is worth
nothing, the search has nowhere left to put the energy and leaves 17% of the
battery unspent. **The two faults are one fault and have to be fixed together.**
The latch change is not shipped.

### The check that would have caught it is not the obvious one

A warning on the predicted end-of-lap charge would not have caught the Austin
map. That map was **predicted to finish empty and finished on 74%**: the model
was wrong about where the energy went, and a check on the model's own prediction
cannot catch the model being wrong. Checking the driven laps instead says
nothing either -- 66 of 87 strat-1 laps in the corpus finish above 10%, mostly
because they did not start full.

What is checkable is the request. Energy commanded before the driver's first
lift is energy the car has been measured refusing, whatever the model believes:

* **map A: 2.30 MJ, 19% of everything it spends**
* map B: 1.53 MJ, 11%

Both waste it and only one is dramatic. Shipped at a tenth so both are named,
with `first_lift_m` reading the pooled profile so it is where this driver lifts.
On a fresh solve of the same session it fires: *"1.9 MJ of this map -- 11% of
everything it spends -- is commanded before the first corner, and the car will
refuse most of it."*

A second note covers a qualifying map **predicted** to finish holding 10% or
more, which is what the latch-aware search produced. It is the weaker of the two
and is documented as such.

## The force balance, fixed

The corner and the latch each cost accuracy alone and cost nothing together.
That is the whole result:

| | corner A | corner B | a megajoule worth | corpus | long right | asked at the line | ends on |
|---|---|---|---|---|---|---|---|
| **measured on track** | **6.183** | **5.723** | **-0.460 s** | | | | |
| as it stood | 6.250 | 6.193 | -0.057 s | 0.76% | 221 m | 1.90 MJ | 0% |
| ceiling lifted only | 6.155 | 5.800 | -0.355 s | 0.78% | 302 m | 1.92 MJ | 0% |
| latch only | 6.250 | 6.193 | -0.057 s | 0.78% | 137 m | 1.15 MJ | **17%** |
| **both** | **6.155** | **5.800** | **-0.355 s** | **0.76%** | 229 m | **1.53 MJ** | **0%** |

The model now values a megajoule through the long right at 0.355 s against the
0.460 s the car measured -- **77% of it, from 12%** -- and predicts the corner
itself to 0.03 s on the lap it was checked against and 0.08 s on the lap it was
not. Holdout accuracy over 64 laps is unchanged at **0.76%**.

### Two wrong turns worth keeping

**Cornering drag was the wrong idea.** The signature looked unmistakable: too
fast with no power, barely moved by power, which is a car sitting on its
drag-limited equilibrium. So a term was added for the induced drag of a tyre
carrying lateral load, `K * m * a_lat^2 / g`, fitted on the lap with no
deployment and asked to predict the lap with one. Fitted, **K wants to be zero**:
with the ceiling lifted the force balance already puts that lap within 0.03 s
and 1.3 kph of the car. There was nothing for it to fix.

What was hiding that is the **friction ellipse** -- the earlier idea of making
lateral load answer to simulated speed. It chokes the drive exactly as the model
speeds up, so extra power buys nothing, and it made every freed-model result
read as if the corner were still dead. Lift the ceiling and leave the grip term
alone and the corner comes right on its own.

**And the A/B harness was serving stale bytecode.** A sweep of eight values of K
returned eight identical rows. The harness writes each variant to one filename
and imports it; two variants differing only in a constant of the same length --
`0.01` against `0.05` -- produce files of identical size, and when the rewrite
lands inside the same mtime tick Python reuses the cached `.pyc`. It now writes
a fresh filename per load and invalidates the import caches. The earlier ceiling
result was re-run against the fixed harness and is unchanged (0.83 -> 0.88 ->
0.92 on the unselected half), so nothing built on it moves.

### What the checks now hold

`deployment in a corner taken flat out is worth something` builds a split
through the first flat, still-gaining, laterally loaded stretch it can find and
requires 350 kW to buy speed there. It caught its own first draft: the split
opened at the corner, where the driver was already at full throttle, and the
demand latch correctly refused it -- the check was measuring the latch, not the
corner. Splits now open before the preceding lift, which is the advice the tool
gives its own users. It reports **13.5 kph** on Austin against the 16 measured.

## The fixed model on track: a dead heat, and why that is useful

The first map the tool wrote with the corner and the latch both fixed, against
map B, which had driven a 98.794. Recharge lap between, switched at the line,
`STRATS=1` and `STRATS=2` in the logger, both from `cota_v2.ini`.

**98.673 against 98.668.** Four thousandths apart.

Not a setup that failed to load. The two maps did entirely different things:

| | deployed | ends on | long right | exits it |
|---|---|---|---|---|
| the new map | **11.39 MJ** | **9.4%** | 1.53 MJ | **277.0 kph** |
| map B | 10.63 MJ | 28.7% | 1.00 MJ | 267.4 kph |

The corner fix is confirmed a third time and on a third pair of laps: half a
megajoule more through the long right is worth **0.236 s and 9.6 kph of exit
speed**. The under-deployment fix works too -- the new map finishes on 9.4%
where map B leaves 28.7% in the battery.

And it bought nothing, because of where the energy came from. Timed off the
logger's own clock rather than by integrating speed, which matters: integrating
disagreed with the recorded lap by up to 0.2 s and would have supported a
conclusion the car does not.

| metres | energy the new map adds | time it gains | rate |
|---|---|---|---|
| 500-1000 | +0.22 MJ | +0.065 s | 0.30 s/MJ |
| 2000-2500 | +0.26 MJ | +0.056 s | 0.22 s/MJ |
| **2500-3000** | **-0.28 MJ** | **-0.116 s** | 0.41 s/MJ lost |
| 4000-4500 | +0.37 MJ | +0.135 s | 0.36 s/MJ |
| **4500-5000** | **+0.46 MJ** | **+0.236 s** | **0.51 s/MJ** |
| **5000-5428** | **-0.16 MJ** | **-0.160 s** | **1.00 s/MJ lost** |

The long right returns 0.51 s per megajoule, the best rate on the lap bar one.
**The last two corners return 1.00 s/MJ -- twice as much -- and the search took
energy out of them to pay for the corner.** 2500-3000 returns 0.41 and lost
energy too. Gains of 0.49 s against losses of 0.54 s, and the lap comes out level.

So the corner is no longer the problem. The problem is now the other end: the
model undervalues deployment out of slow corners, where the car is at its most
responsive to power, and is willing to sell them to buy a fast one. That is the
next thing to measure, and it comes with its own ground truth already.

One anomaly, recorded rather than explained: 3500-4000 has the new map spending
0.04 MJ *more* and running 0.141 s *slower*. The driver's noise floor where two
maps agree is about 0.02 s per 500 m -- 0-500 came in at 0.011 s and 1500-2000
at 0.029 s -- so 0.141 s is seven times it and in the wrong direction. One lap
each cannot separate that from a mistake in a technical section.

## Balance: the search has not levelled its own rates

Reported from the car: the maps look concentrated, favouring one long traction
zone, and a spread across the high-speed corner, the slow-corner exits and the
run to turn one might be worth more than piling into any one of them.

That has an exact form. At an optimum the last megajoule spent in each place
returns the same time; if one zone returns twice another, energy should move
until the rates level. And the Austin map finishes the lap on empty, so the lap
is **energy-limited** -- the total is fixed and the only question is placement.

### The search stops short of its own optimum

Moving 50 kW from one zone to another in the map the search chose, **nine single
moves make the model's own lap time faster**, the best by 0.098 s:

| out of | into | change | the car rates them |
|---|---|---|---|
| 2000-2500 | 2500-3000 | **-0.098 s** | 0.22 -> 0.41 s/MJ |
| 5000-5428 | 2500-3000 | -0.093 s | **1.00 -> 0.41 s/MJ** |
| 4000-4500 | 2500-3000 | -0.087 s | 0.36 -> 0.41 s/MJ |
| 4500-5000 | 2500-3000 | -0.079 s | 0.51 -> 0.41 s/MJ |
| 4000-4500 | 500-1000 | -0.066 s | 0.36 -> 0.30 s/MJ |

This is not a physics fault. By its own reckoning the search left time on the
table, which means it is stopping before it has equalised anything.

### And where it wants to move energy is backwards

Every one of the best moves feeds **2500-3000** or **500-1000**, which the car
rates at 0.41 and 0.30 s/MJ. Two of them pay for it out of **5000-5428, which
the car rates at 1.00 s/MJ, the highest on the lap.** The model is selling the
most valuable stretch of the circuit to buy two of the cheapest.

Measured rates from the dead-heat pair, best first:

| metres | s/MJ | what it is | rests on |
|---|---|---|---|
| 5000-5428 | **1.00** | the last two corners | 0.16 MJ |
| 4500-5000 | 0.51 | the long right | 0.46 MJ |
| 2500-3000 | 0.41 | the back straight | 0.28 MJ |
| 4000-4500 | 0.36 | approach to the long right | 0.37 MJ |
| 500-1000 | 0.30 | out of turn one | 0.22 MJ |
| 2000-2500 | 0.22 | | 0.26 MJ |

The ordering wants reading with care. A rate is a ratio, and the two at the top
of it rest on the smallest energy differences on the lap -- 0.16 MJ for the last
corners against 0.46 for the long right -- so the long right's 0.51 is the more
solid number and the 1.00 is the more likely to move with more laps.

### The run to turn one is the latch, not a choice

Nothing is deployed from the line to turn one because nothing *can* be: the car
arrives flat out on the recharge map and demand cannot be raised under throttle.
Switching to the qualifying map before the final corner establishes the demand
during that lift, and the car crosses the line already entitled to it:

| switch | lap | crosses at |
|---|---|---|
| as you cross the line | 98.510 | 190 kph on 99% |
| before the final corner | **98.388** | 211 kph on 87% |

**0.122 s**, and free -- it costs a habit, not a megajoule. Worth noting that
before the latch was modelled this same comparison read 1.145 s; the fix made
the estimate an eighth of what it was, and more believable for it.

## Why nothing has ever deployed on the front straight

Reported from the car mid-session: strat 1 was not deploying on the front
stretch. It was not, and neither has any map this tool has ever written. The
cause is not the driver, the switch, or the search declining it.

A lap begins with the car already at full throttle, so the demand it carries
across the line is whatever the **last split of the lap** established -- and
demand cannot be raised while the driver stays on the throttle. Six maps at
Austin, with the deployment measured over the first 525 m:

| setup | map | opening | final | final > opening? | deployed |
|---|---|---|---|---|---|
| cota_longright | 1 | 300 | 300 | no | 1 kJ |
| cota_longright | 2 | 200 | 200 | no | 0 kJ |
| cota_v2 | 1 | 200 | 200 | no | 0 kJ |
| cota_v2 | 2 | 200 | 200 | no | 0 kJ |
| cota_balance | 1 | 190 | 190 | no | 0 kJ |
| **cota_balance** | **2** | **199** | **350** | **YES** | **1364 kJ** |

**Equal is not enough.** Every map before this one asked for the same power at
both ends of the lap, so the opening was never a reduction and was never
granted. The one map that deployed there did so by accident: its last split was
350 kW because that is where the car pays best, and the opening asked for 199.

Two consequences. The first seven seconds of the lap -- the third largest energy
sink on the circuit, 2.7 MJ of capacity -- has been thrown away by every map the
tool has produced. And **a map of uniform power can never use its own opening**,
which is not a flaw in the idea of spreading energy but a trap in how it was
built: opening equals final by construction.

`open_the_lap` now raises the last split above the opening before a map is
returned. The end of a lap is the run to the line, where power is worth having
anyway, so the raise costs nothing it was not already worth spending.

Honestly held: the mechanism behind the strictness is not pinned down, and only
one of the six observations is a positive case. It could be "final > opening",
or it could be "final at 350". The margin is 50 kW rather than 1 so the answer
is unambiguous either way, and the discriminating test is cheap -- the same map
with its last split at 250 instead of 350.

### What it cost the comparison

The even-against-weighted test is spoiled by it. Strat 1, uniform at 190 kW,
could not fire its opening and finished the lap on 83% having spent 8.0 MJ;
strat 2 spent 11.3 and finished empty. The 1.4 s between them is mostly the
3.3 MJ, not the placement, so **the balance question is still open**.

What was won anyway: **1:38.467**, a new best, on the weighted map, against a
previous best of 1:38.668.

## Scoring the model on differences, and what that found

`validate_model.py` asks whether the model can reproduce a lap on the map it was
driven on. The optimiser never asks that: it asks which of two maps is quicker
and by how much. `validate_deltas.py` scores that instead, on every pair of maps
driven in the same session -- four sessions at Austin and the clip/noclip/clip
stint at Albert Park, ten comparisons in all -- with each lap matched to its map
through the setup the logger recorded, the car fitted only on *other* sessions
so nothing leaks, and each lap cut into 500 m pieces timed off the logger's own
clock. Where two maps agree, the same driver repeats a piece to 0.075 s; that is
the floor.

It corrected one claim before it found anything. The "1.224 s predicted, level
on track" example quoted for `cota_v2` came from how that one prediction was set
up -- a profile pooled from the same session and a starting charge assumed
rather than measured. Done properly the model gets that pair to 0.033 s.

### Four errors in how power is delivered

Traced zone by zone, the model over-predicted a uniform map by 2.1 MJ and got a
weighted one right only because two errors cancelled -- it spent phantom energy
early, ran the battery dry, and delivered nothing at the end. Each was then
measured directly from the car's deployment counter on 33 laps with known maps.

**Partial throttle.** Below the 60% knee the car delivers nothing; above it,
delivery climbs roughly in a straight line to full at 88%:

| throttle | old curve | car |
|---|---|---|
| below 0.60 | 20-66% | **0%** |
| 0.60-0.65 | 72% | 15% |
| 0.70-0.75 | 81% | 45% |
| 0.80-0.85 | 94% | 74% |
| 0.85-0.90 | 98% | 94% |

The old curve contradicted the model's own `DEMAND_RESET_GAS`. It was normalised
against full-throttle delivery without regard to the command, so a floor or a
latch read as a throttle effect. The first second back on and later agree, and
so do qualifying and race maps.

**The floor holds about 1.3 s, then ramps.** A split asking 100 kW delivers 200
for 1.0-1.3 s, then 192, 148, 100. One asking nothing delivers 197, 150, 101,
then stops. The model held one second and, below 210 kph -- which is every slow
corner exit -- dropped at once.

**The latch clock starts at the knee.** The model timed the floor-and-latch
window from 20% throttle. At Austin a 250 kW split began 0.29 s after the knee;
the model's window had already run out during the squeeze, and it refused the
split. The car delivered 1.8 MJ through the long right; the model 0.12.

**The line.** See below.

### The start/finish line, corrected

The demand carried across the line comes from whichever map was live when the
driver last got back on the power -- not necessarily this lap's map. The opening
is granted only if it asks for **strictly** less:

| carried vs opening, laps with charge to spend | result |
|---|---|
| more: 300 vs 200, 350 vs 199, 350 vs 199 | granted, 1.36-1.44 MJ |
| equal: 190 vs 190, 200 vs 200 | refused |
| less: 0 vs 200, twice | refused |

`min()` would have delivered the opening in full on the equal cases. This
replaces the 1.1.0 change that simply zeroed the demand at the line, which was
right for the common case (recharge lap, wheel turned at the line, nothing
carried) for the wrong reason.

**Two claims from the previous session were wrong.** "Seven of seven Austin laps
deployed exactly zero before the first lift" used counter arithmetic the stale
first samples defeat; lap02 of that run deployed 1.37 MJ, because the lap before
ran a map asking 300 against its opening of 200 -- the rule, not an exception.
And the laps that looked like exceptions to the rule (350 carried, 199 refused)
each started with the battery at 0.6%, following a qualifying lap that had
emptied it.

### Replaying laps the way they were entered

Both harnesses now reconstruct how each lap crossed the line: the previous lap
is read, and the map that matters is the one live during the floor-hold window
after the last time the throttle crossed the knee. Not the map just before the
last switch -- turning the rotary from 3 to 1 passes through 2 for a moment,
and taking that as the arrival credited five laps with 300 kW they never
carried. Where no previous lap can be established the lap is taken to have
carried nothing, which is what all seven such laps in the corpus did.

### Result

| | lap error | bias | energy, quali / race | map differences | pieces | direction |
|---|---|---|---|---|---|---|
| 1.1.0 | 0.72% | +0.21% | +0.69 / -0.15 MJ | 0.404 s | 0.137 s | 79% |
| **now** | **0.70%** | **-0.53%** | **+0.28 / -0.06 MJ** | **0.213 s** | **0.081 s** | **89%** |

The error in what the optimiser actually uses is halved, and energy is close to
right for both kinds of map. One number moved the wrong way, and it is left
visible rather than tuned away: the model now predicts laps half a percent
**too fast**. Timed piece by piece at Austin, the gain is concentrated at the
slow corner exits -- turn 1 -0.13 s, the 89 kph hairpin -0.12, the 86 kph
corner before the long right -0.15, the last corner -0.10 -- while the back
straight runs slightly slow. With delivery now matching the car's counter, the
likely reading is traction: at 90-150 kph the car cannot put all of that power
down and the model can. Sakhir, all super-clipping race maps, was already
-0.70% before any of this and shows it most. That is the next thing to measure,
and it is the slow-corner question from the other side.

## Rebalancing: mostly the model, not the search

On the 1.1.0 model, nine exchanges of 50 kW between two splits made the model's
own Austin lap quicker, the best by 0.098 s -- evidence that the climb stopped
before levelling its own rates. A lap that ends on an empty battery is
energy-limited, so neither raising one split nor lowering one pays on its own,
and the climb's paired moves only ever traded harvesting for deployment.

A final pass now offers the missing trade -- one power step off one split, one
onto another, every pair simulated at once, the best kept, repeated until none
improves. Measured on the corrected model, qualifying solves:

| circuit | without | with | free exchanges left | solve time |
|---|---|---|---|---|
| Austin | 97.596 | 97.596 | 0 -> 0 | 33 s -> 33 s |
| Albert Park | 79.400 | 79.400 | 0 -> 0 | 21 s -> 22 s |
| Monza | 83.884 | 83.884 | 0 -> 0 | 30 s -> 29 s |
| Suzuka | 89.143 | 89.132 | 1 -> 0 | 27 s -> 30 s |
| Spa | 105.695 | 105.695 | 0 -> 0 | 126 s -> 123 s |

So the nine free moves were the model, not the search: with power delivered
where the car delivers it, the climb already levels its own rates at four
circuits of five. The pass is kept as the guarantee that nothing is left, at a
cost of a few seconds, and is not claimed as a gain it has not shown.

## The Baku Power Unit Information notice

FIA Document 5, 2026 Azerbaijan Grand Prix, 23 September 2026. The table held a
placeholder for Baku from before the notice, three of its figures wrong. Now:

| | before | notice |
|---|---|---|
| recharge, race | 8.0 MJ | **8.5 MJ** |
| recharge, race with Overtake | 8.5 MJ | **9.0 MJ** |
| recharge, qualifying / practice / out laps | 8.5 / 9.0 / 9.0 | unchanged |
| reduction rate, power-limited distance | none | 50 kW/s, 3796 m |
| curves | none | Base - Standard, Base - Overtake, Alt 1 |
| Alt 1 sectors (race) | none | T1-T2 300-600, T3-T12 1500-2870, T15-T16 3700-4050 |
| reset zones | none | exit T19 4650-5600 (**race too**), [exit T20 5600-6000] (Q) |
| higher speed-threshold sectors | none | none |

Exit of T19 is the first reset zone any notice has declared for the race.

### The curves, as drawn

The notice's chart is vector graphics, so the curves were read from its plotted
coordinates rather than off a picture:

| curve | shape |
|---|---|
| Base - Standard | 350 kW to 290 kph; -5 kW/kph to 100 kW at 340; -20 kW/kph to 0 at 345 |
| Base - Overtake | 350 kW to 337.5 kph; -20 kW/kph to 0 at 355 |
| Alt 1 | 250 kW to 310 kph; then as Base - Standard |

Overtake was already right to half a kph. **Standard was not**: it was modelled
as one straight line from 350 at 290 to nothing at 345, which allows 159 kW at
320 kph where the rules allow 200, and 32 at 340 where they allow 100. Alt 1 had
no numbers at all and fell back to Base.

The model now takes curves as points, and a `Powertrain` can carry an
alternative curve with the windows it applies in, so a race map at Baku is
capped at 250 kW through the three twisty sectors and runs the Base curve down
the straights. The app gives it to the race and recharge strategies and not to
qualifying.

### What it is worth, and what cannot be checked

A Baku race map planned on the old straight line, on the true curve, and on the
true curve with the sectors lands within **0.012 s**, scored under the rules as
they apply. A race lap is energy-limited at 8.5 MJ, so the curve mostly moves
where the fixed energy is spent rather than how much. Kept because a model that
does not know where power will be refused cannot place it.

None of this can be checked against the corpus. The car picks its curve by
*session*, not by strat -- practice and qualifying run Override whatever is
selected -- and every recorded lap is a practice lap. That is also why a race
strat was once measured holding 299 kW at 325 kph, far above Standard, and why
Standard was reverted for replay then: the laps were never on it.

Not modelled, recorded for completeness: Article C5.12.4, sectors where the power
reduction at the start of a Power Limited Pending period may exceed 150 kW
(T1-T2, T3-T12, T15-T16, and exit of T16 4050-5300 in Q, all 350 kW); and the
Overtake detection line at 4177 m (L20) and activation line at 4270 m (L21,
TBC) with a 1.0 s gap.

### Apply writes the whole Power & Energy Limits page

Apply already wrote the five recharge limits, the reduction rate and the four
curves when "write the allocation" was ticked. Two things kept the menu from
reading as the notice does:

* **Practice was overwritten** with the active strat's session figure, so a
  qualifying map tested in practice was held to qualifying's allowance. At Baku
  that is 8.5 MJ in a slot the notice sets at 9.0. The car has its own item
  for exactly this, Power and Energy Limits, and the driver uses it.
* **That item was reset** to Current Session on every apply. Baku.ini was on
  Qualify before the 12:15 apply, came back on Current Session, and was put
  back by hand at 12:31.

Now every figure goes in as published and the selector is left as the driver
set it. The saved panel lists what was written in the menu's own labels. The
check writes Baku's notice onto a copy set to Qualify and reads all eleven
items back.

The driver's copy of the exe is 1.1.0, whose table predates the notice, so
its apply still writes qualifying 9.0, race with Overtake 8.5 and 100 kW/s.
The menu in the screenshot showed the notice's figures because they had been
set by hand; the file on disk did not.

## Pooling, provenance and the corner ceiling

Baku's holdout replay came in at 3.75% mean and 9.15% worst. Three things were
behind it, and only the last was the model.

### Stopped laps pushed real laps out of the session

The pooling rule throws out a lap quicker than 70% of the session's median as a
fragment. Today's Baku session held a 10:02 and a 7:34 (the garage, a pause),
which put the median at 4:42, so every real lap from 1:46 to 1:50 fell under
the fragment line and lap05 was modelled on the 7:34 alone. The median is now
taken over the quicker half of the session. Stopped laps can't move it, and
a genuine fragment still sits far below it: the 35.525 s Albert Park file
among sixteen 1:21s still puts the line at 57 s. No other circuit moved. Baku:
**3.75% → 1.48%, worst 9.15% → 3.40%**.

### A setup file written after the lap is not a record of it

A lap that names its setup file was attributed to that file as it reads now,
if the map fitted. Three Baku laps of 21/09 named the server's shared setup,
which the server rewrote on 23/09, and the new contents passed at 0.35-0.37
against a floor of 0.35. A file written since the lap was saved now sends the
lap to the snapshots instead. 11 laps moved:

| | laps | fit before | fit after | map |
|---|---|---|---|---|
| Austin, named cota.ini | 6 | 0.36-0.44 | **0.54-0.61** | different: the 20/09 11:47 snapshot |
| Baku, named the shared setup | 3 | 0.35-0.37 | 0.37-0.39 | same map, found in Baku.ini's snapshot |
| Baku, named Baku.ini (saved again at 12:31) | 2 | 0.77-0.78 | 0.77-0.78 | same |

The six Austin laps now fit much better, so they had been replayed on a map
they never ran. Their lap-time error got *worse* (0.44% → 0.58%, all fast): the
wrong map had been hiding the model's fast bias. That settled the next
question.

### The corner ceiling takes the slot mean

The ceiling used the fastest sample in each 2.5 m slot, chosen when the model
was 1.22% slow. Most of that was power not delivered at corner exits, since
fixed, and on the corrected corpus the maximum left the model 0.64% fast. The
mean of the slot's samples, out of sample:

| circuit | laps | max: mean / bias | **mean: mean / bias** |
|---|---|---|---|
| Albert Park | 21 | 0.51 / -0.31 | **0.46 / -0.12** |
| Austin | 27 | 0.55 / -0.49 | **0.43 / -0.11** |
| Baku | 7 | 1.52 / -1.52 | **1.10 / -1.10** |
| Monaco | 3 | 0.87 / -0.87 | **0.21 / -0.21** |
| Monza | 6 | 0.81 / -0.20 | 0.87 / **-0.03** |
| Red Bull Ring | 3 | 1.41 / -0.92 | **1.34 / -0.68** |
| Sakhir | 8 | 1.42 / -1.42 | **1.08 / -1.08** |
| Suzuka | 2 | 0.59 / -0.34 | 0.59 / **-0.13** |
| **all** | **77** | **0.78 / -0.64** | **0.63 / -0.33** |

Every circuit's worst lap improves. The benchmark the search runs on, which
compares maps within a session, doesn't move: 0.214 s against 0.213, segments
0.082 against 0.081, direction 88% against 89%.

### A charged store is spent: the Baku A/B

The replay spent every kilojoule of a charged store (11.2-11.4 MJ) where two
laps on the server's shared setup had deployed 9.3-9.5 and finished with
1.4-1.9 MJ left. That was either the car holding charge back or those laps
being on a different map from the one they were matched to (fit 0.51).

An A/B settled it. `baku_v2.ini`: strat 1 the map the 1.1.0 app wrote at
12:15, strat 2 the one today's model writes from the two Baku.ini laps, strat
3 recharge, limits to the notice, Power and Energy Limits on Qualify. Driven
1 → 3 → 2 → 3 → 1, switching 130-190 m before the line (fuel ran short for
more).

Every push lap began at 89-91% and **ended at 1%**, deploying 11.3-11.7 MJ.
The car spends the store, as modelled. It was the shared-setup laps' map
attribution.

| | strat 1 (lap02, lap06) | strat 2 (lap04) |
|---|---|---|
| lap | 1:47.355, 1:46.879 | 1:46.950 |
| B - A, measured | | **-0.156 s** (against the pair's mean) |
| B - A, benchmark | | -0.113 s |

Two pieces are the driver, not the map:

* 2000-2500, the castle: lap02 is 0.29 s off lap06; lap04 matches lap06.
* **4000-4100, T16: lap04 apexed at 105 kph against 117 on both A laps** and
  lost 0.261 s there. The benchmark had B gaining 0.127 s over 4000-4500 and
  it lost 0.214.

After T16 the map did what it was built for. 250 kW against 200 overtook
both A laps by 4260 m and was 6 kph quicker at 4540: -0.054 s over
4260-4500 and -0.155 over 4500-5000, where the benchmark had -0.151. It ran
flat about 500 m from the line (+0.124 s in the last piece; modelled +0.271).
Against lap06, the cleaner A lap, B is 0.071 s behind with 0.278 s lost at
T16, so roughly **0.2 s quicker** with a normal T16. The model's
expectation from full was 0.233.

## Where the model stands

| | laps | mean | worst | bias |
|---|---|---|---|---|
| start of this work | 36 | 1.47% | 3.20% | +1.22% |
| Albert Park and Austin added | 56 | 0.82% | 2.52% | +0.18% |
| **now** | **77** | **0.63%** | **3.02%** | **-0.33%** |

**29 of the 77 carry the map the logger recorded, or a setup file unchanged
since the lap.** The rest are matched by behaviour against the snapshots.

## Sepang (R16, the Bahrain Grand Prix in Malaysia)

FIA Document 4, 1 October 2026. Added to the table as `sepang`:

| | notice |
|---|---|
| recharge, race / with Overtake | 8.5 / 9.0 MJ |
| recharge, qualifying / practice / out laps | 7.5 / 9.0 / 9.0 MJ |
| reduction rate, power-limited distance | 100 kW/s, 3365 m |
| curves | Base - Standard, Base - Overtake, Alt 1 (race, other parts of the lap) |
| Alt 1 sectors (race) | T4-T7 1550-2500, T9-T14 3100-4100 |
| reset zones | exit T5 1950-2100, exit T6 2150-2350, [exit T15 5200-5500] (Q) |
| higher speed threshold | T5-T6 1750-2200, T12-T13 3700-3900, both 270 km/h |
| centreline | 5543 m |

The curves are the same three as Baku's chart. Not modelled, recorded:
C5.12.4 sectors (T1-T2 600-750, T5-T7 1900-2500, T9-T11 3100-3400, T12-T13
3750-4000, [exit T15 5050-5300], all 350 kW); Overtake detection 5110 m (L18),
activation 5160 m (L19), gap 1.0 s.

The event's name points at the wrong circuit: "bahrain" maps to Sakhir. "Malaysia"
and the full event name map to Sepang; "bahrain" alone stays Sakhir's. Folder
names `chq_sepang`, `rt_sepang_2026` and `sepang_international_circuit` all
resolve by the contained-name rule.

The first Sepang laps also broke a check that had nothing to do with them. The
line-rule check took the first lap alphabetically with a run to the first
corner, and `chq_sepang` now sorts before Austin. Its lap01 is a recharge lap
that falls from 286 to 252 kph at full throttle. The check now wants a car that
gains speed through the opening, and finds Austin again.
