# UI redesign for 1.2.0

Approved from clickable mockups on 16 November 2026, not from a written spec.
This note records what was approved and the calls made while building it.

## Why

The old page was one long screen with a rail of settings down the left: it
read as cluttered, and what a map does could only be seen as bars over a
distance axis. The ask was a much simpler app that leans on tabs instead of
scrolling and shows things graphically, starting with the track map and
where the car deploys on it.

## What was approved

**Four steps across the top, in the order they are used.**

1. **Session** asks one question: "Are you driving *circuit*?" with a small
   outline of the track, the lap count and the best lap. Yes goes on; No
   waits for other laps.
2. **Solve** shows the two recharge limits (official figures locked until
   "Change"), anything worth knowing about the laps, and one Solve button.
   Solving shows a progress ring and then moves on by itself.
3. **Maps** is the centrepiece: the circuit, drawn from the game's own
   racing line, with deployment in amber-to-red by power and harvesting in
   teal. A car goes round at the simulated speeds with a live readout of
   speed and what the map is asking for. Zones are crisp lines, not glows
   (the glow was tried and rejected). The seven maps are listed on the
   right, each in its own colour; picking one redraws the track. Lap time,
   the gap to the driver's best lap, battery start to end, and energy
   deployed and harvested sit under the list. A speed trace with the zones
   as bands sits under the track, with a playhead that follows the car.
4. **Save** lists the setups for the circuit, saves into the one picked,
   and then says "Restart the session before driving" loudly.

**Everything else is on the right:** History (past runs and comparing two),
Help, and Settings with its own side tabs (Folders, Logger, Preferences,
About).

Look: dark, the app's own fonts (Barlow Condensed for headings, JetBrains
Mono for figures), one amber accent for actions, a subtle grid behind the
track.

## Calls made while building (not explicitly approved)

* **Nothing the old app did was dropped.** Every control moved to a screen;
  the script keeps its element ids, so the logic underneath is the same.
* **Notes and the split table are tabs on the Maps stage** (Track / Notes /
  Numbers), to keep the screen from scrolling. The in-car switch advice is
  at the top of Notes.
* **The between-laps switch lives in Settings, under Preferences.** It was
  on the rail so it could be set before the first lap; Settings is reachable
  from the moment the window opens, so that still holds.
* **A track whose racing line cannot be read is drawn as a plain loop**, with
  a line saying so. Zones still sit in the right order and proportion.
* **The speed readout sits bottom right of the map**, not top right as in
  the mockup, where it covered part of the Austin back straight.
* **Screens can be opened by address** (`#settings`), which the app never
  uses; it is how each screen is checked.

## New pieces

* `GET /api/track?key=` in `fa26_app.py`: the outline, from
  `content/tracks/<track>[/<layout>]/ai/fast_lane.ai`.
* `ui/track.js`: the Maps screen.
* `/api/poll` carries the session `key`, and `/api/setups` accepts it, so
  maps kept from an earlier session still draw on the right circuit and can
  still be saved.
