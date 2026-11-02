"""
Make the Jubao logo files transparent and generate the extra variants.

Run from the project root:
    pip install pillow numpy scipy
    python tools/make_logo_assets.py

Reads  static/images/logo/jubao_logo.png and jubao_text.png (white background)
Writes static/images/logo/jubao_logo.png, jubao_logo_light.png,
       jubao_text.png, jubao_text_light.png   (transparent)
       static/images/favicon.ico, favicon-32.png (transparent), favicon.svg
       (switches to the light icon in dark browser mode),
       apple-touch-icon.png, icon-192.png, icon-512.png (solid background on purpose)
Originals are copied to static/images/logo/originals/ first.
"""
import base64
import io
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

LOGO_DIR = Path("static/images/logo")
IMG_DIR = Path("static/images")
LIGHT_FG = (250, 247, 240)  # replaces the navy on dark backgrounds
PAD = 0.04  # transparent margin kept around the trimmed logo


def load_on_white(path):
    img = Image.open(path).convert("RGBA")
    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    return Image.alpha_composite(white, img).convert("RGB")


def detect_brand_colors(rgb):
    """Find the flat fill colors: navy (darkest), red and gold. Returns (colors, navy_index)."""
    arr = np.asarray(rgb, dtype=float).reshape(-1, 3)
    strong = arr[arr.min(axis=1) < 150]  # skip white and anti-aliased edge pixels
    if len(strong) == 0:
        sys.exit("No logo pixels found")
    r, g, b = strong[:, 0], strong[:, 1], strong[:, 2]
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    seeds = [strong[np.argmin(lum)]]  # navy
    red_score = r - np.maximum(g, b)
    if red_score.max() > 80:
        seeds.append(strong[np.argmax(red_score)])
    gold_score = np.where(r > 150, np.minimum(r, g) - b, -999)
    if gold_score.max() > 60:
        seeds.append(strong[np.argmax(gold_score)])
    colors = []
    for seed in seeds:
        near = strong[np.linalg.norm(strong - seed, axis=1) < 40]
        colors.append(np.median(near, axis=0))
    return colors, 0


def to_rgba(rgb, colors, recolor=None):
    """Un-mix each pixel from white: pixel = white + t * (color - white)."""
    p = np.asarray(rgb, dtype=float) - 255.0  # H x W x 3
    best_err = np.full(p.shape[:2], np.inf)
    best_t = np.zeros(p.shape[:2])
    best_idx = np.zeros(p.shape[:2], dtype=int)
    for i, c in enumerate(colors):
        v = np.asarray(c, dtype=float) - 255.0
        t = np.clip((p @ v) / (v @ v), 0, 1)
        err = np.linalg.norm(p - t[..., None] * v, axis=2)
        better = err < best_err
        best_err[better] = err[better]
        best_t[better] = t[better]
        best_idx[better] = i
    out = np.zeros(p.shape[:2] + (4,), dtype=np.uint8)
    for i, c in enumerate(colors):
        col = recolor.get(i, c) if recolor else c
        out[best_idx == i, :3] = np.round(col).astype(np.uint8)
    alpha = best_t
    alpha[alpha < 0.04] = 0
    alpha[alpha > 0.96] = 1
    out[..., 3] = np.round(alpha * 255).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def despeckle(img, min_share=0.002):
    """Remove tiny stray specks (needs scipy; skipped if it is not installed)."""
    try:
        from scipy import ndimage
    except ImportError:
        print("  (scipy not installed: skipping despeckle)")
        return img
    arr = np.array(img)
    solid = arr[..., 3] > 8
    labels, n = ndimage.label(solid, structure=np.ones((3, 3)))
    if n < 2:
        return img
    sizes = ndimage.sum(solid, labels, range(1, n + 1))
    for i, size in enumerate(sizes, start=1):
        if size < solid.sum() * min_share:
            arr[labels == i, 3] = 0
    return Image.fromarray(arr, "RGBA")


def trim(img, pad=PAD):
    img = despeckle(img)
    bbox = img.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    img = img.crop(bbox)
    m = int(max(img.size) * pad)
    canvas = Image.new("RGBA", (img.width + 2 * m, img.height + 2 * m), (0, 0, 0, 0))
    canvas.paste(img, (m, m))
    return canvas


def square(img, size, fill=(255, 255, 255, 255), inner=0.72):
    """Icon centered on a solid square (for app icons)."""
    canvas = Image.new("RGBA", (size, size), fill)
    target = int(size * inner)
    scale = target / max(img.size)
    icon = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    canvas.alpha_composite(icon, ((size - icon.width) // 2, (size - icon.height) // 2))
    return canvas.convert("RGB")


def fit_transparent(img, size, inner=0.94):
    """Icon centered on a transparent square (for favicons)."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    scale = size * inner / max(img.size)
    icon = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    canvas.alpha_composite(icon, ((size - icon.width) // 2, (size - icon.height) // 2))
    return canvas


def png_b64(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def process(name):
    src = LOGO_DIR / f"{name}.png"
    if not src.exists():
        sys.exit(f"Missing {src}")
    backup = LOGO_DIR / "originals"
    backup.mkdir(exist_ok=True)
    if not (backup / src.name).exists():
        shutil.copy(src, backup / src.name)
    rgb = load_on_white(backup / src.name)  # always start from the original
    colors, idx_navy = detect_brand_colors(rgb)
    dark_on_light = trim(to_rgba(rgb, colors))
    light = trim(to_rgba(rgb, colors, recolor={idx_navy: LIGHT_FG}))
    dark_on_light.save(LOGO_DIR / f"{name}.png", optimize=True)
    light.save(LOGO_DIR / f"{name}_light.png", optimize=True)
    print(f"{name}: colors found {[tuple(int(v) for v in c) for c in colors]} (navy first, then red, gold)")
    return dark_on_light, light


def main():
    icon, icon_light = process("jubao_logo")
    process("jubao_text")
    # App icons keep a solid background on purpose (iOS and Android fill transparency).
    square(icon, 512).save(IMG_DIR / "icon-512.png", optimize=True)
    square(icon, 192).save(IMG_DIR / "icon-192.png", optimize=True)
    square(icon, 180, inner=0.80).save(IMG_DIR / "apple-touch-icon.png", optimize=True)
    # Browser-tab favicons are transparent.
    fav = fit_transparent(icon, 256)
    fav.resize((32, 32), Image.LANCZOS).save(IMG_DIR / "favicon-32.png", optimize=True)
    fav.save(IMG_DIR / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    dark_png = png_b64(fit_transparent(icon, 64))
    light_png = png_b64(fit_transparent(icon_light, 64))
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">'
        '<style>.l{display:none}@media (prefers-color-scheme:dark){.d{display:none}.l{display:inline}}</style>'
        f'<image class="d" width="64" height="64" href="data:image/png;base64,{dark_png}"/>'
        f'<image class="l" width="64" height="64" href="data:image/png;base64,{light_png}"/>'
        '</svg>'
    )
    (IMG_DIR / "favicon.svg").write_text(svg, encoding="utf-8")
    print("Done. Open the PNGs on a dark and a light background to check the edges.")


if __name__ == "__main__":
    main()