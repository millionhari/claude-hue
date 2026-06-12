#!/usr/bin/env python3
"""Generate the Claude Hue app icon as a PNG — pure stdlib (zlib + struct).

Draws the dashboard's "Iris orb" on a charcoal squircle: a glowing green
lamp with an amber underglow. Usage: gen_icon.py <out.png> [size]
"""

import struct
import sys
import zlib


def write_png(path, w, h, rows):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "icon.png"
    S = int(sys.argv[2]) if len(sys.argv) > 2 else 1024

    margin = S * 0.06
    half = (S - 2 * margin) / 2
    cx = cy = S / 2
    n = 5.0                               # squircle exponent
    bg_top, bg_bot = (27, 25, 22), (15, 14, 12)
    orb_c, orb_cy, orb_r = (61, 220, 142), S * 0.46, S * 0.27
    amber, amber_cy, amber_r = (232, 163, 61), S * 0.78, S * 0.16

    rows = []
    for y in range(S):
        row = bytearray()
        for x in range(S):
            dx, dy = abs(x - cx) / half, abs(y - cy) / half
            sq = dx ** n + dy ** n
            if sq > 1.06:
                row += b"\x00\x00\x00\x00"
                continue
            a = 255 if sq <= 1.0 else int(255 * (1.06 - sq) / 0.06)
            r, g, b = lerp(bg_top, bg_bot, y / S)

            ad = (((x - cx) ** 2 + (y - amber_cy) ** 2) ** .5) / amber_r
            if ad < 2.8:
                t = max(0.0, 1 - ad / 2.8) ** 2 * 0.55
                r, g, b = lerp((r, g, b), amber, t)

            od = (((x - cx) ** 2 + (y - orb_cy) ** 2) ** .5) / orb_r
            if od < 1.0:
                core = max(0.0, 1 - od) ** 0.7
                r, g, b = lerp(orb_c, (235, 255, 240), core * 0.75)
            elif od < 2.4:
                t = max(0.0, 1 - (od - 1) / 1.4) ** 2 * 0.6
                r, g, b = lerp((r, g, b), orb_c, t)

            row += bytes((r, g, b, a))
        rows.append(row)

    write_png(out, S, S, rows)
    print(f"wrote {out} ({S}x{S})")


if __name__ == "__main__":
    main()
