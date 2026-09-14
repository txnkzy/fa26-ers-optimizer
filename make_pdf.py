"""Write the installation sheet as a PDF, with no PDF library.

    python make_pdf.py

Produces `docs/Installation.pdf`, which `build_exe.py` puts in the zip beside
the executable. A PDF opens the same way on every Windows machine without
anything installed, and it is the one file someone can read *before* deciding
whether to run an unsigned executable.

Hand-written for the same reason the icon is: a PDF page is a stream of
drawing operators, and the fourteen standard fonts need no embedding, so this
needs nothing beyond the standard library -- one less thing between a fresh
checkout and a build that works.

The version is read from `fa26_app.py`, so the sheet cannot end up documenting
a build it was not made alongside.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "Installation.pdf"

#: A4. Chosen over Letter because it prints acceptably on Letter paper and
#: most of this car's drivers are not in North America.
PAGE_W, PAGE_H = 595.28, 841.89
LEFT, RIGHT = 64.0, 64.0
TOP, FLOOR = 92.0, 72.0
COLUMN = PAGE_W - LEFT - RIGHT

#: The page's own palette, so the sheet and the app look related.
GROUND = (0x0B / 255, 0x0E / 255, 0x14 / 255)
INK = (0.13, 0.14, 0.16)
SOFT = (0.42, 0.44, 0.48)
PAPER = (1.0, 1.0, 1.0)
BARS = ((0xFF / 255, 0x5C / 255, 0x7A / 255),
        (0x5E / 255, 0xC8 / 255, 0xFF / 255),
        (0x54 / 255, 0xD6 / 255, 0xA0 / 255))
ACCENT = BARS[1]

#: Advance widths for the standard fonts, characters 32 to 126. Without these
#: every line has to be guessed at, and wraps ragged or overruns the margin.
_HELV = """278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278
556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 1015 667 667
722 722 667 611 778 722 278 500 667 556 833 722 778 667 778 722 667 611 722
667 944 667 667 611 278 278 278 469 556 333 556 556 500 556 556 278 556 556
222 222 500 222 833 556 556 556 556 333 500 278 556 500 722 500 500 500 334
260 334 584"""
_BOLD = """278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278
556 556 556 556 556 556 556 556 556 556 333 333 584 584 584 611 975 722 722
722 722 667 611 778 722 278 556 722 611 833 722 778 667 778 722 667 611 722
667 944 667 667 611 333 278 333 584 556 333 556 611 556 611 556 333 611 611
278 278 556 278 889 611 611 611 611 389 556 333 611 556 778 556 556 500 389
280 389 584"""

WIDTHS = {
    "F1": [int(n) for n in _HELV.split()],
    "F2": [int(n) for n in _BOLD.split()],
    "F3": [600] * 95,
}


def width(text: str, font: str, size: float) -> float:
    table = WIDTHS[font]
    total = 0
    for ch in text:
        index = ord(ch) - 32
        total += table[index] if 0 <= index < 95 else 556
    return total * size / 1000.0


def wrap(text: str, font: str, size: float, room: float) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = word if not line else line + " " + word
        if width(trial, font, size) <= room or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def escape(text: str) -> str:
    for a, b in (("\\", "\\\\"), ("(", "\\("), (")", "\\)")):
        text = text.replace(a, b)
    return text


class Doc:
    """A page cursor and the drawing operators, nothing more."""

    def __init__(self, version: str) -> None:
        self.version = version
        self.pages: list[list[str]] = []
        self.buf: list[str] = []
        self.y = 0.0
        self.page()

    # ------------------------------------------------------------ mechanics
    def page(self) -> None:
        self.buf = []
        self.pages.append(self.buf)
        self.y = PAGE_H - TOP

    def room(self, needed: float) -> None:
        """Start a new page rather than let a block fall off the bottom."""
        if self.y - needed < FLOOR:
            self.page()
            self.running_head()

    def fill(self, colour: tuple) -> None:
        self.buf.append("%.3f %.3f %.3f rg" % colour)

    def box(self, x: float, y: float, w: float, h: float,
            colour: tuple) -> None:
        self.fill(colour)
        self.buf.append("%.2f %.2f %.2f %.2f re f" % (x, y, w, h))

    def draw(self, x: float, y: float, text: str, font: str = "F1",
             size: float = 10.0, colour: tuple = INK) -> None:
        self.fill(colour)
        self.buf.append("BT /%s %.2f Tf %.2f %.2f Td (%s) Tj ET"
                        % (font, size, x, y, escape(text)))

    # -------------------------------------------------------------- blocks
    def masthead(self) -> None:
        """The one piece of identity on the sheet: the app's own mark."""
        height = 132.0
        self.box(0, PAGE_H - height, PAGE_W, height, GROUND)
        x = LEFT
        for top, colour in zip((16.0, 30.0, 22.0), BARS):
            self.box(x, PAGE_H - 66, 11, top, colour)
            x += 17
        self.draw(LEFT, PAGE_H - 90, "FA26 ERS DEPLOYMENT OPTIMIZER",
                  "F2", 17, PAPER)
        self.draw(LEFT, PAGE_H - 110,
                  "Installing it  \u00b7  version %s" % self.version,
                  "F1", 10.5, (0.62, 0.66, 0.72))
        self.y = PAGE_H - height - 34

    def running_head(self) -> None:
        self.draw(LEFT, PAGE_H - 52, "FA26 ERS Deployment Optimizer",
                  "F2", 8.5, SOFT)
        self.box(LEFT, PAGE_H - 60, COLUMN, 0.6, (0.85, 0.86, 0.88))
        self.y = PAGE_H - 88

    def heading(self, text: str) -> None:
        self.room(52)
        self.y -= 10
        self.box(LEFT, self.y - 5, 18, 2.2, ACCENT)
        self.draw(LEFT + 26, self.y - 9, text.upper(), "F2", 10.5, INK)
        self.y -= 30

    def para(self, text: str, colour: tuple = INK, indent: float = 0.0,
             size: float = 10.0) -> None:
        lines = wrap(text, "F1", size, COLUMN - indent)
        self.room(len(lines) * (size + 4.2))
        for line in lines:
            self.draw(LEFT + indent, self.y, line, "F1", size, colour)
            self.y -= size + 4.2
        self.y -= 4

    def step(self, number: int, title: str, body: str) -> None:
        self.room(62)
        self.draw(LEFT, self.y, "%d" % number, "F2", 15, ACCENT)
        self.draw(LEFT + 22, self.y, title, "F2", 11, INK)
        self.y -= 17
        self.para(body, INK, 22)

    def bullet(self, text: str) -> None:
        lines = wrap(text, "F1", 10, COLUMN - 22)
        self.room(len(lines) * 14.2 + 2)
        self.box(LEFT + 5, self.y + 3.2, 3, 3, SOFT)
        for line in lines:
            self.draw(LEFT + 22, self.y, line, "F1", 10, INK)
            self.y -= 14.2
        self.y -= 2

    def mono(self, text: str, indent: float = 22.0) -> None:
        self.room(22)
        self.draw(LEFT + indent, self.y, text, "F3", 9, (0.25, 0.30, 0.38))
        self.y -= 18

    def note(self, text: str) -> None:
        """A rail down the side, for the thing people actually get wrong."""
        lines = wrap(text, "F1", 10, COLUMN - 40)
        height = len(lines) * 14.2 + 10
        self.room(height + 8)
        self.box(LEFT, self.y - height + 18, 2.4, height - 4, BARS[0])
        for line in lines:
            self.draw(LEFT + 16, self.y, line, "F1", 10, INK)
            self.y -= 14.2
        self.y -= 8

    # --------------------------------------------------------------- output
    def finish(self) -> bytes:
        total = len(self.pages)
        for index, buf in enumerate(self.pages, start=1):
            self.buf = buf
            label = "%d of %d" % (index, total)
            self.draw(LEFT, 50,
                      "FA26 ERS Deployment Optimizer  v%s" % self.version,
                      "F1", 8, SOFT)
            self.draw(PAGE_W - RIGHT - width(label, "F1", 8), 50, label,
                      "F1", 8, SOFT)
        return assemble(self.pages)


