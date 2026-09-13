"""Reader for Assetto Corsa `data.acd` archives.

Container
---------
A flat sequence of records::

    int32 name_len | name bytes | int32 data_len | data_len * int32

Each payload byte is stored widened to a little-endian int32 and offset by one
character of a repeating key: ``plain[i] = stored[i] - key[i % len(key)]``.

Key
---
The key is a *string*: the decimal representations of eight integers derived
from the car folder name, concatenated. Because several of those integers are
negative, the key contains '-' characters and its length varies by car
(25 chars for `abarth500`, 28 for `vrc_formula_alpha_2025_csp`).

Rather than depend on an exact reproduction of the derivation, the key is
recovered from the archive itself: the repeat period is found by coincidence
analysis, each position is solved against a character-frequency model, and the
result is validated by the requirement that every key character be a digit or
'-'. Known-good keys are cached in KNOWN_KEYS to skip that work.
"""

from __future__ import annotations

import collections
import math
import struct
from pathlib import Path

KNOWN_KEYS: dict[str, str] = {
    "vrc_formula_alpha_2025_csp": "210-186-156-39-238-233-3-113",
}

_KEY_CHARS = set("0123456789-")

# Byte frequencies measured across the decrypted contents of a real AC car
# archive (ini/lut/lua), as parts-per-100000. Sharp enough to separate the
# candidate key characters, which differ by as little as 3.
_FREQ = {9: 14, 10: 4864, 13: 4863, 32: 13149, 34: 319, 35: 7, 37: 30, 38: 37, 39: 10, 40: 361, 41: 361, 42: 27, 43: 24, 44: 336, 45: 442, 46: 3392, 47: 173, 48: 5526, 49: 2252, 50: 1762, 51: 1162, 52: 1252, 53: 1644, 54: 1061, 55: 972, 56: 1076, 57: 1021, 58: 309, 59: 107, 60: 13, 61: 2641, 62: 18, 64: 8, 65: 1683, 66: 465, 67: 1289, 68: 998, 69: 2355, 70: 559, 71: 371, 72: 540, 73: 1582, 74: 49, 75: 360, 76: 1519, 77: 1417, 78: 1278, 79: 1344, 80: 1478, 81: 10, 82: 1475, 83: 2124, 84: 2167, 85: 819, 86: 237, 87: 403, 88: 355, 89: 355, 90: 50, 91: 253, 92: 43, 93: 253, 94: 2, 95: 4703, 97: 1397, 98: 243, 99: 825, 100: 517, 101: 2478, 102: 679, 103: 444, 104: 369, 105: 1438, 106: 10, 107: 149, 108: 1156, 109: 438, 110: 1336, 111: 980, 112: 480, 113: 80, 114: 1321, 115: 1368, 116: 1766, 117: 695, 118: 149, 119: 130, 120: 76, 121: 267, 122: 17, 123: 37, 124: 1344, 125: 37, 126: 2}


def _read_records(raw: bytes):
    pos = 0
    while pos < len(raw):
        (name_len,) = struct.unpack_from("<i", raw, pos)
        if not 0 < name_len < 256:
            raise ValueError(f"implausible name length {name_len} at offset {pos}")
        name = raw[pos + 4 : pos + 4 + name_len].decode("utf-8")
        dpos = pos + 4 + name_len
        (data_len,) = struct.unpack_from("<i", raw, dpos)
        start = dpos + 4
        yield name, [struct.unpack_from("<i", raw, start + i * 4)[0] for i in range(data_len)]
        pos = start + data_len * 4


def _detect_period(stored: list[int], max_period: int = 96) -> int:
    """Pick the key length from the self-coincidence spectrum.

    Multiples of the true period score about as well as the period itself, so
    take the shortest period within 90% of the strongest peak.
    """
    rates: dict[int, float] = {}
    for period in range(1, max_period + 1):
        n = len(stored) - period
        if n < 512:
            break
        rates[period] = sum(1 for i in range(n) if stored[i] == stored[i + period]) / n
    if not rates:
        return 8
    peak = max(rates.values())
    return min(p for p, r in rates.items() if r >= peak * 0.9)


def _solve_key(entries: dict[str, list[int]]) -> str:
    text = [v for name, v in entries.items() if name.endswith((".lua", ".ini"))]
    if not text:
        text = list(entries.values())
    longest = max(text, key=len)
    period = _detect_period(longest)

    total = sum(_FREQ.values())
    floor = math.log(0.2 / total)
    logp = [math.log(_FREQ[b] / total) if b in _FREQ else floor for b in range(256)]

    def solve(p: int) -> str:
        buckets: list[collections.Counter] = [collections.Counter() for _ in range(p)]
        for stored in text:
            for i, c in enumerate(stored):
                buckets[i % p][c & 0xFF] += 1
        candidates = [ord(ch) for ch in sorted(_KEY_CHARS)]
        out = ""
        for bucket in buckets:
            _, best = max(
                (sum(n * logp[(c - k) & 0xFF] for c, n in bucket.items()), k)
                for k in candidates
            )
            out += chr(best)
        return out

    # A detected period is often a multiple of the true one. Collapsing to the
    # shortest repeating unit doubles or triples the data behind each position.
    full = solve(period)
    for divisor in range(1, period + 1):
        if period % divisor == 0 and full[:divisor] * (period // divisor) == full:
            return full[:divisor] if divisor == period else solve(divisor)
    return full


def unpack(acd_path: str | Path, car_folder_name: str | None = None) -> dict[str, bytes]:
    """Return {entry name: decrypted bytes} for one data.acd."""
    acd_path = Path(acd_path)
    if car_folder_name is None:
        car_folder_name = acd_path.parent.name

    entries = dict(_read_records(acd_path.read_bytes()))
    key = KNOWN_KEYS.get(car_folder_name) or _solve_key(entries)

    out: dict[str, bytes] = {}
    for name, stored in entries.items():
        out[name] = bytes((c - ord(key[i % len(key)])) & 0xFF for i, c in enumerate(stored))
    return out
