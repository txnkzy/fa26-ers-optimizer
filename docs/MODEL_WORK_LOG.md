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
