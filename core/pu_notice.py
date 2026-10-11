"""Reading the FIA's Power Unit Information notice for a Grand Prix.

Every round the FIA publishes a two-page notice: a cover sheet, then one form
with the event's recharge limits, power reduction rate, power curves and the
sectors where exceptions apply. The form is the same every round, so it is
read by position -- each value found on the row and under the column its
label sits over -- rather than by the order its text happens to come out in,
which runs down columns and loses which number belongs to which session.

Nothing here guesses. A value that cannot be placed is a `NoticeError`, and
`check` refuses an entry whose numbers are outside what the regulations
allow, so a notice laid out differently stops the import instead of
publishing something wrong to every player.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Recharge per lap is published in MJ and stored in tenths, as the car does.
UNITS_PER_MJ = 10

_WINDOW = re.compile(r"^(\[)?\s*(\d{2,5})\s*-\s*(\d{2,5})\s*\*?\s*(\])?\*?$")


class NoticeError(ValueError):
    """The notice could not be read with confidence."""


@dataclass
class _Line:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2.0


@dataclass
class Notice:
    event: str                 # "2026 SPANISH GRAND PRIX"
    round: str                 # "R14"
    venue: str                 # "Madrid"
    centreline_m: int
    race: int                  # tenths of a MJ
    race_overtake: int
    qualify: int
    practice: int
    outlap: int
    power_limited_m: int
    rate_kw_s: int
    curve_standard: str
    curve_override: str
    curve_alternative: str
    curve_practice: str
    alt_zones: list = field(default_factory=list)       # [start, end]
    reset_zones: list = field(default_factory=list)     # [start, end] or [start, end, "q"]
    speed_zones: list = field(default_factory=list)     # [start, end, kph]
    #: Every curve the chart draws, by its legend name: (kph, kW) corners
    #: from where the power first falls to where it reaches nothing.
    curves: dict = field(default_factory=dict)
    #: Sector windows left out because they carry a mark the form does not
    #: explain, as printed.
    skipped: list = field(default_factory=list)

    def entry(self, number: int | None = None) -> dict:
        """The notice as a circuit entry in data/fa26_recharge.json."""
        out = {
            "practice": self.practice, "qualify": self.qualify,
            "race": self.race, "race_overtake": self.race_overtake,
            "outlap": self.outlap,
            "source": "fia:%02d" % number if number else "fia",
            "rate_kw_s": self.rate_kw_s,
            "power_limited_m": self.power_limited_m,
            "curve_standard": self.curve_standard,
            "curve_override": self.curve_override,
            "centreline_m": self.centreline_m,
        }
        if self.curve_alternative:
            out["curve_alternative"] = self.curve_alternative
        if self.alt_zones:
            out["alt_zones"] = [list(z) for z in self.alt_zones]
        if self.speed_zones:
            out["speed_zones"] = [list(z) for z in self.speed_zones]
        if self.reset_zones:
            out["reset_zones"] = [list(z) for z in self.reset_zones]
        # A curve the app was built without travels with the circuit, so a
        # notice that brings one in -- Singapore's Rev 1 and Alt 2 -- needs
        # no new version of the program.
        new = {name: [list(p) for p in points]
               for name, points in self.curves.items()
               if name not in BUILT_IN_CURVES}
        if new:
            out["curves"] = new
        aliases = sorted({_key(self.venue)} | {_key(w) for w in _event_words(self.event)})
        out["aliases"] = [a for a in aliases if a]
        return out


def _lines(page) -> list[_Line]:
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            # Some notices space with non-breaking spaces (Austria's title is
            # "2026<NBSP>AUSTRIAN<NBSP>GRAND<NBSP>PRIX"); read as ordinary ones.
            text = " ".join(s["text"] for s in line["spans"]).replace(
                "\xa0", " ").strip()
            if text:
                out.append(_Line(text, *line["bbox"]))
    return out


def _find(lines, text, after_y=-1.0):
    hits = [l for l in lines if l.text.lower() == text.lower() and l.y0 > after_y]
    if not hits:
        raise NoticeError("the notice has no '%s'" % text)
    return min(hits, key=lambda l: l.y0)


def _value(lines, label, tolerance=3.0):
    """The value printed to the right of a label in the competition table."""
    key = _find(lines, label)
    row = [l for l in lines if abs(l.y0 - key.y0) < tolerance and l.x0 > key.x1]
    if not row:
        raise NoticeError("nothing beside '%s'" % label)
    return min(row, key=lambda l: l.x0).text


def _number(text, unit, what):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%s\s*" % unit, text)
    if not m:
        raise NoticeError("%s reads '%s', not a number in %s" % (what, text, unit))
    return float(m.group(1))


def _windows(lines, column_x, top, bottom, skipped=None, reach=30.0):
    """Lap-distance windows printed under one column between two heights."""
    out = []
    for l in lines:
        if not top < l.y0 < bottom or abs(l.xc - column_x) > reach:
            continue
        m = _WINDOW.match(l.text)
        if not m:
            continue
        start, end = int(m.group(2)), int(m.group(3))
        if "*" in l.text:
            # A footnote mark the form never explains. Madrid's alternative-
            # curve sector reads "1500-5100*"; taken as written it caps race
            # deployment at 250 kW over two thirds of the lap. Left out, and
            # reported, for a person to settle.
            if skipped is not None:
                skipped.append(l.text)
            continue
        qualifying = bool(m.group(1) or m.group(4))
        out.append((l.y0, [start, end, "q"] if qualifying else [start, end]))
    return [w for _y, w in sorted(out)]


_CURVE_NAME = re.compile(r"^(Base|Rev \d+|Alt \d+)\b")


def _rows(lines, unit):
    """Lines grouped into rows by height, within `unit`."""
    rows = []
    for l in sorted(lines, key=lambda l: l.y0):
        if rows and abs(rows[-1][0].y0 - l.y0) <= unit:
            rows[-1].append(l)
        else:
            rows.append([l])
    return rows


def parse(pdf_bytes: bytes) -> Notice:
    """Read a Power Unit Information notice. Raises NoticeError if it cannot.

    The FIA has changed this form during the season -- Australia's heads its
    recharge row "Normal lap / Overtake" and draws two curves, China's has no
    alternative-curve table, Austria's is printed half as large again -- so
    nothing here depends on where a table sits on the page. Values are found
    by what they are (the one row holding five MJ figures), tables by the
    article that heads them, and every distance is scaled to the page.
    """
    try:
        import pymupdf
    except ImportError:                          # older installs name it fitz
        import fitz as pymupdf
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    form = None
    for page in doc:
        if "POWER AND ENERGY LIMITS" in page.get_text():
            form = page
            break
    if form is None:
        raise NoticeError("no page carries the POWER AND ENERGY LIMITS form")
    lines = _lines(form)
    s = form.rect.width / 595.0                  # 1.0 on an A4 form

    event = next((l.text for l in lines
                  if re.fullmatch(r"\d{4} .*GRAND PRIX.*", l.text)), "")
    if not event:                                # Austria's sits on the cover
        text = " ".join(pg.get_text() for pg in doc)
        found = re.search(r"\d{4} [A-Z][A-Z \-]*GRAND PRIX[A-Z ]*",
                          re.sub(r"[\s\xa0]+", " ", text))
        event = found.group(0).strip() if found else ""
    round_no = _value(lines, "ROUND No.", 3 * s)
    venue = _value(lines, "VENUE", 3 * s)
    centreline_m = int(round(_number(_value(lines, "CENTRELINE", 3 * s), "km",
                                     "the centreline") * 1000))

    # Recharge per lap, the power limited distance and the rate: the one row
    # carrying five MJ figures, in the order Race, Race with Overtake,
    # Qualifying, Free Practice, Out laps.
    candidates = [r for r in _rows(lines, 3 * s)
                  if sum(1 for l in r if l.text.endswith("MJ")) >= 5]
    if len(candidates) != 1:
        raise NoticeError("found %d rows of recharge limits, not 1"
                          % len(candidates))
    row = sorted(candidates[0], key=lambda l: l.x0)
    mj = [l for l in row if l.text.endswith("MJ")]
    metres = [l for l in row if re.fullmatch(r"\d+\s*m", l.text)]
    rate = [l for l in row if l.text.endswith("kW/s")]
    if len(mj) != 5 or len(metres) != 1 or len(rate) != 1:
        raise NoticeError(
            "the recharge row has %d limits, %d distances and %d rates, "
            "not 5, 1 and 1" % (len(mj), len(metres), len(rate)))
    race, race_ot, qualify, practice, outlap = (
        int(round(_number(l.text, "MJ", "a recharge limit") * UNITS_PER_MJ))
        for l in mj)
    power_limited_m = int(_number(metres[0].text, "m", "the power limited distance"))
    rate_kw_s = int(_number(rate[0].text, "kW/s", "the reduction rate"))

    # The curves in force: the row of curve names outside the chart. Four on
    # the current form -- Race without and with Overtake, the alternative
    # curve, practice and qualifying -- and two on the first (Standard and
    # Override, the second also for practice and qualifying).
    box = _chart_box(form, lines)
    named = [l for l in lines if _CURVE_NAME.match(l.text)
             and not (box.x0 <= l.x0 <= box.x1 and box.y0 <= l.y0 <= box.y1)]
    rows = [sorted(r, key=lambda l: l.x0) for r in _rows(named, 3 * s)]
    if len(rows) != 1 or len(rows[0]) not in (2, 3, 4):
        raise NoticeError("the curves in force read %s, not one row of 2 to 4"
                          % [[l.text for l in r] for r in rows])
    names = [l.text for l in rows[0]]
    if len(names) == 4:
        curve_standard, curve_override, curve_alt, curve_practice = names
    elif len(names) == 3:                        # Monaco: no alternative curve
        curve_standard, curve_override, curve_practice = names
        curve_alt = ""
    else:
        curve_standard, curve_override = names
        curve_alt, curve_practice = "", curve_override

    # The exceptions, each table found by the article heading it:
    # C5.2.8iii alternative-curve sectors, C5.12.4 sectors with a larger
    # reduction at Power Limited Pending (not modelled), C5.12.5 reset
    # sectors. Then C5.12.7, the speed-threshold sectors.
    exceptions = _find(lines, "IDENTIFICATION OF SECTORS WHERE EXCEPTIONS APPLY")
    speed_heading = next((l for l in lines
                          if l.text == "Sectors with a higher speed threshold"), None)
    after = [l.y0 for l in lines if l.y0 > exceptions.y0 and l.text in
             ("OVERTAKE", "Sectors with a higher speed threshold")]
    bottom = min(after) - 5 * s if after else form.rect.height
    articles = [l for l in lines if l.y0 > exceptions.y0 and l.y0 < bottom
                and l.text in ("Article C5.2.8iii", "Article C5.12.4",
                               "Article C5.12.5")]
    headers = [l for l in lines if l.text == "Lap distance window"
               and exceptions.y0 < l.y0 < bottom]
    column = {}
    for h in headers:
        above = [a for a in articles if a.y0 < h.y0]
        if above:
            owner = min(above, key=lambda a: abs(a.xc - h.xc))
            column[owner.text] = h
    skipped: list = []
    reach = 30 * s

    def windows_for(article):
        h = column.get(article)
        if h is None:
            return []
        return _windows(lines, h.xc, h.y1 + 5 * s, bottom, skipped, reach)

    if "Article C5.12.5" not in column:
        raise NoticeError("the reset sector table (C5.12.5) is missing")
    alt_zones = [w[:2] for w in windows_for("Article C5.2.8iii")]
    reset_zones = windows_for("Article C5.12.5")

    speed_zones = []
    if speed_heading is not None:
        speed_window = next((l for l in lines if l.text == "Lap distance window"
                             and l.y0 > speed_heading.y0), None)
        threshold = next((l for l in lines if l.text == "Speed threshold"
                          and l.y0 > speed_heading.y0), None)
        if speed_window is not None and threshold is not None:
            for w in _windows(lines, speed_window.xc, speed_window.y1 + 5 * s,
                              speed_window.y1 + 70 * s, skipped, reach):
                y = _row_y(lines, w, speed_window, reach)
                same_row = [l for l in lines
                            if abs(l.xc - threshold.xc) < reach
                            and re.search(r"\d", l.text) and abs(l.y0 - y) < 3 * s]
                if not same_row:
                    raise NoticeError("speed sector %d-%d has no threshold"
                                      % tuple(w[:2]))
                kph = int(re.search(r"\d+", same_row[0].text).group())
                speed_zones.append([w[0], w[1], kph])

    notice = Notice(event, round_no, venue, centreline_m, race, race_ot,
                    qualify, practice, outlap, power_limited_m, rate_kw_s,
                    curve_standard, curve_override, curve_alt, curve_practice,
                    alt_zones, reset_zones, speed_zones,
                    _curves(form, lines, box), skipped)
    check(notice)
    return notice


def _chart_box(page, lines):
    """The white panel the power curve chart is drawn in: the one holding
    the "vCar (km/h)" axis title."""
    label = next((l for l in lines if l.text.startswith("vCar")), None)
    if label is None:
        raise NoticeError("the power curve chart is missing")
    panels = [d["rect"] for d in page.get_drawings()
              if d.get("fill") == (1.0, 1.0, 1.0)
              and d["rect"].x0 <= label.x0 and d["rect"].x1 >= label.x1
              and d["rect"].y0 <= label.y0 and d["rect"].y1 >= label.y1]
    if not panels:
        raise NoticeError("the power curve chart has no panel")
    return min(panels, key=lambda r: r.width * r.height)


#: The curves the program already carries, as (kph, kW) corners. A notice
#: that draws one of them must draw it exactly so, which is how the chart
#: reading is checked on every import: if Base - Standard comes out anything
#: other than 350 kW to 290 kph, 100 at 340 and nothing at 345, the axes
#: were misread and nothing from that chart is trusted.
BUILT_IN_CURVES = {
    "Base - Standard": ((290.0, 350.0), (340.0, 100.0), (345.0, 0.0)),
    "Base - Overtake": ((337.5, 350.0), (355.0, 0.0)),
    "Alt 1": ((310.0, 250.0), (340.0, 100.0), (345.0, 0.0)),
}


def _curves(page, lines, box) -> dict:
    """The power curves drawn on the chart, read off its vector lines.

    The chart is drawn, not pictured: each curve is a polyline, and the axis
    labels sit on the grid lines, so a corner's speed and power come out
    exact rather than estimated. The curves are drawn in the legend's order.
    """
    drawings = page.get_drawings()
    grid = [d for d in drawings if d.get("color") and min(d["color"]) > 0.7
            and box.contains(d["rect"])]
    xs = sorted({round(p.x, 2) for d in grid for it in d["items"]
                 for p in it[1:3] if hasattr(p, "x")})
    ys = sorted({round(p.y, 2) for d in grid for it in d["items"]
                 for p in it[1:3] if hasattr(p, "y")})
    if len(xs) < 2 or len(ys) < 2:
        raise NoticeError("the chart's grid could not be read")
    numbers = [l for l in lines if re.fullmatch(r"\d+", l.text)
               and box.x0 < l.x0 < box.x1 and box.y0 < l.y0 < box.y1]
    bottom = sorted((l.xc, int(l.text)) for l in numbers if l.y0 > max(ys) - 1)
    left = sorted(((l.y0 + l.y1) / 2, int(l.text)) for l in numbers
                  if l.x1 < min(xs) + 1)
    if len(bottom) < 2 or len(left) < 2:
        raise NoticeError("the chart's axis labels could not be read")
    snap = lambda v, grid_values: min(grid_values, key=lambda g: abs(g - v))
    (x0, k0), (x1, k1) = (snap(bottom[0][0], xs), bottom[0][1]), \
                         (snap(bottom[-1][0], xs), bottom[-1][1])
    (y0, p0), (y1, p1) = (snap(left[0][0], ys), left[0][1]), \
                         (snap(left[-1][0], ys), left[-1][1])
    kph = lambda x: k0 + (x - x0) * (k1 - k0) / (x1 - x0)
    kw = lambda y: p0 + (y - y0) * (p1 - p0) / (y1 - y0)

    legend = sorted((l for l in lines if box.x0 < l.x0 < box.x1
                     and box.y0 < l.y0 < box.y1 and re.search(r"[A-Za-z]", l.text)
                     and not l.text.startswith(("MGUK", "vCar"))),
                    key=lambda l: l.y0)
    paths = [d for d in drawings if d.get("width") and d["width"] > 0.9
             and len(d["items"]) >= 3 and d["rect"].x1 > box.x0]
    if len(paths) != len(legend):
        raise NoticeError("the chart draws %d curves for %d legend names"
                          % (len(paths), len(legend)))
    out = {}
    for name, path in zip((l.text for l in legend), paths):
        pts = [(path["items"][0][1].x, path["items"][0][1].y)] + [
            (it[2].x, it[2].y) for it in path["items"] if it[0] == "l"]
        out[name] = _corners([(kph(x), kw(y)) for x, y in pts])
    return out


def _corners(points) -> tuple:
    """(kph, kW) corners from the first fall in power to the first zero."""
    pts = [(round(v * 2) / 2, max(0.0, round(p * 2) / 2)) for v, p in points]
    pts = sorted(p for p in pts if p[0] > 0)
    top = max(p for _v, p in pts)
    out = []
    for v, p in pts:
        if p >= top - 1.0 and out and out[-1][1] >= top - 1.0:
            out[-1] = (v, p)                     # still flat: move the knee on
            continue
        if out and p <= 0.5 and out[-1][1] <= 0.5:
            break                                # already at nothing
        out.append((v, p))
    # Drop corners that sit on the straight line between their neighbours:
    # within 2 kW of it, which is a tenth of a point on the chart.
    simple = [out[0]]
    for i in range(1, len(out) - 1):
        (va, pa), (vb, pb), (vc, pc) = simple[-1], out[i], out[i + 1]
        on_line = pa + (pc - pa) * (vb - va) / (vc - va) if vc != va else pb
        if abs(pb - on_line) > 2.0:
            simple.append(out[i])
    if len(out) > 1:
        simple.append(out[-1])
    # Published corners are whole kilowatts.
    return tuple((v, float(round(p))) for v, p in simple)


def _key(text: str) -> str:
    """A name reduced to plain letters: "Montréal" is "montreal", not "montral"."""
    import unicodedata
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]+", "", plain.lower())


def _event_words(event: str) -> list[str]:
    """The country or name in "2026 SINGAPORE GRAND PRIX"."""
    m = re.fullmatch(r"\d{4}\s+(.*?)\s+GRAND PRIX.*", event.strip(), re.I)
    return [m.group(1)] if m else []


def _row_y(lines, window, column, reach=30.0):
    for l in lines:
        m = _WINDOW.match(l.text)
        if m and abs(l.xc - column.xc) < reach and int(m.group(2)) == window[0] \
                and int(m.group(3)) == window[1]:
            return l.y0
    return -1e9


#: What the regulations allow, so a misread cannot ship: recharge per lap is
#: at most 9 MJ and a quarter of a MJ apart at least, the reduction rate is
#: one of the two published figures, and every sector lies on the lap.
def check(n: Notice) -> None:
    for name in ("race", "race_overtake", "qualify", "practice", "outlap"):
        value = getattr(n, name)
        if not 30 <= value <= 90:
            raise NoticeError("%s recharge of %.1f MJ is outside 3-9 MJ"
                              % (name, value / UNITS_PER_MJ))
    if n.race_overtake < n.race:
        raise NoticeError("race with Overtake (%.1f MJ) is below the race (%.1f)"
                          % (n.race_overtake / 10, n.race / 10))
    if n.rate_kw_s not in (50, 100):
        raise NoticeError("a reduction rate of %d kW/s is neither 50 nor 100"
                          % n.rate_kw_s)
    if not 1500 <= n.centreline_m <= 8000:
        raise NoticeError("a centreline of %d m is not a Grand Prix circuit"
                          % n.centreline_m)
    if not 0 < n.power_limited_m < n.centreline_m:
        raise NoticeError("the power limited distance %d m is not on the lap"
                          % n.power_limited_m)
    # A sector may run across the line: Austria's exit of T10 is 4100-4400
    # on a 4236 m lap.
    for kind in ("alt_zones", "reset_zones", "speed_zones"):
        for z in getattr(n, kind):
            if not 0 <= z[0] < n.centreline_m and z[0] < z[1] <= n.centreline_m + 500:
                raise NoticeError("%s sector %d-%d is not on a %d m lap"
                                  % (kind, z[0], z[1], n.centreline_m))
    for z in n.speed_zones:
        if not 150 <= z[2] <= 350:
            raise NoticeError("a speed threshold of %d kph is implausible" % z[2])
    for name in (n.curve_standard, n.curve_override, n.curve_practice,
                 n.curve_alternative):
        if name and name not in n.curves and name not in BUILT_IN_CURVES:
            raise NoticeError("power curve '%s' is named but not drawn" % name)
    for name, points in n.curves.items():
        known = BUILT_IN_CURVES.get(name)
        if known is not None and tuple(points) != known:
            raise NoticeError("the chart's %s reads %s, not %s: the axes were "
                              "misread" % (name, points, known))
        if not points or points[-1][1] != 0 or points[0][1] > 350:
            raise NoticeError("curve %s does not run from at most 350 kW down "
                              "to nothing: %s" % (name, points))
        if any(b[1] > a[1] or b[0] < a[0] for a, b in zip(points, points[1:])):
            raise NoticeError("curve %s rises with speed: %s" % (name, points))
