"""Draw the application icon, with no image library.

    python make_icon.py

Writes `ui/app.ico`: the three deployment bars from the page's own mark, on
the same dark ground, at the five sizes Windows asks for. A .ico is a small
header plus one bitmap per size, and a 32-bit BGRA bitmap is just pixels, so
this needs nothing beyond the standard library -- one less thing between a
fresh checkout and a build that works.
"""

from __future__ import annotations

import struct
from pathlib import Path

OUT = Path(__file__).resolve().parent / "ui" / "app.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)

#: Ground, then the three strategy colours, as the page defines them.
GROUND = (0x0B, 0x0E, 0x14)
BARS = (
    # left edge, width, top, colour -- all as a fraction of the icon
    (0.17, 0.16, 0.53, (0xFF, 0x5C, 0x7A)),
    (0.42, 0.16, 0.28, (0x5E, 0xC8, 0xFF)),
    (0.67, 0.16, 0.44, (0x54, 0xD6, 0xA0)),
)
BOTTOM = 0.81
RADIUS = 0.22          # corner radius of the ground, as a fraction


def inside_rounded(x: float, y: float, size: float, radius: float) -> bool:
    """Whether a point falls inside a rounded square."""
    for cx, cy in ((radius, radius), (size - radius, radius),
                   (radius, size - radius), (size - radius, size - radius)):
        near_x = x < radius if cx == radius else x > size - radius
        near_y = y < radius if cy == radius else y > size - radius
        if near_x and near_y:
            return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2
    return True


def coverage(px: int, py: int, size: int) -> tuple:
    """Colour and alpha for one pixel, sampled 4x4 to soften the edges."""
    hits = 0
    colour_hits = {}
    for sy in range(4):
        for sx in range(4):
            x = px + (sx + 0.5) / 4
            y = py + (sy + 0.5) / 4
            if not inside_rounded(x, y, size, RADIUS * size):
                continue
            hits += 1
            pick = GROUND
            for left, width, top, rgb in BARS:
                if (left * size <= x <= (left + width) * size
                        and top * size <= y <= BOTTOM * size):
                    pick = rgb
                    break
            colour_hits[pick] = colour_hits.get(pick, 0) + 1
    if not hits:
        return (0, 0, 0, 0)
    # Blend whatever the samples landed on, so bar edges are not stepped.
    r = g = b = 0
    for (cr, cg, cb), n in colour_hits.items():
        r += cr * n
        g += cg * n
        b += cb * n
    total = sum(colour_hits.values())
    return (r // total, g // total, b // total, hits * 255 // 16)


def bitmap(size: int) -> bytes:
    """One image, as the BMP-with-alpha an .ico entry holds."""
    rows = []
    for py in range(size - 1, -1, -1):        # BMP rows run bottom-up
        row = bytearray()
        for px in range(size):
            r, g, b, a = coverage(px, py, size)
            row += bytes((b, g, r, a))        # BGRA
        rows.append(bytes(row))
    pixels = b"".join(rows)
    # BITMAPINFOHEADER: height is doubled because the format expects a mask
    # below the colour data, even when the alpha channel makes it redundant.
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         len(pixels), 0, 0, 0, 0)
    mask = b"\x00" * (((size + 31) // 32) * 4 * size)
    return header + pixels + mask


def main() -> int:
    images = [(size, bitmap(size)) for size in SIZES]
    offset = 6 + 16 * len(images)
    directory = b""
    for size, blob in images:
        directory += struct.pack("<BBBBHHII",
                                 size if size < 256 else 0,
                                 size if size < 256 else 0,
                                 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + directory
                    + b"".join(blob for _, blob in images))
    print("%s  (%d sizes, %.1f KB)"
          % (OUT.name, len(images), OUT.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