def assemble(pages: list[list[str]]) -> bytes:
    """Objects, offsets and an xref table -- the whole of the file format."""
    objects: list[bytes] = []

    def add(body) -> int:
        objects.append(body.encode("latin-1")
                       if isinstance(body, str) else body)
        return len(objects)

    fonts = {}
    for key, base in (("F1", "Helvetica"), ("F2", "Helvetica-Bold"),
                      ("F3", "Courier")):
        fonts[key] = add("<< /Type /Font /Subtype /Type1 /BaseFont /%s "
                         "/Encoding /WinAnsiEncoding >>" % base)

    pages_id = add("")                      # reserved; filled in below
    page_ids = []
    for buf in pages:
        stream = "\n".join(buf).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n%s\nendstream"
                      % (len(stream), stream))
        page_ids.append(add(
            "<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] "
            "/Resources << /Font << %s >> >> /Contents %d 0 R >>"
            % (pages_id, PAGE_W, PAGE_H,
               " ".join("/%s %d 0 R" % (k, v) for k, v in fonts.items()),
               content)))
    objects[pages_id - 1] = (
        "<< /Type /Pages /Count %d /Kids [%s] >>"
        % (len(page_ids), " ".join("%d 0 R" % i for i in page_ids))
    ).encode("latin-1")
    catalog = add("<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += (b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, catalog, start))
    return bytes(out)


def version() -> str:
    text = (ROOT / "fa26_app.py").read_text(encoding="utf-8")
    found = re.search(r'^VERSION = "([^"]+)"', text, re.M)
    return found.group(1) if found else "unknown"


def build() -> Doc:
    doc = Doc(version())
    doc.masthead()

    doc.para("This tool works out where to spend the battery around a "
             "circuit for the VRC Formula Alpha 2026, from laps you have "
             "actually driven, and writes the result into your setup. This "
             "sheet covers getting it installed. Everything after that is in "
             "READ ME FIRST.txt, in the same folder.")

    doc.heading("Before you start")
    doc.bullet("Assetto Corsa, with Custom Shaders Patch.")
    doc.bullet("The VRC Formula Alpha 2026. The tool only works with that "
               "car, and it reads the physics out of your own copy -- no "
               "part of the car is included here.")
    doc.bullet("Windows. Nothing else: no Python, no installer, no account, "
               "and no internet connection at any point.")

    doc.heading("Installing")
    doc.step(1, "Unpack the zip somewhere you will find again",
             "Put the files in a folder of your own. Windows will happily "
             "run the executable from inside the zip viewer and then lose "
             "track of where it went.")
    doc.step(2, "Run FA26 Optimizer.exe",
             "Windows shows a blue box reading \"Windows protected your "
             "PC\". Click More info, then Run anyway. That warning means the "
             "file is not code-signed, which is true -- signing is an annual "
             "fee and this is free. Some antivirus flags it for the same "
             "reason. If that is not good enough on trust, the source is "
             "published and you can build it yourself. The first start takes "
             "a few seconds while it unpacks.")
    doc.step(3, "Let it find Assetto Corsa",
             "The first screen looks the game up in Steam's own library list "
             "and usually finds it. If it does not, paste the folder in "
             "yourself -- the one with content and apps inside it:")
    doc.mono("C:\\Program Files (x86)\\Steam\\steamapps\\common\\assettocorsa",
             42)
    doc.step(4, "Press Install the logger",
             "This puts a small app into Assetto Corsa that records your "
             "laps: two files in apps\\lua, and it touches nothing else. The "
             "optimizer cannot do anything without it, because the whole "
             "method is built on laps you have driven rather than on a "
             "generic model of the car.")
    doc.step(5, "Enable it in game",
             "Get in the car, open the apps bar down the right-hand edge of "
             "the screen, and click FA26 Baseline Logger. Assetto Corsa "
             "remembers it, so this is a one-time step.")
    doc.step(6, "Drive a lap and check it arrives",
             "Cross the line, then alt-tab to the optimizer. The lap should "
             "be listed there. If it is, you are installed, and the rest is "
             "in the read-me.")

    doc.heading("The part everyone gets wrong")
    doc.note("Assetto Corsa only reads a setup file when the car loads. A "
             "map saved while you are sitting on track never reaches the "
             "car, and reloading the setup in the pit menu is not enough "
             "either -- you have to restart the session. If all three "
             "strategies feel identical on track, this is why.")

    doc.heading("If something goes wrong")
    doc.bullet("No laps appear: the logger is not enabled in the apps bar, "
               "or you are not in the Formula Alpha 2026.")
    doc.bullet("Nothing happens when you double-click: look for error.log in "
               "%LOCALAPPDATA%\\FA26 Optimizer, and send what it says.")
    doc.bullet("Anything else: press Copy diagnostics in the app, next to "
               "Save to a setup, and paste the result wherever you are "
               "asking. It carries the version, where your game was found, "
               "what the optimizer decided and every step it took -- which "
               "is the difference between a question that can be answered "
               "and one that cannot.")

    doc.heading("What it puts on your machine")
    doc.bullet("%LOCALAPPDATA%\\FA26 Optimizer -- its settings, and a cached "
               "copy of the car physics unpacked from your own install.")
    doc.bullet("apps\\lua\\fa26_baseline inside Assetto Corsa -- the logger, "
               "and only when you ask for it.")
    doc.bullet("Documents\\Assetto Corsa\\fa26_baseline -- the lap files the "
               "logger records.")
    doc.bullet("The setups you pick -- deployment maps 1 to 3 only, and the "
               "old file is copied to .bak first.")
    doc.para("Nothing else is written, and nothing is sent anywhere. To "
             "remove it: delete the executable and those three folders. Your "
             "setups keep their backups.")
    return doc


def verify(doc: Doc) -> list[str]:
    """Check nothing was laid out off the page or past the margin.

    Every position here is computed rather than typed, so a change to the
    wording cannot break the file -- but a change to the *layout* silently
    can, and a PDF renders a line that has run off the edge without
    complaining. The build calls this, so a mangled sheet stops it.
    """
    pattern = re.compile(
        r"BT /(F\d) ([\d.]+) Tf ([-\d.]+) ([-\d.]+) Td \((.*)\) Tj ET$")
    problems = []
    for number, buf in enumerate(doc.pages, start=1):
        for op in buf:
            found = pattern.match(op)
            if not found:
                continue
            font, size = found.group(1), float(found.group(2))
            x, y = float(found.group(3)), float(found.group(4))
            text = (found.group(5).replace("\\(", "(").replace("\\)", ")")
                    .replace("\\\\", "\\"))
            where = "page %d: %r" % (number, text[:44])
            if x + width(text, font, size) > PAGE_W - RIGHT + 0.5:
                problems.append("past the right margin, " + where)
            if not 44 <= y <= PAGE_H - 30:
                problems.append("off the page, " + where)
    return problems


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = build()
    data = doc.finish()
    problems = verify(doc)
    if problems:
        for problem in problems:
            print("  " + problem)
        return 1
    OUT.write_bytes(data)
    print("%s\n  %.1f KB, %d pages" % (OUT, len(data) / 1024, len(doc.pages)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
