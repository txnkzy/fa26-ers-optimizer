# FA26 ERS Deployment Optimizer — v1.1.4

**The FA26 Optimizer is finished.** This is the last feature release for the
2026 car. From here it's in maintenance: it keeps working, it keeps itself up
to date through the rest of the season, and if a car update or anything else
breaks it, it gets fixed.

## What's in this release

**It tells you when there's a newer version.** A banner at the top of the app
says when a newer version is out and where to get it, so a fix always reaches
you.

**It tells you when the car has changed.** If a VRC update to the Formula
Alpha 2026 changes the setup items the optimizer relies on, the app now says
so plainly — and points you to the Discord for an updated version — instead
of failing part way through a save.

**Race-weekend data stays automatic to the end of the season.** Each Grand
Prix's FIA Power Unit Information notice keeps arriving on its own, as it did
for Austin. The watch that fetches them runs until 25 December 2026 and then
switches itself off.

## The FA26 Optimizer in one page

Over its life the optimizer went from a single deployment map to a full
strategy for every PU mode on the wheel:

* **Qualifying, race and recharge maps** built from your own laps, scored the
  way you'll really drive them — the charge you'll start with, the power rules
  at the line, the car's own power curves.
* **Attack, defend, push and save modes** (AD1, AD2, K2, ES) with their own
  maps and SoC targets, using the car's SoC Adaptation.
* **A race map that keeps power to the line**, and maps that keep you flat
  through fast corners instead of forcing big lifts.
* **Every FIA power unit notice** for 2026, read straight from the FIA and
  checked figure by figure.
* **A search that uses every core** of your CPU and double-checks its answers.
* **A model as accurate as our laps can measure**: map against map, within
  0.14 s per 500 m — the same as the difference between two of your own laps
  on the same map.

## Thank you

To everyone who drove A/B laps, sent diagnostics, reported bugs and asked the
awkward questions: this was built on your laps. See you for the FA27.

## Requests

No new features are planned, but requests are welcome — post them in the
Discord and they'll be considered.
