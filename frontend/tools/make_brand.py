"""Build the integration's brand images (the icon Home Assistant shows for it) from the logo.

    python tools/make_brand.py

Home Assistant (2026.x) serves a custom integration's own brand images from its brand/ folder:
icon.png (square, 256 px) and logo.png (wide, 128-256 px high), each also @2x; the dark_ variants
fall back to these. They're drawn from the same outlines as the dashboard's icons in src/icons.js
(made by make_icons.py from the logo): all three batteries, fitted into the square for the icon.

Needs Pillow (Home Assistant's test environment has it).
"""

from pathlib import Path
import re

from PIL import Image, ImageChops, ImageDraw

HERE = Path(__file__).resolve().parent
ICONS_JS = HERE.parent / "src" / "icons.js"
OUT = HERE.parent.parent / "custom_components" / "cellkeeper" / "brand"

GREEN = (67, 160, 71)  # #43a047, Home Assistant's success green: readable on light and dark
SUPER = 8  # drawn this many times larger, then scaled down, for smooth edges
VIEW = 24.0  # the icons' viewBox


def icon_paths():
    text = ICONS_JS.read_text(encoding="utf-8")
    return dict(re.findall(r'^  "([a-z-]+)": "([^"]+)",$', text, re.M))


def rings(path):
    """The path's closed polygons, each a list of (x, y) in viewBox units."""
    out = []
    for sub in re.findall(r"M([^Z]+)Z", path):
        nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", sub)]
        out.append(list(zip(nums[0::2], nums[1::2])))
    return out


def bounds(rs):
    xs = [x for r in rs for x, _ in r]
    ys = [y for r in rs for _, y in r]
    return min(xs), min(ys), max(xs), max(ys)


def render(rs, width, height, pad):
    """The polygons filled even-odd (holes stay clear), fitted into width x height with `pad`
    pixels free around them, as RGBA."""
    x0, y0, x1, y1 = bounds(rs)
    scale = min((width - 2 * pad) / (x1 - x0), (height - 2 * pad) / (y1 - y0))
    ox = (width - (x1 - x0) * scale) / 2 - x0 * scale
    oy = (height - (y1 - y0) * scale) / 2 - y0 * scale
    W, H = width * SUPER, height * SUPER
    mask = Image.new("L", (W, H), 0)
    for r in rs:
        one = Image.new("L", (W, H), 0)
        ImageDraw.Draw(one).polygon([((ox + x * scale) * SUPER, (oy + y * scale) * SUPER) for x, y in r], fill=255)
        mask = ImageChops.logical_xor(mask.convert("1"), one.convert("1")).convert("L")
    mask = mask.resize((width, height), Image.LANCZOS)
    img = Image.new("RGBA", (width, height), (*GREEN, 0))
    img.putalpha(mask)
    return img


def main():
    paths = icon_paths()
    logo = rings(paths["logo"])
    OUT.mkdir(parents=True, exist_ok=True)
    x0, y0, x1, y1 = bounds(logo)
    aspect = (x1 - x0) / (y1 - y0)
    for suffix, k in (("", 1), ("@2x", 2)):
        render(logo, 256 * k, 256 * k, 8 * k).save(OUT / f"icon{suffix}.png", optimize=True)
        h = 128 * k
        render(logo, round(h * aspect) + 8 * k, h + 8 * k, 4 * k).save(OUT / f"logo{suffix}.png", optimize=True)
    for f in sorted(OUT.iterdir()):
        print(f.name, Image.open(f).size, f.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
