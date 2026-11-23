#!/usr/bin/env python3
"""Generate extension icons and package extension/ into dist/zen-pwa.xpi."""

from __future__ import annotations

import zipfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
EXT_DIR = ROOT / "extension"
ICONS_DIR = EXT_DIR / "icons"
DIST_DIR = ROOT / "dist"


def build_extension_icons() -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    for size in (48, 96):
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        pad = max(2, size // 16)
        radius = size // 4
        draw.rounded_rectangle(
            [(pad, pad), (size - pad, size - pad)],
            radius=radius,
            fill=(37, 99, 235, 255),
        )
        try:
            font = ImageFont.truetype("segoeuib.ttf", int(size * 0.56))
        except Exception:
            font = ImageFont.load_default()
        text = "Z"
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        draw.text(
            ((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]),
            text,
            fill=(255, 255, 255, 255),
            font=font,
        )
        img.save(ICONS_DIR / f"icon-{size}.png", format="PNG")


def build_xpi() -> Path:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    xpi_path = DIST_DIR / "zen-pwa.xpi"
    with zipfile.ZipFile(xpi_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(EXT_DIR.rglob("*")):
            if file_path.is_file():
                arcname = file_path.relative_to(EXT_DIR).as_posix()
                zf.write(file_path, arcname)
    return xpi_path


if __name__ == "__main__":
    build_extension_icons()
    out = build_xpi()
    print(f"Built icons and packaged: {out}")
