# FA26 ERS Deployment Optimizer — v1.1.3

**Circuit data now updates itself.** Every Grand Prix weekend the FIA
publishes a Power Unit Information notice with the event's recharge limits,
power curves and sectors. The optimizer now picks each one up automatically,
within hours of it being published — no new version to download. Just open
the app as usual.

**Every 2026 notice checked.** All seventeen notices so far, Australia to
Singapore, have been read and checked against the app's figures, filling in
sectors that were missing at nine circuits and adding the new power curves
used at Monaco and Singapore.

**Clearer advice on short tracks and odd sessions.** The notes under each map
now say plainly when the car simply can't spend everything it recovers, warn
when a plan relies on more recovery than your laps showed, and no longer
describe a map that holds its charge as "banking 0.05 MJ a lap".

---

## Circuit data from the FIA

* When the optimizer starts, it downloads the latest circuit table from this
  project. It only uses it if it is at least as new as the one built into
  the program, and ignores anything that fails its checks. Offline, it uses
  the built-in table, as before. Nothing about you or your laps is sent.
* The table is kept up to date automatically: every three hours a job checks
  the FIA's documents page, reads any new Power Unit Information notice —
  limits, reduction rate, power curves straight off the notice's chart, and
  every sector table — checks every figure against what the regulations
  allow, and publishes it. A notice it cannot read with confidence is not
  published.
* New power curves travel with the data. Singapore's notice brought in
  Rev 1 - Standard, Rev 1 - Overtake and Alt 2, which cut deployment hard
  above 200 kph; Monaco uses Rev 1 too.
* The diagnostics show which circuit data the app is using.

## What the check of every notice changed

* **Alternative power curve sectors** (race only) added at Miami, Canada,
  Barcelona, Austria, Silverstone, Spa, Hungary and Zandvoort.
* **Reset sectors** added at Australia, Monaco, Barcelona, Silverstone and Spa.
* **Monaco** now uses the real shape of its Rev 1 curves instead of the
  standard curve; the model's lap error there fell from 0.21% to 0.16%.
* **Singapore** uses its published limits — race 8.5 MJ, 9.0 with Overtake —
  and its new curves.
* **Canada** is found from "Montréal" as well as "Montreal".

## Notes under each map

* When a map already asks for full power everywhere you're on the throttle and
  still finishes with charge, it now says the car can't spend everything it
  recovers on this circuit — not a fault, and re-running won't change it —
  instead of warning about lost recovery and asking for a re-run.
* A new warning when a plan relies on recovering far more energy than your
  laps actually recovered, with what to check.
* A mode map that neither spends nor banks says so, instead of "banks 0.05 MJ
  a lap, empty to full in about 80 laps".
* After saving, the app explains which PU mode selects which map.
