"""Qt text rendering (HarfBuzz shaping => correct Kannada conjuncts) for covers and Kannada-as-image pages."""
from __future__ import annotations

import io
import os
import threading

from PIL import Image

_lock = threading.Lock()
_app = None

KANNADA_FONTS = '"Noto Sans Kannada", "Kannada Sangam MN", "Kannada MN"'
SERIF = '"Georgia", "Times New Roman"'
SANS = '"Helvetica Neue", "Arial"'


def ensure_qt():
    """Make sure a Q(Gui)Application exists (CLI use creates an offscreen one)."""
    global _app
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _app = QGuiApplication([])
    return QGuiApplication.instance()


def _qimage_to_pil(img) -> Image.Image:
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return Image.open(io.BytesIO(bytes(ba))).convert("L")


def render_html(html: str, width: int, font_px: int = 26, family: str = KANNADA_FONTS, bold: bool = False,
                align: str = "left", line_height: int = 150) -> Image.Image:
    """Lay out an HTML fragment at a fixed width and return a grayscale image."""
    ensure_qt()
    from PySide6.QtGui import QTextDocument, QImage, QPainter, QColor, QFont
    with _lock:
        doc = QTextDocument()
        doc.setDocumentMargin(0)
        f = QFont()
        f.setPixelSize(font_px)
        doc.setDefaultFont(f)
        doc.setDefaultStyleSheet(
            f"body{{font-family:{family},{SANS};font-size:{font_px}px;color:#000;"
            f"font-weight:{'bold' if bold else 'normal'}}}"
            f"p{{margin:0;line-height:{line_height}%;text-align:{align}}}")
        doc.setHtml(f'<body><p align="{align}">{html}</p></body>')
        doc.setTextWidth(width)
        h = max(1, int(doc.size().height() + 0.5) + 4)
        img = QImage(width, h, QImage.Format_RGB32)
        img.fill(QColor("white"))
        p = QPainter(img)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setRenderHint(QPainter.Antialiasing)
        doc.drawContents(p)
        p.end()
        return _qimage_to_pil(img)


def make_cover(title: str, author: str, width: int, height: int, kannada: bool = False) -> Image.Image:
    """A clean typographic cover sized exactly to the device screen."""
    ensure_qt()
    from PySide6.QtGui import QTextDocument, QImage, QPainter, QColor, QFont, QPen
    from PySide6.QtCore import QRectF, Qt
    from .epub import esc

    title = (title or "Untitled").strip()
    author = (author or "").strip()
    fam = f"{KANNADA_FONTS},{SERIF}" if kannada else SERIF
    with _lock:
        img = QImage(width, height, QImage.Format_RGB32)
        img.fill(QColor("white"))
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)

        band_h = int(height * 0.62)
        p.fillRect(0, 0, width, band_h, QColor("#111111"))
        # thin inner frame lines for a bookish feel
        pen = QPen(QColor("#9a9a9a"))
        pen.setWidth(2)
        p.setPen(pen)
        m = int(width * 0.06)
        p.drawRect(m, m, width - 2 * m, band_h - 2 * m)
        p.fillRect(m, band_h + int(height * 0.045), int(width * 0.18), 6, QColor("#111111"))

        def longest_word_fits(text, px, weight, max_w):
            from PySide6.QtGui import QFontMetrics
            f = QFont(fam.split(",")[0].strip('" ') if kannada else "Georgia")
            f.setPixelSize(px)
            f.setBold(weight >= 600)
            fm = QFontMetrics(f)
            return all(fm.horizontalAdvance(w) <= max_w * 0.97 for w in text.split())

        def layout(text, px, color, weight, max_w, max_h, min_px=18):
            while px > min_px and not longest_word_fits(text, px, weight, max_w):
                px -= 2
            while True:
                doc = QTextDocument()
                doc.setDocumentMargin(0)
                f = QFont()
                f.setPixelSize(px)
                doc.setDefaultFont(f)
                doc.setDefaultStyleSheet(
                    f"p{{font-family:{fam};font-size:{px}px;color:{color};font-weight:{weight};"
                    f"line-height:118%;margin:0}}")
                doc.setHtml(f'<p align="left">{esc(text)}</p>')
                doc.setTextWidth(max_w)
                if doc.size().height() <= max_h or px <= min_px:
                    return doc
                px -= 2

        inner_w = width - 4 * m
        tdoc = layout(title, int(width * 0.13), "#ffffff", 700, inner_w, band_h - 4 * m)
        th = tdoc.size().height()
        p.save()
        p.translate(2 * m, band_h - 2 * m - th)
        tdoc.drawContents(p)
        p.restore()

        if author:
            adoc = layout(author, int(width * 0.07), "#111111", 400, width - 2 * m, height - band_h - int(height * 0.12))
            p.save()
            p.translate(m, band_h + int(height * 0.09))
            adoc.drawContents(p)
            p.restore()
        p.end()
        return _qimage_to_pil(img)
