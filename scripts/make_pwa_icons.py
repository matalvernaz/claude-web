"""Regenerate the PWA / home-screen icons in static/.

Icons are committed to the repo; this script only exists so they can be
reproduced (or recoloured) without a design tool. Needs Pillow, which is not
in requirements.txt — run it with any interpreter that has Pillow installed:

    python3 scripts/make_pwa_icons.py

Three shapes, because the platforms mask differently:

* ``icon-192`` / ``icon-512`` (manifest purpose ``any``) — rounded corners
  baked in, since nothing masks these.
* ``icon-maskable-512`` (purpose ``maskable``) — full-bleed square with the
  glyph pulled into the centre 60% so Android's circle/squircle crop can take
  up to 20% off every edge without clipping it.
* ``apple-touch-icon`` — opaque square, no rounding: iOS applies its own mask
  and a pre-rounded source ends up with doubled corners.
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

BG = (26, 26, 26, 255)       # --bg      #1a1a1a
ACCENT = (122, 162, 247, 255)  # --accent #7aa2f7
FG = (230, 230, 230, 255)    # --fg      #e6e6e6

CORNER_RADIUS_FRAC = 0.18
STROKE_FRAC = 0.075


def _draw_glyph(img: Image.Image, glyph_frac: float) -> None:
    """Draw a terminal prompt (chevron + cursor) centred on ``img``.

    ``glyph_frac`` is the glyph's width as a fraction of the image edge.
    """
    size = img.size[0]
    draw = ImageDraw.Draw(img)
    cx = cy = size / 2
    width = size * glyph_frac
    stroke = max(2, round(size * STROKE_FRAC))
    half_h = width * 0.28

    chev_left = cx - width * 0.45
    chev_right = cx - width * 0.05
    draw.line(
        [
            (chev_left, cy - half_h),
            (chev_right, cy),
            (chev_left, cy + half_h),
        ],
        fill=ACCENT,
        width=stroke,
        joint="curve",
    )

    # Underscore cursor, baseline-aligned with the chevron's lower arm.
    draw.rounded_rectangle(
        [
            (cx + width * 0.08, cy + half_h - stroke),
            (cx + width * 0.45, cy + half_h),
        ],
        radius=stroke / 2,
        fill=FG,
    )


def _rounded(size: int, glyph_frac: float) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle(
        [(0, 0), (size - 1, size - 1)],
        radius=round(size * CORNER_RADIUS_FRAC),
        fill=BG,
    )
    _draw_glyph(img, glyph_frac)
    return img


def _square(size: int, glyph_frac: float) -> Image.Image:
    img = Image.new("RGBA", (size, size), BG)
    _draw_glyph(img, glyph_frac)
    return img


def main() -> int:
    written = []
    for name, img in (
        ("icon-192.png", _rounded(192, 0.52)),
        ("icon-512.png", _rounded(512, 0.52)),
        ("icon-maskable-512.png", _square(512, 0.40)),
        ("apple-touch-icon.png", _square(180, 0.52)),
    ):
        path = STATIC_DIR / name
        # Apple's icon is composited on an opaque background by iOS anyway, and
        # a stray alpha channel there has historically rendered as black.
        out = img.convert("RGB") if name == "apple-touch-icon.png" else img
        out.save(path, "PNG", optimize=True)
        written.append(f"{path.name} {out.size[0]}x{out.size[1]} {out.mode}")
    print("\n".join(written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
