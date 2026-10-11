"""Per-track lap recharge allocation for the FA26.

The 2026 car's maximum harvest per lap is set per circuit, and it is the
constraint that governs how much can be deployed: deployment itself is
uncapped, so a lap can only spend what it banked. Getting this number right
matters more than any other single setting.

The setup stores it in tenths of a MJ (40 = 4.0 MJ, 90 = 9.0 MJ) across five
session variants. Values are kept in a JSON table that can be extended by hand
or learned from a VRC-authored setup, which is authoritative where one exists.
"""

from __future__ import annotations

import json
import re
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path

from paths import resource_root

TABLE_PATH = resource_root() / "data" / "fa26_recharge.json"

#: The circuit table as last downloaded, laid over the one built into the
#: program; see `circuit_updates.py`. Each FIA Power Unit Information notice
#: reaches it within hours of publication, so a race weekend needs no new
#: version of the program.
#: The table's own entry for when it was last updated, ISO time in UTC.
META = "_meta"

DOWNLOADED_PATH = (Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"))
                   / "FA26 Optimizer" / "circuits.json")

#: Whether a declared C5.12.5 zone lets the power demand rise under full
#: throttle. The notice's words are "a reset of MGU-K power *reduction* is
#: permitted", and the model took that for a reset of the demand latch. The
#: car does not do it. Every step up a map asked for inside a declared zone,
#: with the driver already past the knee so that only the zone could allow it:
#: 0 of 9 delivered -- seven at Baku's exit of T19, two at Sepang's exit of T6,
#: with up to 2.5 MJ in the store. Outside the zones the same events are
#: refused 0 of 8, as the latch says. Granting them gave a Sepang map 0.77 MJ
#: through T6 that the car kept back and spent at the end of the lap, and an
#: A/B the model called 0.18 s one way came out 0.10 s the other.
#:
#: Every lap measured is a practice session. A real qualifying session might
#: behave differently, and this is the switch if it is ever shown to.
RESET_ZONES_RAISE_DEMAND = False

#: Whether power is cut at once, rather than ramped, below a declared C5.12.7
#: sector's speed. The model did so on the regulation's word. Every split end
#: measured inside such a sector, below its speed, with the driver flat: 8 of
#: 8 ramped -- 220 kW to 150 half a second later, 318 to 250 -- exactly as
#: the car does everywhere else. Cutting at once took about 0.3 MJ a pass off
#: both Sepang maps through T5-T6 and T12-T13 that the car delivered.
SPEED_ZONES_CUT_INSTANTLY = False

SESSION_ITEMS = {
    "practice": "ERS_REGEN_MAX_MJ_P",
    "qualify": "ERS_REGEN_MAX_MJ_Q",
    "race": "ERS_REGEN_MAX_MJ_R",
    "race_overtake": "ERS_REGEN_MAX_MJ_R_OT",
    "outlap": "ERS_REGEN_MAX_MJ_OL",
}

# Stored as tenths of a MJ, matching the setup file.
UNITS_PER_MJ = 10


