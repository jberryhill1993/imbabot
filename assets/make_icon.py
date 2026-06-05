"""Generate the Imbabot app icon (assets/imbabot.icns + imbabot.ico).

Draws a dark rounded-square with an up (green) / down (red) breakout arrow pair —
the opening-range straddle motif. Redraws crisply at each icon size, then packs an
.iconset -> .icns (macOS) and a multi-size .ico (Windows).

Run:  python assets/make_icon.py
Requires Pillow; .icns step uses macOS `iconutil`.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent

BG = (15, 22, 31, 255)          # dark navy, matches the app theme
BORDER = (77, 163, 255, 255)    # accent blue
GREEN = (46, 204, 113, 255)
RED = (231, 76, 60, 255)


def _triangle(d: ImageDraw.ImageDraw, cx, apex_y, base_y, half_w, color):
    d.polygon([(cx, apex_y), (cx - half_w, base_y), (cx + half_w, base_y)], fill=color)


def draw_icon(S: int) -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = S * 0.045
    radius = S * 0.225
    d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=radius, fill=BG)
    # thin inner accent border
    bw = max(1, int(S * 0.012))
    d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=radius,
                        outline=BORDER, width=bw)

    # UP arrow (green) — left of center
    cx1 = S * 0.34
    head_w = S * 0.15
    stem_w = S * 0.052
    _triangle(d, cx1, S * 0.24, S * 0.55, head_w, GREEN)              # head
    d.rectangle([cx1 - stem_w, S * 0.55, cx1 + stem_w, S * 0.80], fill=GREEN)  # shaft

    # DOWN arrow (red) — right of center
    cx2 = S * 0.66
    d.rectangle([cx2 - stem_w, S * 0.20, cx2 + stem_w, S * 0.45], fill=RED)    # shaft
    _triangle(d, cx2, S * 0.76, S * 0.45, head_w, RED)               # head (apex down)

    return img


ICONSET_SIZES = [16, 32, 64, 128, 256, 512, 1024]
ICNS_MAP = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]


def main() -> int:
    # preview
    draw_icon(512).save(HERE / "icon_preview.png")

    # macOS .iconset -> .icns
    iconset = HERE / "imbabot.iconset"
    iconset.mkdir(exist_ok=True)
    for name, size in ICNS_MAP:
        draw_icon(size).save(iconset / name)
    icns = HERE / "imbabot.icns"
    try:
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
        print(f"wrote {icns}")
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"iconutil unavailable ({exc}); .icns not built (fine off-macOS)")

    # Windows .ico (multi-size)
    ico = HERE / "imbabot.ico"
    base = draw_icon(256)
    base.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"wrote {ico}")
    print(f"wrote {HERE / 'icon_preview.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
