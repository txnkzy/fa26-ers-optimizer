# FA26 ERS Deployment Optimizer — v1.2.0

**About twenty times faster, and a new look.** 1.1.4 was meant to be the
last feature release. Then the search got fast enough to be worth making the
rest of the app as quick to use.

## What's in this release

**The map is drawn on the track.** The results screen now shows the circuit
itself, with deployment in amber and red and harvesting in teal, and a car
going round at the speeds the plan predicts, saying what the map is asking
for where it is. Hover a zone for its power and distances. Pick any of the
seven maps on the right and the track redraws for it.

**Four steps instead of one long page.** Session, Solve, Maps, Save, across
the top in the order you use them, with folders, the logger, past runs and
help on the right. Everything the old layout did is still there; it has just
stopped scrolling.

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

## Two fixes from your reports

**"Power changes the car will refuse."** Where a long straight had been cut
into several zones, the app raised the power of the last one before the line
on its own, which is an increase under throttle: the car refused it, and
refused the opening zone of the next lap with it. Laps driven by the AI hit
this on every map. The raise is now carried back along the straight to where
the throttle was picked up, or not made at all.

**Harvesting all the way to Turn 1.** Once the car is harvesting at full
throttle it carries on until you lift, even past the end of the zone that
asked for it and across the start/finish line. The app did not know that, so
a race map could harvest into the line and leave the car slowing down the
whole pit straight. It now plans with the harvest lasting until the lift.

Thank you to PortalFawn for both.

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