@dataclass
class Allocation:
    """Per-session harvest limits, in tenths of a MJ.

    The FIA publishes five figures per event, and they differ: the 2026
    Australian Grand Prix notice gives race 8.0 MJ, race with Overtake 8.5,
    qualifying 7.0, free practice 8.5 and out laps 8.5. A circuit's headline
    number is its qualifying figure, so it must not be applied to every session
    -- doing that briefly had Monza planning its race laps to 5.0 MJ.

    The qualifying figure runs from 5.0 MJ at Monza to 9.0 MJ at Monaco,
    Hungaroring and Singapore; 7.0 MJ is used when it is unknown, which is the
    middle of the published range.

    Confidence check: the published Barcelona figure of 7.0 MJ is exactly what
    VRC ships in its own Barcelona setup, so the mod follows this table rather
    than the car's maxima.
    """

    practice: int = 90
    qualify: int = 70
    race: int = 80
    race_overtake: int = 85
    outlap: int = 90
    source: str = "fia-baseline"
    #: Article C5.12.8, which the event notice sets per circuit at 50 or
    #: 100 kW/s. The car exposes it as ERS_POWER_REDUCTION_RATE, and it decides
    #: how fast deployment may fall, so it belongs with the allocation.
    rate_kw_s: int = 100
    power_limited_m: int = 0
    #: Article C5.2.8 curve names, normal running and with Override active.
    curve_standard: str | None = None
    curve_override: str | None = None
    #: Article C5.12.5 -- lap-distance windows where a reset of MGU-K power
    #: reduction is permitted, so deployment may be raised under full throttle.
    #: Entered by hand from the event notice rather than parsed out of
    #: `exceptions_raw`: that text interleaves the C5.12.4, C5.12.5, C5.12.7
    #: and C5.2.8iii sector tables with no delimiter between them, and the
    #: order varies by round, so a regex over it cannot tell which article a
    #: window belongs to. An empty tuple means "none declared", which is also
    #: the safe default -- it reproduces the behaviour of not modelling them.
    #: Each entry is (start_m, end_m, scope), scope "q" where the notice
    #: brackets the sector as SQ and Q only. Read them through
    #: `reset_zones_for`, which applies the scope. Most published zones are
    #: qualifying-only, and a race map that assumed otherwise was being allowed
    #: a power increase the race car will refuse -- but not all of them: Baku's
    #: exit of T19, 4650-5600 m, is unbracketed and applies in the race too.
    reset_zones: tuple = ()
    #: Article C5.12.7 -- sectors with a higher speed threshold for cutting
    #: ERS-K power instantly, as (start_m, end_m, kph). Unlike the C5.12.4 and
    #: C5.12.5 tables this one reads off the notice unambiguously, because its
    #: windows are followed by their own km/h column. Monza and Red Bull Ring
    #: declare none; Barcelona declares three at 260 km/h.
    speed_zones: tuple = ()
    #: Article C5.2.8iii -- the alternative ERS-K power curve named for
    #: declared sectors in Sprint and Race.
    curve_alternative: str | None = None
    #: Article C5.2.8iii -- the lap-distance windows where that alternative
    #: curve applies, as (start_m, end_m). Race and Sprint only; qualifying and
    #: practice run the Override curve everywhere. Baku's notice names three --
    #: T1-T2, T3-T12 and T15-T16 -- and caps deployment there at Alt 1's
    #: 250 kW, while the straights between them keep the Base curve. Earlier
    #: notices named Alt 1 without these windows being entered, so there the
    #: curve was written to the car but the model never applied it.
    alt_zones: tuple = ()
    #: The notice's centreline length in metres. Sector windows are quoted
    #: against it, and an AC track's spline rarely measures the same -- Madrid
    #: is 5416 m on the notice and 5340 m in telemetry, Monza 5793 against
    #: 5750 -- so a window near the end of the lap lands up to 80 m out if it
    #: is used as published. Given both lengths the windows can be scaled.
    centreline_m: int = 0
    #: Power curves this circuit's notice brought in that the program was
    #: built without, as {name: [[kph, kW], ...]}; see fa26.register_curves.
    curves: dict = field(default_factory=dict)
    #: Other names the circuit goes by -- the notice's venue and its event's
    #: country -- so a track folder named after either finds it.
    aliases: tuple = ()

    @property
    def is_baseline(self) -> bool:
        return self.source == "fia-baseline"

    def mj(self, session: str = "qualify") -> float:
        return getattr(self, session, self.qualify) / UNITS_PER_MJ

    def _scale(self, length_m: float | None) -> float:
        if not length_m or not self.centreline_m:
            return 1.0
        return float(length_m) / float(self.centreline_m)

    def reset_zones_for(self, session: str = "qualify",
                        length_m: float | None = None,
                        as_published: bool = False) -> tuple:
        """Article C5.12.5 windows applying in this session, as (start, end).

        Pass the lap's own length to map the notice's centreline distances onto
        the track as the game measures it.

        Empty unless `RESET_ZONES_RAISE_DEMAND`: the model reads these as places
        the demand may rise under full throttle, and the car does not honour
        that -- see the constant. `as_published` returns them regardless, for
        anything that wants the notice rather than the car.
        """
        if not (RESET_ZONES_RAISE_DEMAND or as_published):
            return ()
        quali = session in ("qualify", "qualifying", "practice", "outlap")
        k = self._scale(length_m)
        return tuple((int(round(z[0] * k)), int(round(z[1] * k)))
                     for z in (self.reset_zones or ())
                     if len(z) < 3 or z[2] != "q" or quali)

    def alt_zones_for(self, session: str = "race",
                      length_m: float | None = None) -> tuple:
        """Article C5.2.8iii windows, as (start, end), for a race or sprint.

        Empty in any other session: practice and qualifying run the Override
        curve everywhere, and the car picks its curve by session.
        """
        if session not in ("race", "race_overtake", "sprint") or not self.alt_zones:
            return ()
        k = self._scale(length_m)
        return tuple((int(round(z[0] * k)), int(round(z[1] * k)))
                     for z in self.alt_zones)

    def speed_zones_for(self, length_m: float | None = None,
                        as_published: bool = False) -> tuple:
        """Article C5.12.7 windows, as (start, end, kph). Not session-scoped.

        Empty unless `SPEED_ZONES_CUT_INSTANTLY`, for the same reason as the
        reset zones: the model uses these for one rule the car does not
        follow. `as_published` returns them regardless.
        """
        if not (SPEED_ZONES_CUT_INSTANTLY or as_published):
            return ()
        k = self._scale(length_m)
        return tuple((int(round(z[0] * k)), int(round(z[1] * k)), float(z[2]))
                     for z in (self.speed_zones or ()))


