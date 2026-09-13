"""Reading and writing Assetto Corsa setup files by declared item ID.

A car's `setup.ini` (inside data.acd) declares every custom setup item with an
`ID=`, and the saved setup stores values in `[CUSTOM_SCRIPT_ITEM_<n>]` sections.
The indices are not sequential and differ between cars -- the FA25 uses small
numbers, the FA26 large hashes -- so nothing here assumes anything about them
beyond the declaration.

Writing rewrites only the VALUE lines of the requested items and leaves every
other line of the file byte for byte, so car settings are never disturbed.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

SECTION_RE = re.compile(r"^\[CUSTOM_SCRIPT_ITEM_(\d+)\]\s*$")
VALUE_RE = re.compile(r"^VALUE\s*=", re.IGNORECASE)


def item_index_map(car_setup_ini_text: str) -> dict[str, int]:
    """Map declared item IDs to their CUSTOM_SCRIPT_ITEM index."""
    mapping: dict[str, int] = {}
    index = None
    for line in car_setup_ini_text.replace("\r", "").split("\n"):
        stripped = line.strip()
        match = SECTION_RE.match(stripped)
        if match:
            index = int(match.group(1))
            continue
        if index is not None and stripped.startswith("ID="):
            mapping[stripped[3:].strip()] = index
    return mapping


def item_attributes(car_setup_ini_text: str) -> dict[str, dict[str, str]]:
    """Every declared attribute (NAME, MIN, MAX, STEP, ...) keyed by item ID."""
    out: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in car_setup_ini_text.replace("\r", "").split("\n"):
        stripped = line.strip()
        if SECTION_RE.match(stripped):
            current = {}
            continue
        if "=" in stripped:
            key, value = stripped.split("=", 1)
            current[key.strip()] = value.strip()
            if key.strip() == "ID":
                out[value.strip()] = current
    return out


def read_values(setup_text: str) -> dict[int, int]:
    """Current value of every item present in a saved setup."""
    values: dict[int, int] = {}
    index = None
    for line in setup_text.replace("\r", "").split("\n"):
        stripped = line.strip()
        match = SECTION_RE.match(stripped)
        if match:
            index = int(match.group(1))
            continue
        if index is not None and VALUE_RE.match(stripped):
            try:
                values[index] = int(float(stripped.split("=", 1)[1]))
            except ValueError:
                pass
            index = None
    return values


def write_values(setup_path: str | Path, updates: dict[int, int],
                 backup: bool = True) -> Path | None:
    """Set the given items in place, leaving the rest of the file untouched.

    Returns the backup path, or None when a backup already existed. Raises if
    any requested item is absent, rather than silently writing a partial map.
    """
    setup_path = Path(setup_path)
    original = setup_path.read_bytes()
    text = original.decode("utf-8", "replace")

    missing = set(updates) - set(read_values(text))
    if missing:
        raise ValueError(
            f"setup file is missing {len(missing)} of the {len(updates)} items "
            f"to write; is it the right car?"
        )

    backup_path = None
    if backup:
        backup_path = setup_path.with_suffix(setup_path.suffix + ".bak")
        if backup_path.exists():
            backup_path = None
        else:
            shutil.copy2(setup_path, backup_path)

    newline = b"\r\n" if b"\r\n" in original else b"\n"
    lines = original.split(newline)

    index = None
    written = 0
    for i, raw in enumerate(lines):
        stripped = raw.decode("utf-8", "replace").strip()
        match = SECTION_RE.match(stripped)
        if match:
            index = int(match.group(1))
            continue
        if index is not None and VALUE_RE.match(stripped):
            if index in updates:
                lines[i] = f"VALUE={updates[index]}".encode("utf-8")
                written += 1
            index = None

    if written != len(updates):
        raise ValueError(f"expected to update {len(updates)} values, updated {written}")

    setup_path.write_bytes(newline.join(lines))
    return backup_path
