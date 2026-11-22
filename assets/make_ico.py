#!/usr/bin/env python3
"""Pack the existing icon PNGs into assets/icon.ico for the Windows app.

Standard library only, no drawing: it reuses the PNGs in assets/icon.iconset
(made by make_icons.py), so the Windows icon is the same ticket heart as the
Mac one. Run from anywhere:

    python3 assets/make_ico.py

The release workflow runs it before building; icon.ico is git-ignored.
"""
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = [(16, "icon_16x16.png"), (32, "icon_16x16@2x.png"), (64, "icon_32x32@2x.png"),
           (128, "icon_128x128.png"), (256, "icon_256x256.png")]


def png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


def build(out_path=os.path.join(HERE, "icon.ico")):
    images = []
    for size, name in SOURCES:
        with open(os.path.join(HERE, "icon.iconset", name), "rb") as f:
            data = f.read()
        if png_size(data) != (size, size):
            raise ValueError("%s is not %dx%d" % (name, size, size))
        images.append((size, data))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:   # PNG-compressed entries (Windows Vista and later)
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    with open(out_path, "wb") as f:
        f.write(header + entries + blobs)
    return out_path


if __name__ == "__main__":
    print("wrote", build())
