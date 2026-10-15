"""Image processing tuned for 4-level grayscale e-ink panels."""
from __future__ import annotations

import io

from PIL import Image, ImageOps, ImageFilter, ImageEnhance, ImageStat

Image.MAX_IMAGE_PIXELS = 400_000_000

RASTER_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/tiff"}


def open_image(data: bytes, media_type: str = "") -> Image.Image | None:
    if media_type == "image/svg+xml" or data.lstrip()[:5] in (b"<?xml", b"<svg ", b"<svg\n") or b"<svg" in data[:300]:
        return render_svg(data)
    try:
        im = Image.open(io.BytesIO(data))
        if getattr(im, "is_animated", False):
            im.seek(0)
        im.load()
        return im
    except Exception:
        return None


def render_svg(data: bytes, max_w: int = 1200) -> Image.Image | None:
    """Rasterize SVG with Qt (QtSvg). Returns None if Qt/QtSvg is unavailable or the SVG is invalid."""
    try:
        from PySide6.QtSvg import QSvgRenderer
        from PySide6.QtGui import QImage, QPainter, QColor
        from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    except Exception:
        return None
    r = QSvgRenderer(QByteArray(data))
    if not r.isValid():
        return None
    size = r.defaultSize()
    w, h = max(1, size.width()), max(1, size.height())
    scale = min(4.0, max_w / w) if w < max_w else max_w / w
    W, H = max(1, int(w * scale)), max(1, int(h * scale))
    img = QImage(W, H, QImage.Format_ARGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    r.render(p)
    p.end()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return Image.open(io.BytesIO(bytes(ba))).convert("RGB")


def flatten(im: Image.Image) -> Image.Image:
    """Composite transparency on white; returns RGB or L."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        return bg.convert("RGB")
    if im.mode in ("1", "L", "RGB"):
        return im
    return im.convert("RGB")


def is_line_art(im: Image.Image) -> bool:
    """Diagrams, text scans, charts: few distinct tones, mostly white."""
    g = im.convert("L")
    small = g.resize((min(200, g.width), min(200, g.height)))
    hist = small.histogram()
    total = sum(hist) or 1
    light = sum(hist[230:]) / total
    dark = sum(hist[:40]) / total
    mid = 1 - light - dark
    return light > 0.55 and mid < 0.25


def fit_size(w: int, h: int, max_w: int, max_h: int) -> tuple[int, int]:
    if w <= max_w and h <= max_h:
        return w, h
    s = min(max_w / w, max_h / h)
    return max(1, round(w * s)), max(1, round(h * s))


def boost_contrast(g: Image.Image, line_art: bool) -> Image.Image:
    """Stretch levels and darken mid-tones so they survive 4-level quantization."""
    g = ImageOps.autocontrast(g, cutoff=(1, 1) if not line_art else (0.5, 2))
    # gamma < 1 darkens mid-tones; e-ink panels render greys too light
    exponent = 1.25 if not line_art else 1.45
    g = g.point([round(255 * (i / 255) ** exponent) for i in range(256)])
    g = ImageEnhance.Sharpness(g).enhance(1.4 if not line_art else 1.8)
    return g


def process(im: Image.Image, max_w: int, max_h: int, grayscale: bool, contrast: bool,
            upscale_small: bool = False) -> tuple[Image.Image, bool]:
    """Return processed image and whether it is line art (=> PNG may be smaller/sharper)."""
    im = flatten(im)
    line_art = is_line_art(im)
    w, h = im.size
    nw, nh = fit_size(w, h, max_w, max_h)
    if upscale_small and w < max_w * 0.6 and h < max_h * 0.6:
        s = min(max_w / w, max_h / h, 2.0)
        nw, nh = round(w * s), round(h * s)
    if (nw, nh) != (w, h):
        im = im.resize((nw, nh), Image.LANCZOS)
    if grayscale:
        im = im.convert("L")
        if contrast:
            im = boost_contrast(im, line_art)
    elif contrast:
        im = ImageEnhance.Contrast(im).enhance(1.25)
    return im, line_art


def encode(im: Image.Image, quality: int, prefer_png: bool = False) -> tuple[bytes, str]:
    """Baseline JPEG (progressive JPEGs decode at 1/8 resolution on Xteink firmware) or optimized PNG."""
    if prefer_png:
        buf = io.BytesIO()
        out = im
        if im.mode == "L":
            # 16 grey levels are plenty for a 4-level panel and keep PNGs tiny
            out = im.point(lambda v: (v // 17) * 17)
        out.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png"
    buf = io.BytesIO()
    if im.mode not in ("L", "RGB"):
        im = im.convert("RGB")
    im.save(buf, "JPEG", quality=int(quality), optimize=True, progressive=False, subsampling=2 if im.mode == "RGB" else 0)
    return buf.getvalue(), "image/jpeg"


def encode_4level(im: Image.Image) -> tuple[bytes, str]:
    """Text images for a 4-grey panel: snap to black/dark/light/white, then PNG (very small)."""
    g = im.convert("L").point([0 if v < 64 else 85 if v < 128 else 170 if v < 200 else 255 for v in range(256)])
    buf = io.BytesIO()
    g.save(buf, "PNG", optimize=True)
    return buf.getvalue(), "image/png"


def is_blank(im: Image.Image) -> bool:
    g = im.convert("L")
    st = ImageStat.Stat(g.resize((64, 64)))
    return st.stddev[0] < 2.0


def trim_margins(im: Image.Image, pad: int = 6, threshold: int = 235) -> Image.Image:
    """Crop uniform white margins (used for PDF page images)."""
    g = im.convert("L")
    mask = g.point(lambda v: 255 if v < threshold else 0).filter(ImageFilter.MaxFilter(3))
    box = mask.getbbox()
    if not box:
        return im
    l, t, r, b = box
    l, t = max(0, l - pad), max(0, t - pad)
    r, b = min(im.width, r + pad), min(im.height, b + pad)
    if (r - l) < im.width * 0.3 or (b - t) < im.height * 0.3:
        return im
    return im.crop((l, t, r, b))
