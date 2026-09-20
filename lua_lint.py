"""Catch the Lua mistakes a Python-side parser does not.

    python lua_lint.py logger/fa26_baseline/fa26_baseline.lua

`luaparser` is installed for the checks and it accepted a file Assetto Corsa
refused outright:

    Failed to parse: fa26_baseline.lua:206: unfinished string near '"'

The file had a raw newline inside a double-quoted string, because the shell
that wrote it turned a `\\n` escape into an actual line break. Real Lua forbids
that in a short string; the Python parser did not care. So the logger shipped,
installed cleanly, failed to load in the game, and four laps were driven with
nothing recording them.

This is the lexer that would have caught it: it walks the file the way Lua's
own scanner does -- long comments, line comments, long strings, short strings,
escapes -- and reports the things that stop a chunk loading. It is deliberately
small and deliberately not a parser. `luaparser` still runs alongside it and
catches what a lexer cannot.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _long_bracket(text: str, i: int) -> int | None:
    """Length of a `[==[` opener at `i`, or None if there is not one."""
    if text[i] != "[":
        return None
    j = i + 1
    while j < len(text) and text[j] == "=":
        j += 1
    if j < len(text) and text[j] == "[":
        return j + 1 - i
    return None


def problems(source: str) -> list[str]:
    """Everything wrong with this chunk that would stop Lua loading it."""
    found: list[str] = []
    i, line = 0, 1
    n = len(source)
    while i < n:
        ch = source[i]

        if ch == "\n":
            line += 1
            i += 1
            continue

        # comments, long and short
        if source.startswith("--", i):
            opener = _long_bracket(source, i + 2)
            if opener is not None:
                closer = "]" + "=" * (opener - 2) + "]"
                end = source.find(closer, i + 2 + opener)
                if end < 0:
                    found.append("line %d: unterminated long comment" % line)
                    break
                line += source.count("\n", i, end)
                i = end + len(closer)
                continue
            end = source.find("\n", i)
            i = n if end < 0 else end
            continue

        # long strings
        opener = _long_bracket(source, i)
        if opener is not None:
            closer = "]" + "=" * (opener - 2) + "]"
            end = source.find(closer, i + opener)
            if end < 0:
                found.append("line %d: unterminated long string" % line)
                break
            line += source.count("\n", i, end)
            i = end + len(closer)
            continue

        # short strings: the ones that cannot hold a raw newline
        if ch in "\"'":
            started = line
            j = i + 1
            while j < n:
                c = source[j]
                if c == "\\":
                    if j + 1 < n and source[j + 1] == "\n":
                        line += 1          # an escaped line break is allowed
                    j += 2
                    continue
                if c == "\n":
                    found.append(
                        "line %d: unfinished string -- a short string cannot "
                        "contain a raw line break, which is what a mangled "
                        "\\\\n escape leaves behind" % started)
                    break
                if c == ch:
                    break
                j += 1
            else:
                found.append("line %d: unterminated string at end of file" % started)
                break
            if j < n and source[j] == "\n":
                line += 1
            i = j + 1
            continue

        i += 1

    return found


def check(path) -> list[str]:
    return problems(Path(path).read_text(encoding="utf-8", errors="replace"))


def main(argv=None) -> int:
    paths = (argv or sys.argv[1:]) or ["logger/fa26_baseline/fa26_baseline.lua"]
    bad = 0
    for name in paths:
        issues = check(name)
        if issues:
            bad += 1
            print("%s:" % name)
            for issue in issues:
                print("  " + issue)
        else:
            print("%s: clean" % name)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
