# FA26 ERS Deployment Optimizer — v1.1.0

The short version: the maps are faster, and three of the reasons why were
measured on track rather than argued about in a spreadsheet.

## Faster maps

**Deployment through fast corners.** The optimizer used to think spending
battery through a corner you take flat out was worth nothing, so it never did
it. On track it is worth **0.46 s and 16 kph** of exit speed through Austin's
long final-sector right. Fixed, and confirmed on three separate pairs of laps.

**The whole battery, not three quarters of it.** Maps used to hold a quarter of
the battery back on every lap. That costs **0.99 s** across seven circuits, so
it is gone. Qualifying maps now arrive at the line empty, which is what a
qualifying lap is for.

**The run to the first corner.** No map this tool has ever written deployed
anything between the start/finish line and turn one — on any circuit. The car
crosses the line already at full throttle and cannot be asked for more power
until you lift, so the opening was always refused. Every map is now built so it
can fire: around **seven seconds of full throttle** that used to be wasted.

**Super-clipping.** Now used where it pays, worth up to **1.08 s** — and
measured at **+0.936 s** at Albert Park over a back-to-back run.

## A model you can trust further

Prediction error against real laps is down from **1.47% to 0.75%**, measured
over 70 laps. Half of those laps now carry a record of the exact map they were
driven on, written by the logger at the time, instead of being matched to a
guess afterwards.

## Telemetry logger

Updated, and safer. It records which strategy each lap ran and which setup file
was loaded, so nothing has to be reconstructed later. It no longer throws away a
good lap because the one before it was invalid, cannot be replaced while the
game is running, and cannot lose a lap by failing to write its notes.

## The app itself

* The switch that keeps searching between laps is now on the main page from the
  moment the window opens, instead of appearing only after your first lap.
* The activity log is gone. In its place, the one thing you actually have to do
  in the car: when to put STRAT 1 on, given as a landmark you can use at speed —
  crossing the line, or before the final corner.
* Maps that ask for power where the car will refuse it are flagged, rather than
  quietly handed over.
* Past runs are kept so two can be compared, and a qualifying estimate appears
  while you are still driving.

## Known limits

The model still undervalues deployment out of slow corners, and the best balance
of energy across a whole lap is an open question rather than a solved one. Both
are being worked on with measurements from the car.
