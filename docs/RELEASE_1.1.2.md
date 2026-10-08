# FA26 ERS Deployment Optimizer — v1.1.2

**Compatible with the VRC Formula Alpha 2026 v1.1 update.**

**New and improved modelling algorithm.** The optimizer now searches on every
core of your CPU and double-checks its answers, finding faster maps in about
the same time.

**Full PU mode strategies.** On top of qualifying, race and recharge, you now
get dedicated **Attack (AD1)**, **Defend (AD2)**, **Push (K2)** and **Save
(ES)** maps. Each is built from your own driving and wired straight to your
wheel's PU modes, so one switch changes your whole strategy.

**Uses the car's built-in SoC Adaptation.** The Attack, Defend, Push and Save
modes each get their own state-of-charge target and use VRC's SoC Adaptation,
so the car fine-tunes its deployment to your driving lap by lap.

**Smarter race map.** You now reach the final corner with charge in hand and
keep your power across the line, instead of running dry before the main
straight.

**Built for driveability.** Maps avoid sending you into fast sweepers faster
than you can take them flat, so there are fewer big lifts and you're more
consistent lap to lap.

**Plus:** more accurate qualifying maps, laps logged exactly where the game
times them, and a smoother app that closes cleanly.

*Tip: set Power and Energy Limits to Race in your setup when racing.*

---

## The PU modes

Saving a map now sets up your wheel's PU modes as well:

| Mode | Map | What it does |
|---|---|---|
| **Q** | STRAT 1, Qualifying | Your fastest lap from a full battery |
| **RACE** | STRAT 2, Race | Holds its charge level lap after lap |
| **SLO**, **SC** | STRAT 3, Recharge | Refills the battery; also for safety cars |
| **AD1** | STRAT 4, Attack | Spends about 10% of the battery a lap, planned for Overtake: power held to higher speeds, plus the extra 0.5 MJ you get in a race |
| **AD2** | STRAT 5, Defend | Spends about 10% a lap, without Overtake |
| **K2** | STRAT 6, Push | Spends about 20% a lap |
| **ES** | STRAT 7, Save | Banks about 10% a lap, to build charge for an attack |

* In those modes the mode chooses the map, not the STRAT rotary. **FS, FW, IN,
  K2+, T4 and RS** are left free, so the rotary still works there — use one of
  them to compare maps of your own.
* AD1, AD2, K2 and ES run with SoC Adaptation on and an SoC Target to match.
  RACE runs with it off: the race map holds its own level, and on track
  adaptation fought it and cost lap time.
* Each map's card says what it costs or banks a lap and what it buys, for
  example "K2: spends 0.75 MJ of the battery a lap, so about 5 laps from full".
* Overtake's extra 0.5 MJ only exists in a race, when you cross the line with
  it active. In practice AD1 gets the later power cut-off but not the energy.
* Solving takes a little longer, about two to two and a half minutes, because
  there are four more maps to work out.

## The race map

The race map used to run the battery to empty every lap. At Sepang that meant
arriving at the final hairpin with nothing, recovering nothing under braking
(the lap's recovery limit was already used), and crossing the line at 241 kph
instead of 291. It now carries a little charge — about 10% — from lap to lap
where that is quicker, and still uses the whole battery within the lap. It is
planned both ways and the quicker is kept, so it is never slower than before.
On track at Sepang it reached the last sector with 34–51% charge and the main
straight about 46 kph faster.

## Driveability

A map that brings you into a fast corner quicker than you can take it flat
forces a lift in the middle of it, and that lift has to be judged afresh every
lap. The optimizer now counts those lifts as a cost and moves the energy where
you can stay flat. At Sepang T5 the laps taken flat on the limit were the most
consistent and, on average, the quickest.

## Search

* Qualifying and race are searched side by side on separate cores.
* Every finished map is shaken and climbed again on spare cores, keeping the
  result only if it is quicker. Across sixteen circuits this found up to a
  tenth more on several race maps and never made a map worse.
* Qualifying maps are planned for the charge you will really start with —
  the recharge lap before them — and switching strats before the final corner
  is advised only when it pays.
* Speed sectors now ramp power down rather than cutting it, and reset zones no
  longer grant extra power, matching what the car does on track.

## Fixes

* Works with the car's v1.1 update, which reordered the setup file and stopped
  1.1.1 reading setups.
* The setup list refreshes while it is open and includes setups saved for all
  tracks.
* The program closes when its window is closed, even if something holds it up.
* A lap with a bad reading in it no longer blanks the page.
* Laps driven with SoC Adaptation on are left out of the model's accuracy
  checks, since the car was changing the map as it drove.
