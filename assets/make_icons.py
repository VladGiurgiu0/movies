#!/usr/bin/env python3
"""Generate every Tastebuds icon from one drawing: the ticket heart.

Run from the repo root (needs matplotlib + numpy, dev-time only):

    python3 assets/make_icons.py

Outputs, all into assets/:
  icon-64/180/192/512.png   full-bleed squares (favicon, apple-touch, PWA)
  icon.iconset/…            macOS iconset (squircle + transparent margin);
                            turn into icon.icns on a Mac with `make icns`
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.path import Path
from matplotlib.patches import PathPatch
import matplotlib.transforms as mt

HERE = os.path.dirname(os.path.abspath(__file__))
CREAM, VIOLET = "#f1eee4", "#6f51e0"


def signed_area(p):
    x, y = p[:, 0], p[:, 1]
    return 0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)


def orient(p, ccw=True):
    return p if (signed_area(p) > 0) == ccw else p[::-1]


def squircle(cx, cy, r, n=5.0, pts=241):
    t = np.linspace(0, 2 * np.pi, pts)
    return np.column_stack([cx + r * np.sign(np.cos(t)) * np.abs(np.cos(t)) ** (2 / n),
                            cy + r * np.sign(np.sin(t)) * np.abs(np.sin(t)) ** (2 / n)])


def heart(cx, cy, s, pts=241):
    t = np.linspace(0, 2 * np.pi, pts)
    return np.column_stack([cx + 16 * np.sin(t) ** 3 * s / 16,
                            cy + (13 * np.cos(t) - 5 * np.cos(2 * t)
                                  - 2 * np.cos(3 * t) - np.cos(4 * t)) * s / 16])


def halfdisk(cx, r, sign, pts=61):
    t = np.linspace(-np.pi / 2, np.pi / 2, pts) if sign > 0 else np.linspace(np.pi / 2, 3 * np.pi / 2, pts)
    arc = np.column_stack([cx + r * np.cos(t), r * np.sin(t)])
    return np.vstack([arc, arc[:1]])


def punched(outer, holes, **kw):
    verts, codes = [], []
    for poly in [orient(outer, True)] + [orient(h, False) for h in holes]:
        verts.extend(poly)
        codes.extend([Path.MOVETO] + [Path.LINETO] * (len(poly) - 1))
    return PathPatch(Path(verts, codes), **kw)


def draw(px, mode="square"):
    """mode 'square': full-bleed (web/PWA/apple-touch).
    mode 'macos': squircle at ~82% with transparent margin (icns)."""
    fig = plt.figure(figsize=(px / 100, px / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 100); ax.set_ylim(0, 100); ax.axis("off"); ax.set_aspect("equal")
    if mode == "square":
        ax.add_patch(plt.Rectangle((0, 0), 100, 100, fc=CREAM, ec="none"))
    else:
        ax.add_patch(PathPatch(Path(squircle(50, 50, 41)), fc=CREAM, ec="none"))
    w, h, r = 64, 42, 4
    tx = np.array([[-w / 2 + r, -h / 2], [w / 2 - r, -h / 2], [w / 2, -h / 2 + r], [w / 2, h / 2 - r],
                   [w / 2 - r, h / 2], [-w / 2 + r, h / 2], [-w / 2, h / 2 - r], [-w / 2, -h / 2 + r]])
    scale = 1.0 if mode == "square" else 0.82
    tr = mt.Affine2D().rotate_deg(-8).scale(scale).translate(50, 50)
    outer = tr.transform(tx)
    holes = [tr.transform(p) for p in (halfdisk(-w / 2, 6, +1), halfdisk(w / 2, 6, -1), heart(0, -1, 12.5))]
    ax.add_patch(punched(outer, holes, fc=VIOLET, ec="none"))
    return fig


def save(path, px, mode):
    fig = draw(px, mode)
    fig.savefig(path, transparent=(mode == "macos"))
    plt.close(fig)


if __name__ == "__main__":
    for px in (64, 180, 192, 512):
        save(os.path.join(HERE, "icon-%d.png" % px), px, "square")
    iconset = os.path.join(HERE, "icon.iconset")
    os.makedirs(iconset, exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        save(os.path.join(iconset, "icon_%dx%d.png" % (base, base)), base, "macos")
        save(os.path.join(iconset, "icon_%dx%d@2x.png" % (base, base)), base * 2, "macos")
    print("icons written to", HERE)
