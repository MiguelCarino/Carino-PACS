"""Generate the tray + app PNG icons with the standard library only (no Pillow).

A simple concentric "aperture/clock" glyph in Carino gold on transparency:
gold disc, dark ring, gold hub. Run:  python make_icon.py
"""
import math
import struct
import zlib

GOLD = (0xEA, 0xB3, 0x08)
DARK = (0x0A, 0x0A, 0x0A)


def _pixel(x, y, size):
    cx = cy = (size - 1) / 2
    d = math.hypot(x - cx, y - cy)
    R = size * 0.46      # outer radius
    ring_o = size * 0.34
    ring_i = size * 0.22
    hub = size * 0.13
    if d <= hub:
        return GOLD + (255,)
    if ring_i <= d <= ring_o:
        return DARK + (255,)
    if d <= R:
        return GOLD + (255,)
    if d <= R + 1:                      # 1px anti-aliased edge
        return GOLD + (int(255 * max(0.0, R + 1 - d)),)
    return (0, 0, 0, 0)


def write_png(path, size):
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0 for the scanline
        for x in range(size):
            raw += bytes(_pixel(x, y, size))

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        f.write(chunk(b"IEND", b""))
    print("wrote", path, f"{size}x{size}")


if __name__ == "__main__":
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    write_png(os.path.join(here, "tray.png"), 32)
    write_png(os.path.join(here, "icon.png"), 512)  # electron-builder wants >=512
