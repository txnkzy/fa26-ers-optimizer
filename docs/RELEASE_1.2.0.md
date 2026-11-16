# FA26 ERS Deployment Optimizer — v1.2.0

**About twenty times faster, and the same answers.** 1.1.4 was meant to be
the last feature release, and this adds no features: nothing about how you
use the app has changed. It just stops making you wait.

## What's in this release

**A solve takes seconds, not minutes.** The lap simulation at the heart of
the search — the part that prices every map the optimizer tries — has been
rewritten as compiled code. On the machine it was measured on, a full solve
at Austin went from 205 seconds to 11, and Las Vegas from 170 to 9. Yours
will differ with your processor, but the wait that used to be "go back to the
game for a few minutes" is now usually well under a minute.

**The maps are the same maps.** The compiled simulation was held to the
original's numbers exactly, not approximately: 14,880 simulated laps across
24 circuits came back identical to the last digit, a whole solve at Austin
produced the same seven maps, and race searches at Barcelona and Spa chose
the same map in the same number of steps. If the
compiled part cannot load on your machine for any reason, the app quietly
uses the original and gives the same result, only slower.

**The recharge map fills the battery.** STRAT 3 had been arriving at the line
about a tenth short of full on most circuits, because the car deploys on its
own out of the last corner and the map did nothing to cancel it. It now adds
one short harvest zone at the end of the lap, which in simulation arrives
full on all 24 circuits tested. If you switch to your qualifying map before
the final corner, as the app suggests, you will not notice a difference: the
battery was already full there.

**Diagnostics say which simulation is running.** "Copy diagnostics" now
includes a "lap integration" line — `compiled`, or `python` with the reason.
If a solve ever seems slow again, that line says why.

## Known rough edges

* The new recharge zone is a result from the model. It has not yet been
  confirmed lap for lap in the car.
* On circuits whose timing line sits well after the track's own zero point —
  the `fn_cota` Austin is 294 m — the qualifying map can run out of charge
  just before the line.
* The Attack and Defend maps sometimes come out energy-neutral rather than
  spending their 10%.

None of these is new in 1.2.0; the last two were found while testing it.

## Requests

Still welcome in the Discord.