#: Shortest circuit name that may be matched as a substring of a folder name.
MIN_CONTAINED_NAME = 5

_CANONICAL: tuple = ()


def _canonical() -> tuple:
    """Known circuit keys, longest first, so the most specific name wins."""
    global _CANONICAL
    if not _CANONICAL:
        names = set(DEFAULT_TABLE) | set(_raw_table())
        _CANONICAL = tuple(sorted(names, key=len, reverse=True))
    return _CANONICAL


def _data_aliases() -> dict:
    out = {}
    for key, entry in _raw_table().items():
        for alias in entry.get("aliases") or ():
            if alias and alias != key:
                out.setdefault(alias, key)
    return out


def valid_entry(entry) -> bool:
    """Whether a circuit entry is safe to use: every field known, limits sane."""
    try:
        alloc = Allocation(**entry)
    except TypeError:
        return False
    limits = (alloc.practice, alloc.qualify, alloc.race, alloc.race_overtake,
              alloc.outlap)
    return (all(isinstance(v, int) and 30 <= v <= 90 for v in limits)
            and alloc.rate_kw_s in (50, 100))


def _raw_table() -> dict:
    """The built-in table with the downloaded one laid over it, entry by entry.

    A downloaded entry replaces a built-in one of the same name and adds any
    the program does not know. One that does not pass `valid_entry` is
    ignored, and a download that cannot be read at all changes nothing.
    """
    try:
        table = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        table = {}
    try:
        downloaded = json.loads(DOWNLOADED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        downloaded = {}
    built = str((table.pop(META, None) or {}).get("updated", ""))
    if isinstance(downloaded, dict):
        # Only a table at least as new as the one built in. Otherwise a
        # build made after a notice would be overruled by an older download
        # -- which is what any copy of the program would have done between
        # its release and the repository catching up.
        fetched = str((downloaded.get(META) or {}).get("updated", ""))
        if fetched >= built:
            for key, entry in downloaded.items():
                if key != META and isinstance(entry, dict) and valid_entry(entry):
                    table[key] = entry
    return table


def built_in_stamp() -> str:
    """When the table this program was built with was last updated."""
    try:
        table = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str((table.get(META) or {}).get("updated", ""))


def reload() -> None:
    """Forget cached circuit names after the table has been updated."""
    global _CANONICAL
    _CANONICAL = ()


def normalise(track: str) -> str:
    """Reduce a track or telemetry name to a stable key.

    Telemetry files are named `<track>__<layout>__<car>__<stamp>__lapNN_...`,
    and some tracks carry no layout at all, so the car id is used as the
    terminator rather than a fixed field count.
    """
    name = Path(track).name
    for suffix in (".csv", ".ini"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    parts = name.split("__")
    if parts:
        head = []
        for p in parts:
            if p.startswith("vrc_formula_alpha") or re.match(r"^\d{8}_\d{6}$", p):
                break
            head.append(p)
        if head:
            name = head[0]
    # Accents dropped rather than deleted: "Montréal" is "montreal".
    import unicodedata
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.lower().strip()
    name = re.sub(r"^(fn_|rj_|acu_|csp_|ks_)", "", name)
    name = re.sub(r"[^a-z0-9]+", "", name)
    # Track folders embed years and layout tags anywhere in the name
    # (lasvegas23, abu_dhabi_2021_chq), so drop digits wherever they appear.
    name = re.sub(r"[0-9]+", "", name)
    name = ALIASES.get(name) or _data_aliases().get(name, name)
    if name in _canonical():
        return name

    # Track folders carry the author's prefix and the layout's suffix --
    # rt_suzuka, vhe_interlagos, miami_f1, shanghai_v2, monaco_2019_CHQ -- and
    # stripping a fixed list of prefixes never keeps up: nine circuits on the
    # 2026 calendar were silently falling through to the baseline allocation,
    # so a Suzuka solve planned to 7.0 MJ instead of the published figure and
    # said the circuit was not on the list. Matching the longest circuit name
    # contained in the folder name covers the lot without a list to maintain.
    #
    # Short names are excluded: "spa" and "baku" turn up inside unrelated
    # words, and both already resolve exactly or through an alias.
    for key in _canonical():
        if len(key) >= MIN_CONTAINED_NAME and key in name:
            return key
    # And the names the notices give, the same way: a circuit new to the
    # table arrives with its venue and country, and a track folder carries
    # them inside an author's prefix and a year.
    for alias, key in sorted(_data_aliases().items(), key=lambda kv: -len(kv[0])):
        if len(alias) >= MIN_CONTAINED_NAME and alias in name:
            return key
    return name


#: AC track folder names vary by author, so map the ones that occur onto the
#: circuit the published allocation table names.
ALIASES = {
    "melbourne": "albertpark", "australia": "albertpark",
    # The 2026 Spanish Grand Prix is at Madrid, not Barcelona-Catalunya, which
    # is a separate round. The AC folder is madrid_street_circuit_2026, and
    # stripping its digits leaves "madridstreetcircuit", which matched nothing
    # -- so every Madrid lap silently got the generic baseline allocation
    # instead of the published one.
    "madridstreetcircuit": "madrid", "madring": "madrid",
    "spain": "madrid", "spanish": "madrid",
    "spielberg": "redbullring", "austria": "redbullring",
    "catalunya": "barcelona", "montmelo": "barcelona",
    "villeneuve": "montreal", "gillesvilleneuve": "montreal", "canada": "montreal",
    "vegas": "lasvegas",
    "brazil": "interlagos", "saopaulo": "interlagos",
    "losail": "lusail", "qatar": "lusail",
    "abudhabi": "yasmarina", "abudhabichq": "yasmarina", "yasmarinacircuit": "yasmarina",
    "china": "shanghai",
    "japan": "suzuka",
    "bahrain": "sakhir",
    "montecarlo": "monaco",
    "spafrancorchamps": "spa", "belgium": "spa",
    # VRC names its shipped setups by country, the allocation table by circuit.
    "greatbritain": "silverstone", "britain": "silverstone",
    "italy": "monza", "italian": "monza",
    "hungary": "hungaroring", "budapest": "hungaroring",
    "madring": "madrid",
    "azerbaijan": "baku",
    "marinabay": "singapore",
    "cota": "austin", "americas": "austin", "circuitoftheamericas": "austin",
    "mexicocity": "mexico", "rodriguez": "mexico", "hermanosrodriguez": "mexico",
    # Round 16 of 2026 is the "Bahrain Grand Prix in Malaysia", run at Sepang.
    # Named whole, it is Sepang's; "bahrain" alone stays Sakhir's.
    "malaysia": "sepang", "malaysian": "sepang",
    "bahraininmalaysia": "sepang", "bahraingrandprixinmalaysia": "sepang",
}


def load_table() -> dict[str, Allocation]:
    raw = _raw_table()
    if not raw:
        return dict(DEFAULT_TABLE)
    import fa26
    for entry in raw.values():
        fa26.register_curves(entry.get("curves"))
    return {k: Allocation(**v) for k, v in raw.items()}



def save_table(table: dict[str, Allocation]) -> None:
    TABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TABLE_PATH.write_text(
        json.dumps({k: asdict(v) for k, v in sorted(table.items())}, indent=2),
        encoding="utf-8",
    )


def lookup(track: str, table: dict[str, Allocation] | None = None) -> Allocation:
    """Allocation for a track, falling back to the published baseline.

    The FIA sets the per-event figure rather than publishing a fixed table, so
    there is nothing to cross-reference for an arbitrary circuit. Returning the
    baseline is defensible where a specific value is unknown -- it is what the
    regulations default to -- but callers should check `is_baseline` and say so,
    because harvest governs how much can be deployed and a wrong figure
    mis-plans the whole lap.
    """
    table = table if table is not None else load_table()
    return table.get(normalise(track), Allocation())


def learn_from_setup(setup_text: str, mapping: dict[str, int], track: str,
                     source: str = "setup") -> Allocation:
    """Read the allocation out of a setup, e.g. a VRC-authored one."""
    import setupfile
    values = setupfile.read_values(setup_text)
    fields = {
        session: values.get(mapping[item])
        for session, item in SESSION_ITEMS.items()
    }
    if any(v is None for v in fields.values()):
        raise ValueError("setup does not declare the recharge items")
    return Allocation(source=f"{source}:{normalise(track)}", **fields)


#: setup_ers_power_curve.lut, in declaration order.
CURVE_INDEX = {
    "Base - Standard": 0,
    "Base - Overtake": 1,
    "Rev 1 - Standard": 2,
    "Rev 1 - Overtake": 3,
    "Alt 1": 4,
}

def updates_for(alloc: Allocation, mapping: dict[str, int]) -> dict[int, int]:
    """Setup items to write so a car uses this allocation.

    Every item on the car's Power & Energy Limits page that the event notice
    sets: the five per-lap recharge limits, the power reduction rate and the
    four power curves. Not the Power and Energy Limits selector above them,
    which says which session's figures the car enforces. The notice has no
    view on that; it is how a driver makes a practice session run to
    qualifying's limits, and it was being reset to "Current Session" on every
    apply -- a Baku setup set to Qualify came back from the 12:15 apply on
    Current Session and was put back by hand at 12:31.
    """
    updates = {
        mapping[item]: getattr(alloc, session)
        for session, item in SESSION_ITEMS.items()
    }
    if "ERS_POWER_REDUCTION_RATE" in mapping:
        updates[mapping["ERS_POWER_REDUCTION_RATE"]] = alloc.rate_kw_s

    # Article C5.2.8 names the curve for normal running and for Override. The
    # car selects one per session: PQ covers practice and qualifying, which run
    # with Override; R is the race curve; R_OT is the race curve with Override
    # active. Leaving these at whatever the setup shipped with let the car run a
    # different curve from the one the strategies were planned against.
    standard = CURVE_INDEX.get(alloc.curve_standard)
    override = CURVE_INDEX.get(alloc.curve_override)
    # C5.2.8iii names an alternative curve that applies in declared sectors
    # during Sprint and Race. The car has a setup item for it and the tool was
    # leaving it at whatever the setup shipped with, so a race run could be on
    # a curve the event does not call for. Monza names "Alt 1". Where nothing
    # is named, or the name has no published numbers, the item is left alone
    # rather than guessed at.
    alternative = CURVE_INDEX.get(alloc.curve_alternative)
    for item, index in (("ERS_POWER_CURVE_R", standard),
                        ("ERS_POWER_CURVE_PQ", override),
                        ("ERS_POWER_CURVE_R_OT", override),
                        ("ERS_POWER_CURVE_R_ALT", alternative)):
        if index is not None and item in mapping:
            updates[mapping[item]] = index
    return updates


#: What `updates_for` writes, as the car's Power & Energy Limits page labels it.
MENU_RECHARGE = (("Max Lap Recharge (Race)", "race"),
                 ("Max Lap Recharge (Race + OT)", "race_overtake"),
                 ("Max Lap Recharge (Qualify)", "qualify"),
                 ("Max Lap Recharge (Practice)", "practice"),
                 ("Max Lap Recharge (Non-Race Outlap)", "outlap"))
MENU_CURVES = (("Max Power vs Speed (Race)", "curve_standard"),
               ("Max Power vs Speed (Race + OT)", "curve_override"),
               ("Max Power vs Speed (Race Alt)", "curve_alternative"),
               ("Max Power vs Speed (Non-Race)", "curve_override"))


def menu_rows(alloc: Allocation) -> list[tuple[str, str]]:
    """The settings `updates_for` writes, worded as the car's menu shows them.

    So the driver can hold what was saved against the setup screen without
    opening the file. A curve the table does not name is left out, because
    `updates_for` leaves that item alone.
    """
    rows = [(label, "%.1f MJ" % (getattr(alloc, session) / 10.0))
            for label, session in MENU_RECHARGE]
    rows.append(("Power Reduction Rate", "%d kW/s" % alloc.rate_kw_s))
    for label, field_name in MENU_CURVES:
        name = getattr(alloc, field_name)
        if name in CURVE_INDEX:
            rows.append((label, name))
    return rows


def reference_maps_for(track: str) -> list:
    """VRC's own authored deployment map for a track, if the corpus has one.

    Six circuits are covered; the rest return nothing rather than a map from
    somewhere else. Used as an extra starting point for the search, which is
    only worth doing when the map really was authored by someone else.
    """
    path = TABLE_PATH.parent / "fa26_reference_maps.json"
    if not path.exists():
        return []
    corpus = json.loads(path.read_text(encoding="utf-8"))
    entry = corpus.get(normalise(track))
    return [entry["splits"]] if entry and entry.get("splits") else []


def candidate_zones(track: str) -> list:
    """Lap-distance windows named anywhere in an event notice, for transcription.

    The C5.12.4, C5.12.5, C5.12.7 and C5.2.8iii sector tables all appear in the
    notice text, but the dump interleaves them with no delimiter and the column
    order varies by round, so which article a window belongs to cannot be
    recovered automatically -- Red Bull Ring lists five windows across three
    tables, Silverstone eleven. This returns every window it can see so the
    right ones can be entered into `reset_zones` and `speed_zones` by hand
    against the notice, instead of being guessed at.
    """
    import re as _re
    path = TABLE_PATH.parent / "fa26_fia_events.json"
    if not path.exists():
        return []
    events = json.loads(path.read_text(encoding="utf-8"))
    raw = (events.get(normalise(track)) or {}).get("exceptions_raw", "")
    pattern = r"\[?(\d{3,4})\s*-\s*(\d{3,4})\]?"
    return [(int(a), int(b)) for a, b in _re.findall(pattern, raw)]


# Seeded from VRC's own Barcelona setup, and from allocations reported for
# circuits where the number is known. Everything else is left absent on purpose:
# guessing an allocation would silently mis-plan a whole lap.
DEFAULT_TABLE: dict[str, Allocation] = {
    "barcelona": Allocation(practice=90, qualify=70, race=85, race_overtake=90,
                            outlap=90, source="vrc:Barcelona.ini"),
    "monza": Allocation(practice=50, qualify=50, race=50, race_overtake=50,
                        outlap=40, source="reported"),
    "hungaroring": Allocation(practice=90, qualify=90, race=85, race_overtake=90,
                              outlap=90, source="reported"),
}


def scan_setups(setups_root: str | Path, mapping: dict[str, int],
                table: dict[str, Allocation] | None = None,
                skip_defaults: bool = True) -> tuple[dict[str, Allocation], list[str]]:
    """Learn allocations from every setup found under a car's setup folder.

    A setup sitting at the car's defaults tells us nothing, so those are
    skipped unless asked for. Where several setups exist for one track the
    first non-default one wins, and VRC-authored files are preferred because
    they carry the circuit's real allocation.
    """
    table = dict(table if table is not None else load_table())
    notes: list[str] = []
    root = Path(setups_root)
    if not root.exists():
        return table, [f"no setup folder at {root}"]

    default = Allocation()
    files = sorted(root.glob("*/*.ini"),
                   key=lambda p: (0 if "vrc" in p.stem.lower() else 1, p.stem.lower()))
    for path in files:
        track_dir = path.parent.name
        if track_dir == "generic":
            continue
        key = normalise(track_dir)
        try:
            alloc = learn_from_setup(
                path.read_text(encoding="utf-8", errors="replace"), mapping,
                track_dir, source=path.stem)
        except (ValueError, KeyError):
            continue
        is_default = all(getattr(alloc, f) == getattr(default, f)
                         for f in SESSION_ITEMS)
        if skip_defaults and is_default:
            notes.append(f"{track_dir}/{path.name}: at car defaults, ignored")
            continue
        if key in table and table[key].source.startswith("vrc:"):
            continue
        table[key] = alloc
        notes.append(f"{track_dir}/{path.name}: quali {alloc.mj('qualify'):.1f} MJ, "
                     f"race {alloc.mj('race'):.1f} MJ")
    return table, notes
