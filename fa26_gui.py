"""FA26 ERS deployment optimiser -- Tk front end (superseded).

`Run FA26 Optimizer.bat` now launches `fa26_app.py`, which serves the same
solver behind a browser page. This file still works and is kept as a fallback
for a machine with no usable browser; it does not carry the newer workflow --
no session pooling in the picker, no recharge sliders, and it still asks for a
fuel load and an active strategy.


Produces three strategies from one recorded lap and writes them into a setup:

    STRAT 1  Qualifying  fastest lap, spends the store
    STRAT 2  Race        fastest lap that ends with the energy it started
    STRAT 3  Recharge    banks energy, accepting some lap time

Each goes into its own deployment map. The map in use is chosen by the "STRAT
Map" setup item, not by the wheel's PU mode switch -- the per-mode overrides do
not fire, so this tool writes them off and drives the base item instead.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
for sub in ("core", "cars"):
    sys.path.insert(0, str(ROOT / sub))

import ailine
import calibrate
import carphysics
import fa26
import install
import lap as laplib
import optimise26
import profile as prof
import recharge
# Aliased: `_solve_worker` already binds a loop variable called `session`
# (which strategy's allocation to use), and a bare `import session` makes the
# name local to that whole function -- so the module lookup earlier in it fails
# with UnboundLocalError before the loop is ever reached.
import session as lapsession
import setupfile
import sim26

CAR_ID = fa26.CAR_ID


def ac_root():
    """The user's Assetto Corsa folder, found rather than hardcoded."""
    return install.find_root() or Path.home()


def unpacked():
    """The car's data, unpacked from the user's own install on first use."""
    return install.ensure_unpacked(CAR_ID)

STRATEGIES = [
    # label, strat number, session allocation, energy-neutral, one-shot
    ("Qualifying", 1, "quali", False, True),
    ("Race", 2, "race", True, False),
    ("Recharge", 3, "race", False, False),
]


def docs_dir() -> Path:
    return Path(os.path.expanduser("~")) / "Documents" / "Assetto Corsa"


def mean_fuel(path) -> float | None:
    """Mean fuel load over a recorded lap, in litres, or None if absent."""
    import csv
    try:
        with open(path, encoding="utf-8") as handle:
            values = [float(row["fuel"]) for row in csv.DictReader(handle)
                      if row.get("fuel")]
    except (OSError, KeyError, ValueError):
        return None
    return sum(values) / len(values) if values else None


def baseline_dir() -> Path:
    return docs_dir() / "fa26_baseline"


