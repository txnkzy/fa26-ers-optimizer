"""Bring each Grand Prix's FIA Power Unit Information notice into the circuit table.

    python tools/fia_watch.py              # check the FIA's documents page
    python tools/fia_watch.py --pdf FILE   # import a notice already downloaded

Exit status: 0 nothing new or imported cleanly, 1 imported with something
left for a person to check, 2 a notice was refused, 3 the FIA site could not
be reached.

Run every few hours by GitHub (.github/workflows/fia-notices.yml), so a new
notice reaches every player's copy of the program within hours of the FIA
publishing it, with no new version and no one in the loop. Each notice is
read by `core/pu_notice.py`, which refuses anything it cannot place or that
falls outside what the regulations allow; a refusal leaves the table as it
was and fails the run, and GitHub tells the owner. A notice is imported once:
`data/fia_notices.json` records each one by address.

The FIA's documents page lists the current event's documents, so the notice
is picked up while that event is current. Older events are not listed there.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "core"), str(ROOT / "cars")]

import pu_notice
import recharge

TABLE = ROOT / "data" / "fa26_recharge.json"
RECORD = ROOT / "data" / "fia_notices.json"
LIST_URL = ("https://www.fia.com/documents/championships/"
            "fia-formula-one-world-championship-14")
BASE = "https://www.fia.com"
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FA26-ERS-Optimizer"
NOTICE_LINK = re.compile(
    r'(/(?:sites/default|system)/files/decision-document/'
    r'[^"\'\s>]*power_unit_information[^"\'\s>]*\.pdf)', re.I)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def notice_links(html: str) -> list[str]:
    return sorted({BASE + m for m in NOTICE_LINK.findall(html)})


def merge(key: str, entry: dict, table: dict) -> dict:
    """The table with this circuit's entry replaced by the notice's."""
    old = table.get(key, {})
    aliases = sorted(set(old.get("aliases") or ()) | set(entry.get("aliases") or ()))
    new = dict(entry)
    if aliases:
        new["aliases"] = aliases
    out = {k: v for k, v in table.items() if k != recharge.META}
    out[key] = new
    return dict(sorted(out.items()))


def import_notice(pdf: bytes, table: dict) -> tuple[str, dict, pu_notice.Notice]:
    notice = pu_notice.parse(pdf)
    number = int(re.sub(r"\D", "", notice.round) or 0) or None
    entry = notice.entry(number)
    if not recharge.valid_entry(entry):
        raise pu_notice.NoticeError("the entry for %s does not pass the table's "
                                    "own check" % notice.venue)
    key = _circuit_key(notice, table)
    return key, merge(key, entry, table), notice


def _circuit_key(notice, table) -> str:
    """The table's name for the notice's circuit, or a new one from its venue."""
    for name in [notice.venue] + pu_notice._event_words(notice.event):
        key = recharge.normalise(name)
        if key in table:
            return key
    return pu_notice._key(notice.venue)


def describe(key: str, old: dict, new: dict, notice) -> str:
    lines = ["%s (%s, %s): %s" % (notice.event.title(), notice.round,
                                  notice.venue, "updated" if old else "added")]
    for field in sorted(set(old) | set(new)):
        if old.get(field) != new.get(field) and field != "aliases":
            lines.append("  %s: %s -> %s" % (field, old.get(field), new.get(field)))
    return "\n".join(lines)


def save(table: dict, record: dict) -> None:
    # Stamped, so a copy of the program built later is never overruled by an
    # older download; see recharge._raw_table.
    stamp = {"updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    body = {recharge.META: stamp, **{k: v for k, v in table.items()
                                     if k != recharge.META}}
    TABLE.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    RECORD.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pdf", help="import this notice instead of checking the FIA")
    args = ap.parse_args(argv)

    table = json.loads(TABLE.read_text(encoding="utf-8"))
    record = json.loads(RECORD.read_text(encoding="utf-8")) if RECORD.exists() else {}
    today = dt.date.today().isoformat()

    if args.pdf:
        sources = [(Path(args.pdf).name, Path(args.pdf).read_bytes())]
    else:
        try:
            links = [u for u in notice_links(
                fetch(LIST_URL).decode("utf-8", "replace")) if u not in record]
            if not links:
                print("No new Power Unit Information notice.")
                return 0
            sources = [(u, fetch(u)) for u in links]
        except Exception as exc:              # the FIA site, not a notice
            print("Could not reach the FIA's documents: %s" % exc, file=sys.stderr)
            return 3

    changed, failed, warnings = [], [], []
    for where, pdf in sources:
        try:
            key, new_table, notice = import_notice(pdf, table)
        except pu_notice.NoticeError as exc:
            failed.append("%s: %s" % (where, exc))
            # Recorded, so the next run does not refuse it again and again.
            # Import it by hand with --pdf once it has been looked at.
            record[where] = {"refused": str(exc), "on": today}
            continue
        print(describe(key, table.get(key, {}), new_table[key], notice))
        for mark in notice.skipped:
            warnings.append("%s: left out sector %s -- it carries a mark the "
                            "notice does not explain" % (notice.event.title(), mark))
        table = new_table
        record[where] = {"imported": today, "circuit": key,
                         "event": notice.event, "round": notice.round}
        changed.append(key)

    if changed:
        save(table, record)
    elif failed:
        RECORD.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    for f in failed:
        print("REFUSED " + f, file=sys.stderr)
    for w_ in warnings:
        print("CHECK " + w_, file=sys.stderr)
    return 2 if failed else (1 if warnings else 0)


if __name__ == "__main__":
    raise SystemExit(main())
