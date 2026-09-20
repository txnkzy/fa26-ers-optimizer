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