def list_laps() -> list[Path]:
    folder = baseline_dir()
    if not folder.exists():
        return []
    return sorted(folder.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=10)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        self.messages: queue.Queue = queue.Queue()
        self.lap_paths: dict[str, str] = {}
        self.results: list[tuple[str, int, list, object]] = []
        self.lap = None
        self.car_setup_ini = ""

        self._build_inputs()
        self._build_actions()
        self._build_output()
        self.after(100, self._drain)

    # --------------------------------------------------------------- widgets
    def _build_inputs(self) -> None:
        box = ttk.LabelFrame(self, text="Recorded lap", padding=8)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        self.lap_var = tk.StringVar()
        ttk.Label(box, text="Lap").grid(row=0, column=0, sticky="w")
        self.lap_combo = ttk.Combobox(box, state="readonly")
        self.lap_combo.grid(row=0, column=1, sticky="ew", padx=6)
        self.lap_combo.bind("<<ComboboxSelected>>", self._on_lap_selected)
        ttk.Button(box, text="Refresh", command=self.refresh_laps).grid(row=0, column=2)
        ttk.Button(box, text="Browse", command=self._pick_lap).grid(row=0, column=3)

        self.track_label = ttk.Label(box, text="", foreground="#555")
        self.track_label.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.refresh_laps()

        opts = ttk.LabelFrame(self, text="Parameters", padding=8)
        opts.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.fuel_var = tk.DoubleVar(value=45.0)
        self.write_alloc_var = tk.BooleanVar(value=True)
        self.active_var = tk.StringVar(value="Race")

        ttk.Label(opts, text="Fuel (L)").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(opts, from_=0, to=150, increment=5, width=8,
                    textvariable=self.fuel_var).grid(row=0, column=1, padx=(4, 16))
        ttk.Label(opts, text="Starting energy is derived per strategy, not entered",
                  foreground="#555").grid(row=0, column=2, columnspan=2, sticky="w")
        ttk.Checkbutton(opts, text="Also write the published recharge allocation",
                        variable=self.write_alloc_var).grid(row=1, column=0, columnspan=4,
                                                            sticky="w", pady=(6, 0))
        ttk.Label(opts, text="Active on track").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(opts, state="readonly", width=12, textvariable=self.active_var,
                     values=["Qualifying", "Race", "Recharge"]).grid(
                         row=2, column=1, sticky="w", padx=(4, 16), pady=(6, 0))
        ttk.Label(opts, text='sets "STRAT Map" in the setup -- the wheel switch does not select',
                  foreground="#555").grid(row=2, column=2, columnspan=2, sticky="w", pady=(6, 0))

    def _build_actions(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=2, column=0, sticky="ew", pady=8)
        self.solve_btn = ttk.Button(bar, text="Build 3 strategies", command=self._solve)
        self.solve_btn.pack(side="left")
        ttk.Button(bar, text="Cold start from track...",
                   command=self._cold_start).pack(side="left", padx=6)
        self.apply_btn = ttk.Button(bar, text="Apply to setup...", command=self._apply,
                                    state="disabled")
        self.apply_btn.pack(side="left", padx=6)
        self.status = ttk.Label(bar, text="Ready")
        self.status.pack(side="left", padx=12)

    def _build_output(self) -> None:
        box = ttk.Frame(self)
        box.grid(row=3, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.text = tk.Text(box, wrap="none", height=30, font=("Consolas", 9))
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)

    # --------------------------------------------------------------- helpers
    def refresh_laps(self) -> None:
        files = list_laps()
        self.lap_paths = {p.name: str(p) for p in files}
        self.lap_combo["values"] = list(self.lap_paths)
        if files:
            self.lap_combo.set(files[0].name)
            self.lap_var.set(str(files[0]))
            self._show_track()

    def _on_lap_selected(self, _event=None) -> None:
        name = self.lap_combo.get()
        if name in self.lap_paths:
            self.lap_var.set(self.lap_paths[name])
            self._show_track()

    def _show_track(self) -> None:
        path = self.lap_var.get()
        if not path:
            return
        alloc = recharge.lookup(path)
        key = recharge.normalise(path)
        note = "assumed -- circuit not on the published list" if alloc.is_baseline \
            else alloc.source
        self.track_label.configure(
            text=f"circuit: {key}   qualifying {alloc.mj('qualify'):.1f} MJ, "
                 f"race {alloc.mj('race'):.1f} MJ   [{note}]")

    def _pick_lap(self) -> None:
        start = baseline_dir()
        path = filedialog.askopenfilename(
            title="Recorded lap", initialdir=str(start if start.exists() else Path.home()),
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if path:
            self.lap_var.set(path)
            self._show_track()

    def log(self, line: str = "") -> None:
        self.text.insert("end", line + "\n")
        self.text.see("end")

    def _drain(self) -> None:
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log(payload)
            elif kind == "status":
                self.status.configure(text=payload)
            elif kind == "done":
                self.solve_btn.configure(state="normal")
                self.apply_btn.configure(state="normal" if self.results else "disabled")
            elif kind == "error":
                self.solve_btn.configure(state="normal")
                messagebox.showerror("Failed", payload)
        self.after(100, self._drain)

    # ----------------------------------------------------------------- solve
    def _solve(self) -> None:
        path = self.lap_var.get().strip()
        if not path or not Path(path).exists():
            messagebox.showerror("No lap", "Record a lap with the in-game logger first.")
            return
        self.text.delete("1.0", "end")
        self.solve_btn.configure(state="disabled")
        self.apply_btn.configure(state="disabled")
        self.results = []
        threading.Thread(target=self._solve_worker, args=(path,), daemon=True).start()

    def _cold_start(self) -> None:
        """Author a first map for a track with no laps recorded on it.

        The car ships with an empty deployment map, so a first outing is driven
        on the automatic fallback alone -- worth 3.1 s a lap at Madrid against
        a map. Where the track's AI line was authored for a car like this one
        it carries enough of a racing line to solve against: the resulting map
        got within 0.07 s of one built from seven real laps. Where it was not,
        this refuses rather than inventing a lap.
        """
        start = ac_root() / "content" / "tracks"
        folder = filedialog.askdirectory(
            title="Track folder (content/tracks/<track>)",
            initialdir=str(start if start.exists() else Path.home()))
        if not folder:
            return
        self.text.delete("1.0", "end")
        self.solve_btn.configure(state="disabled")
        self.apply_btn.configure(state="disabled")
        self.results = []
        threading.Thread(target=self._solve_worker, args=(folder,),
                         kwargs={"cold": True}, daemon=True).start()

    def _solve_worker(self, path: str, cold: bool = False) -> None:
        emit = lambda line="": self.messages.put(("log", line))
        try:
            self.messages.put(("status", "Loading..."))
            data = unpacked()
            car = carphysics.load(data)
            car.fuel_liters = self.fuel_var.get()
            self.car_setup_ini = (data / "setup.ini").read_text(
                encoding="utf-8", errors="replace")

            # Model the whole session, not the one lap that happens to be
            # selected. Fitting a single lap overfits it: the map comes out
            # perfect on that lap and has three to five power steps refused on
            # every other lap of the same session, and the lap time it claims
            # is up to 1.9 s better than what it then delivers. Pooling halves
            # that gap and improves out-of-sample accuracy from 0.57% to 0.44%.
            if cold:
                found = ailine.find(path)
                if found is None:
                    raise RuntimeError(
                        "No ai/fast_lane.ai in %s, so there is no racing line "
                        "to work from. Drive a few laps instead." % path)
                line = ailine.load(found)
                if not line.usable:
                    raise RuntimeError(
                        "This track's AI line is not usable as a cold start: %s. "
                        "Drive a few laps instead." % line.rejection())
                group = []
                laps = [ailine.as_lap(line, car)]
                emit("Cold start  %s -- %.0f m racing line, tops out at %.0f kph"
                     % (Path(path).name, line.length_m, line.top_speed_kph))
                emit("            No laps are involved, so the lap times below "
                     "are optimistic by a few seconds:")
                emit("            the drag fit has no ERS trace to account for. "
                     "Drive this map and solve again.")
            else:
                group = lapsession.for_lap(path, baseline_dir())
                laps = [laplib.load(p) for p in group]
            lap = laps[0] if len(laps) == 1 else next(
                (l for l, p in zip(laps, group) if str(p) == str(path)), laps[0])
            if cold:
                pass
            elif len(laps) > 1:
                emit("Modelling  %d laps from this session (%s), not just the one"
                     % (len(laps), ", ".join(
                         "%.3f" % lapsession.parse(p)[2] for p in group)))
            elif not cold:
                used = Path(group[0]).name if group else ""
                if used and str(group[0]) != str(path):
                    emit("Modelling  %s alone -- the lap you picked is outside "
                         "3%% of the session best," % used.split("__")[-1])
                    emit("           so it would drag the model toward a lap "
                         "you were not really trying on.")
                else:
                    emit("Modelling  this lap alone -- the session has no other "
                         "comparable laps")
            # Fuel is in the telemetry, so it is not something to type in.
            # The box defaulted to 45 L while a Monza push lap ran at 6.9 --
            # 28 kg out. It changes the replay very little, because the drag
            # fit absorbs the mass it was fitted with, but it is the right
            # number and one less thing to get wrong.
            measured = [f for f in (mean_fuel(p) for p in group) if f] if group else []
            if measured:
                car.fuel_liters = sum(measured) / len(measured)
                self.fuel_var.set(round(car.fuel_liters, 1))
                emit("Fuel       %.1f L, read from the telemetry" % car.fuel_liters)

            # Was this map written after these laps were driven? Three
            # circuits in a row were applied in the wrong order, which makes
            # the telemetry unattributable and every lap-time comparison
            # against it meaningless.
            if group:
                order = lapsession.map_order_warning(group, baseline_dir())
                if order:
                    emit("")
                    emit("!! %s" % order)
                    emit("   Drive after applying, not before: apply, restart "
                         "the session, then drive.")
                    emit("")

            # A qualifying map is planned for a full store because it is meant
            # to follow a recharge lap. Arrive short and it is the wrong map --
            # 0.48 s wrong at Spa, where the lap began at 78%.
            charge = max(l.store_kj[0] for l in laps)
            if charge < laplib.STORE_CAPACITY_KJ * 0.90:
                emit("Battery    these laps began at %.0f%% charge. The "
                     "qualifying map assumes a recharge"
                     % (100 * charge / laplib.STORE_CAPACITY_KJ))
                emit("           lap left it full -- run STRAT 3 first, or it "
                     "plans for energy you will not have.")

            cal = calibrate.fit_longitudinal(laps, car)
            profile = prof.Profile(laps)
            alloc = recharge.lookup(path)
            # Article C5.12.8 is set per circuit at 50 or 100 kW/s, and it caps
            # how fast deployment may fall, so it changes what a map can do.
            powertrain = fa26.Powertrain()
            # Article C5.12.5: sectors where a reset of MGU-K power reduction
            # is permitted, so a map may raise deployment under full throttle
            # there. Empty for a circuit whose notice has not been transcribed,
            # which is the conservative reading.
            # Every C5.12.5 reset window published so far is bracketed as SQ
            # and Q only, so the race and recharge strategies get none of them.
            speed_zones = alloc.speed_zones_for(lap.length_m)
            zones_for = lambda strat: alloc.reset_zones_for(
                "qualify" if strat == 1 else "race", lap.length_m)
            # Article C5.2.8 names a curve for normal running and one for
            # Override. Qualifying runs with Override, the race does not, and
            # the Standard curve starts tapering at 290 kph against the
            # Overtake curve's 337 -- so applying one to both, as this did,
            # overstated race deployment everywhere above 290.
            taper_for = lambda strat: fa26.taper_for(
                alloc.curve_override if strat == 1 else alloc.curve_standard)

            deployed, harvested = lap.totals()
            emit("Lap            %.3f s over %.0f m" % (lap.lap_time_s, lap.length_m))
            seen = {}
            for value in lap.strat:
                seen[value] = seen.get(value, 0) + 1
            emit("Driven on      %s" % ", ".join(
                "STRAT %d (%.0f%%)" % (k, 100 * c / len(lap.strat))
                for k, c in sorted(seen.items(), key=lambda kv: -kv[1])))
            emit("As driven      deploy %.2f MJ, harvest %.2f MJ" % (deployed, harvested))
            emit("Allocation     qualifying %.1f MJ, race %.1f MJ (%.1f with "
                 "Overtake)  [%s]"
                 % (alloc.mj("qualify"), alloc.mj("race"),
                    alloc.mj("race_overtake"), alloc.source))
            emit("Power curve    %s (race), %s (qualifying)"
                 % (alloc.curve_standard or "unknown",
                    alloc.curve_override or "unknown"))
            emit("Power cut rate %d kW/s written to the car (the model uses the "
                 "measured %g kW/s -- see Powertrain.ramp_down_kw_s)"
                 % (alloc.rate_kw_s, fa26.Powertrain().ramp_down_kw_s))
            if alloc.curve_standard and not fa26.taper_is_known(alloc.curve_standard):
                emit("NOTE: %s has no published numbers, so the Base curve is "
                     "used in its place." % alloc.curve_standard)
            emit("Drag profile   %d bins, median k %.3f" % (len(cal.drag.k), cal.drag.k_default))
            emit("Traction       %.2f g     braking %.2f + %.5f v^2"
                 % (cal.traction_ms2 / 9.81, cal.brake_c0, cal.brake_c1))
            if alloc.is_baseline:
                emit()
                emit("NOTE: this circuit is not on the published allocation list, so the")
                emit("      qualifying figure is an assumption. Everything below depends")
                emit("      on it, because harvest governs how much can be deployed.")
            emit()

            seed = optimise26.seed_splits(lap, profile)
            emit("Seeded %d splits from the throttle trace." % len(seed))

            # Any setup already sitting in this track's folder -- VRC's own
            # optional setups especially -- carries a hand-authored map. A hill
            # climb only reaches what its starting point deforms into, and
            # VRC's Spa map simulated 0.85 s faster than this solver's own
            # answer, so their work is used as an extra starting point.
            reference = self._reference_maps(
                lap, Path(path).name if cold else None)
            if reference:
                emit("Each reference map is an extra hill climb: %d seed%s means "
                     "%d climbs per strategy."
                     % (len(reference), "" if len(reference) == 1 else "s",
                        len(reference) + 1))
            if reference:
                emit("Also starting from %d authored map(s) found in the track's "
                     "setup folder." % len(reference))
            emit()

            for label, strat_no, session, neutral, one_shot in STRATEGIES:
                self.messages.put(("status", "Optimising %s..." % label))
                # The car's ERS_SESSION_LIMITS defaults to "Current Session", so
                # the cap actually enforced is whichever session is loaded --
                # practice allows 9.0 MJ where qualifying allows 8.0. Planning to
                # the table while driving in practice silently discards the
                # difference, so a lap that demonstrably harvested more than the
                # table allows is taken as proof of the real limit.
                cap = alloc.mj(session)
                if harvested > cap + 0.2:
                    emit("NOTE: %s plans to the published %.1f MJ, but your lap "
                         "harvested %.2f MJ." % (label, cap, harvested))
                    emit("      ERS_SESSION_LIMITS is \"Current Session\", so a "
                         "practice session enforces 9.0 MJ")
                    emit("      where qualifying enforces %.1f. Drive the lap in "
                         "a Qualify session and the" % cap)
                    emit("      car will hold you to the same budget this plan "
                         "assumes.")
                objective = optimise26.Objective(
                    name=label, harvest_cap_mj=cap,
                    energy_neutral=neutral, one_shot=one_shot,
                    min_store_fraction=0.25 if neutral else 0.0,
                    # A qualifying map assumes the recharge lap left the store
                    # full. Arrive at 78% instead, as a real Spa lap did, and a
                    # map tuned only for full falls apart faster than one that
                    # was not -- so it is scored at three-quarters charge too.
                    charge_fractions=(0.75,) if one_shot else ())
                kind = "quali" if strat_no == 1 else ("race" if strat_no == 2 else "recharge")
                powertrain = fa26.Powertrain(taper=taper_for(strat_no))
                start_store = optimise26.starting_store_kj(lap, kind)
                if strat_no == 3:
                    # Built, not searched -- see optimise26.recharge_map.
                    splits, result = optimise26.build_recharge(
                        seed, car, cal, profile, powertrain,
                        start_store_kj=start_store,
                        harvest_cap_kj=objective.harvest_cap_mj * 1000,
                        reset_zones=zones_for(strat_no), speed_zones=speed_zones)
                    evaluations = 1
                else:
                    splits, result, evaluations = optimise26.optimise(
                        seed, car, cal, profile, powertrain, objective,
                        start_store, extra_seeds=reference,
                        reset_zones=zones_for(strat_no), speed_zones=speed_zones)

                emit("%-11s -> STRAT %d   lap %.3f s   deploy %.2f MJ   harvest %.2f MJ"
                     % (label, strat_no, result.lap_time_s,
                        result.deployed_mj, result.harvested_mj))
                # A map the car will not execute is worse than no map: it looks
                # like a plan and quietly does something else. The search is
                # held to this, so a survivor here is a bug, not a preference.
                # Say when a second run is worth it. A map built from laps
                # driven on an empty map takes the car past the speeds the drag
                # fit ever saw, and it is the user who has to close that loop.
                over = cal.extrapolating_kph(result.speed)
                if over > 8.0:
                    emit("               NOTE: these laps were fitted up to "
                         "%.0f kph and this map runs to %.0f. Drive it and "
                         "re-solve -- the second answer is the real one."
                         % (cal.observed_speed_p95 * 3.6,
                            max(result.speed) * 3.6))
                illegal = optimise26.illegal_increases(
                    splits, profile, zones_for(strat_no))
                if illegal:
                    emit("   WARNING: %d power increase(s) under throttle the car "
                         "cannot execute: %s" % (len(illegal), illegal[:4]))
                # A settled lap finds its own operating charge, so report where
                # the measured lap actually began rather than the seed value.
                began = result.store_start_kj
                emit("               starts at %.0f kJ (%.0f%%), ends %.0f kJ (min %.0f)"
                     % (began, 100 * began / 4000.0,
                        result.store_end_kj, result.store_min_kj))
                # A recharge lap refuses harvest once it is full, which is the
                # point of it, not a fault to report.
                if result.refused_mj > 0.1 and strat_no != 3:
                    emit("               %.2f MJ of harvest refused -- the store "
                         "was full. Deploy earlier to make room."
                         % result.refused_mj)
                spread = optimise26.sensitivity(
                    splits, car, cal, profile, powertrain, objective)
                emit("               if the lap starts with more or less: "
                     + ", ".join("%.0f%% -> %.2fs" % (f * 100, r.lap_time_s)
                                 for f, r in spread))
                self.results.append((label, strat_no, splits, result))

            emit()
            emit("Split tables")
            for label, strat_no, splits, _ in self.results:
                emit("  %s (STRAT %d)" % (label, strat_no))
                emit("    %7s %10s %11s %9s %9s %8s"
                     % ("start", "deployEnd", "clipStart", "splitEnd", "deploykW", "clipkW"))
                # Show what will actually be written, not the raw search
                # state. `clamped` resolves CLIP_OFF into a real ordering, and
                # printing the 50000 marker here reads as the malformed
                # encoding the car silently discards -- which is exactly the
                # bug this project spent a long time chasing.
                for s in (s.clamped(lap.length_m) for s in splits):
                    if s.deploy_kw <= 0 and s.clip_kw <= 0:
                        continue
                    emit("    %7d %10d %11d %9d %9d %8d"
                         % (s.start_m, s.deploy_end_m, s.clip_start_m, s.end_m,
                            s.deploy_kw, s.clip_kw))

            self.lap = lap
            self.alloc = alloc
            self.messages.put(("status", "Done"))
            self.messages.put(("done", None))
        except Exception:
            self.messages.put(("status", "Failed"))
            self.messages.put(("error", traceback.format_exc()))

    def _car_has_loaded(self, written: str) -> str:
        """Whether the game has picked up the file just written.

        Assetto Corsa copies the setup it loads into generic/last.ini, so if
        that file is older than the one just written, the car is still running
        the previous map. Three separate on-track sessions were spent chasing a
        bug that was really this, so it is worth saying out loud.
        """
        last = docs_dir() / "setups" / CAR_ID / "generic" / "last.ini"
        try:
            if not last.exists():
                return ""
            if last.stat().st_mtime < Path(written).stat().st_mtime:
                import time
                stamp = time.strftime("%H:%M", time.localtime(last.stat().st_mtime))
                return "the car last loaded a setup at %s" % stamp
        except OSError:
            return ""
        return ""

    #: Each extra seed is a whole extra hill climb, so the cap is a direct
    #: multiplier on solve time, not a refinement.
    MAX_REFERENCE_SEEDS = 2

    def _reference_maps(self, lap, track: str | None = None) -> list:
        """Maps *someone else* authored for this track, as extra seeds.

        The point of a second starting point is to reach a shape the seed
        cannot deform into -- VRC's Spa map simulated 0.85 s faster than the
        one this solver reached on its own. The point is not to restart from
        this tool's own previous answer, which is what the folder scan was
        actually doing: it read every .ini in the track's setup folder, so a
        Monza solve picked up the three maps written by the last run and ran
        four full climbs instead of one. That is most of a solve's run time,
        spent re-deriving what it already produced.

        A file this tool has written is identifiable: `setupfile.write_values`
        leaves a `.bak` beside it holding the content from before the first
        write. So where a `.bak` exists, the `.bak` is the authored original
        and the `.ini` is our own output -- read the former and skip the
        latter.
        """
        track = track or Path(self.lap_var.get()).name.split("__")[0]
        mapping = setupfile.item_index_map(self.car_setup_ini)
        found: list = []
        seen: set = set()

        def remember(splits) -> None:
            """Keep a map only if its shape is one not already collected.

            The shipped corpus entry for a track is usually byte-identical to
            map 1 of the setup sitting in that track's folder -- they come from
            the same file. Before these were compared, a Spa solve spent both
            its seed slots on the same map and dropped VRC's map 2 entirely,
            and that map replays 0.30 s faster than what the solver produced
            from the one it kept. A duplicate costs a whole hill climb and
            buys nothing.
            """
            key = tuple((s.start_m, s.deploy_end_m, s.clip_start_m, s.end_m,
                         s.deploy_kw, s.clip_kw) for s in splits)
            if key not in seen:
                seen.add(key)
                found.append(splits)

        # The shipped corpus of VRC-authored maps, where it covers this track.
        for splits in recharge.reference_maps_for(track):
            remember([fa26.Split(*row) for row in splits])

        folder = docs_dir() / "setups" / CAR_ID / track
        if folder.is_dir():
            for path in sorted(folder.glob("*.ini")):
                backup = path.with_suffix(path.suffix + ".bak")
                source = backup if backup.exists() else path
                try:
                    text = source.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for index in range(1, 13):
                    try:
                        splits = fa26.read_map(text, mapping, index)
                    except Exception:
                        continue
                    if len(splits) >= 4:
                        remember(splits)
        return found[: self.MAX_REFERENCE_SEEDS]

    # ----------------------------------------------------------------- apply
    def _apply(self) -> None:
        if not self.results:
            return
        start = docs_dir() / "setups" / CAR_ID
        path = filedialog.askopenfilename(
            title="Setup .ini to write into",
            initialdir=str(start if start.exists() else Path.home()),
            filetypes=[("Setup", "*.ini"), ("All files", "*.*")])
        if not path:
            return
        summary = "\n".join("  STRAT %d  %s" % (n, label)
                            for label, n, _, _ in self.results)
        if not messagebox.askyesno(
            "Confirm",
            "Write into\n\n%s\n\n%s\n\nOnly deployment maps 1-3 and their strat "
            "pointers are changed. A .bak copy is made first."
            % (Path(path).name, summary),
        ):
            return
        try:
            mapping = setupfile.item_index_map(self.car_setup_ini)
            backup = fa26.write_strategies(
                path, mapping,
                [(n, splits) for _, n, splits, _ in self.results],
                self.lap.length_m,
                allocation=self.alloc if self.write_alloc_var.get() else None,
                active={"Qualifying": 1, "Race": 2, "Recharge": 3}[self.active_var.get()],
                # Keep a dated copy of the written setup beside the telemetry.
                # Without one there is no way to tell afterwards which map a
                # recorded lap was driven on, because this overwrites the setup
                # in place -- which is how every Monza lap on disk came to be
                # replayed against a map written after the lap was recorded.
                telemetry_dir=baseline_dir())
            self.log("")
            self.log("Wrote 3 strategies to %s" % path)
            if backup:
                self.log("Backup   %s" % backup)
            stale = self._car_has_loaded(path)
            self.log("")
            if stale:
                self.log("!! THE CAR HAS NOT LOADED THIS YET -- " + stale)
                self.log("!! Anything you drive now uses the previous map.")
                self.log("")
            self.log("RESTART THE SESSION before driving. Assetto Corsa reads the")
            self.log("setup when the car loads, so a file written while you are")
            self.log("already on track never reaches the car -- three laps driven")
            self.log("that way on STRAT 1, 2 and 3 came out identical, because all")
            self.log("three were still running the previous map.")
            active_name = self.active_var.get()
            active_no = {"Qualifying": 1, "Race": 2, "Recharge": 3}[active_name]
            self.log("ACTIVE NOW: STRAT %d, %s -- that is what the car will"
                     % (active_no, active_name))
            self.log("drive until you change it. The setup item decides; nothing else does.")
            self.log("Switch strategy with \"STRAT Map\" in the pit setup screen:")
            self.log("    1 = Qualifying     2 = Race     3 = Recharge")
            self.log("(the PU mode switch does NOT select the map -- its overrides")
            self.log(" never fire, so they are written off deliberately.)")
            messagebox.showinfo(
                "Applied",
                "STRAT %d (%s) is the selected one; all three were updated.\nBackup: %s\n\nRESTART THE SESSION before "
                "driving -- reloading the setup in the pit menu is not enough, "
                "because the car reads these values when it loads.\n\nCheck it "
                "worked: STRAT 3 should be several seconds a lap slower and "
                "hardly deploy at all. If all three feel the same, the maps did "
                "not reach the car." % (active_no, active_name, backup))
        except Exception:
            messagebox.showerror("Apply failed", traceback.format_exc())


def main() -> int:
    root = tk.Tk()
    root.title("FA26 ERS Deployment Optimizer")
    root.geometry("1000x820")
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
