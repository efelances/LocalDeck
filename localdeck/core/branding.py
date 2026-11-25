"""Geometric 3-color LocalDeck brand logo (SVG, Pillow RGBA, and Windows .ico)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
  <rect x="4" y="4" width="120" height="120" rx="26" fill="#1C1B19" stroke="#3D3933" stroke-width="4"/>
  <rect x="22" y="22" width="38" height="38" rx="9" fill="#F7F5F0"/>
  <rect x="68" y="22" width="38" height="38" rx="9" fill="#2A6E53"/>
  <circle cx="87" cy="41" r="5" fill="#F7F5F0"/>
  <rect x="22" y="68" width="38" height="38" rx="9" fill="#D8D3C8"/>
  <rect x="68" y="68" width="38" height="38" rx="9" fill="#F7F5F0"/>
</svg>"""


def render_logo_image(size: int = 256) -> Image.Image:
    """Render the geometric LocalDeck logo with 2x supersampling for crisp edges."""
    scale = max(size * 2, 128)
    img = Image.new("RGBA", (scale, scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def sc(v: float) -> float:
        return v * (scale / 128.0)

    draw.rounded_rectangle(
        (sc(4), sc(4), sc(124), sc(124)),
        radius=sc(26),
        fill="#1C1B19",
        outline="#3D3933",
        width=max(2, int(round(sc(4)))),
    )
    # 2x2 tactile keycaps
    draw.rounded_rectangle((sc(22), sc(22), sc(60), sc(60)), radius=sc(9), fill="#F7F5F0")
    draw.rounded_rectangle((sc(68), sc(22), sc(106), sc(60)), radius=sc(9), fill="#2A6E53")
    draw.ellipse((sc(82), sc(36), sc(92), sc(46)), fill="#F7F5F0")
    draw.rounded_rectangle((sc(22), sc(68), sc(60), sc(106)), radius=sc(9), fill="#D8D3C8")
    draw.rounded_rectangle((sc(68), sc(68), sc(106), sc(106)), radius=sc(9), fill="#F7F5F0")

    return img.resize((size, size), Image.Resampling.LANCZOS)


def write_logo_ico(dest: Path) -> Path:
    """Write a multi-resolution Windows .ico file containing 16..256px frames."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    base = render_logo_image(256)
    base.save(
        dest,
        format="ICO",
        bitmap_format="bmp",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return dest
