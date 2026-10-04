# FA26 ERS Deployment Optimizer — v1.1.1

Mostly fixes for things players ran into after 1.1.0, plus Sepang for this
weekend.

**Update the logger after installing:** with Assetto Corsa closed, open the
**Folders** page and press **Update the logger**. Several fixes below are in the
logger itself.

## Its own window

The optimizer now opens as a proper window of its own, like any other app,
instead of borrowing a Microsoft Edge window. That fixes the "127.0.0.1 refused
to connect" page and the app saying **"Cannot reach the optimizer"**. Both came
from Edge passing the window between its own processes, and you no longer need
to update or reconfigure Edge. Closing the window closes the program.

## FIA Power Unit Information

* **Sepang** (R16, the Bahrain Grand Prix in Malaysia) and **Baku** are in, with
  their recharge limits, power reduction rate, power curves and sectors.
* **Saving a map now sets the whole Power & Energy Limits page** to the FIA
  notice: every recharge limit, the power reduction rate and all four power
  curves, so the menu matches the document. Your own **Power and Energy
  Limits** choice is left alone. Set it to Qualify to make a practice session
  run to qualifying's limits.
* After saving, the app lists exactly what it wrote, worded as the menu shows
  it.

## Laps you can trust

* **Laps are cut at the game's own start/finish line**, and each file is named
  with the game's lap time, so the times match the timing screen exactly.
* **Restarting or resetting to the pits mid-lap no longer leaves a fake lap.**
  It used to join two pieces of different laps together and could hand the
  optimizer a "best lap" you never drove. Those laps are now dropped, and the
  logger window says so.
* **If Windows blocks the logger from saving**, usually because Windows
  Security's ransomware protection is on, the logger window now says **LAPS ARE
  NOT BEING SAVED** and explains why. It used to report laps as saved when they
  weren't. The app also warns if it can't save into your setups.

## A more accurate model

Prediction error against real laps is down to **0.62%**, from 0.75% in 1.1.0,
measured over 85 laps on eight circuits. The model's habit of predicting laps
too fast is halved. Garage and pit laps no longer push your real laps out of
the calculation, and a lap is no longer matched to a setup file you saved
after driving it.

On track at Baku, a map from the new model was **0.16 s quicker** than the one
1.1.0 wrote, driven back to back on the same setup.

## Smaller fixes

* **Copy diagnostics** now reports Windows' ransomware protection and whether the
  app can write to your setups. It says "unknown" instead of "no" when it
  couldn't tell whether the game was running.

## Worth knowing

The new lap cutting has been checked on track, but not yet on every Custom
Shaders Patch version or on tracks whose timing line sits away from the start of
the track. If lap times in the optimizer don't match the game's, send **Copy
diagnostics** and a zip of those laps.
