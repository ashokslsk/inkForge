"""PDF -> EPUB.

Paths, chosen per book in Auto mode:
  * text reflow   - the PDF has a clean Unicode text layer (English or Kannada)
  * OCR           - scanned / broken text layer, Tesseract has the needed language and reads it well
  * image reflow  - scanned / broken text layer (e.g. Nudi legacy Kannada fonts): printed words are cut
                    out and re-wrapped at a larger size, like k2pdfopt; faithful for any script
  * page images   - each page as one screen-sized image (comics, magazines, heavy maths)
"""
from __future__ import annotations

import io
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from . import images as imglib
from .devices import get_device
from .epub import Epub, esc, xhtml_page
from .options import Options, BookError
from .textclean import (KANNADA_RE, cleanup_text, dedupe_kannada, dehyphenate_join, kannada_quality, looks_like_legacy_kannada,
                        normalize, pua_ratio)

PRE = "inkpre_"   # images already tuned for the device: the pipeline leaves them alone


def _pymupdf():
    try:
        import pymupdf
        return pymupdf
    except ImportError:  # pragma: no cover
        raise BookError("PDF support is missing (PyMuPDF is not installed).")


# ====================================================================== shared helpers
def parse_filename(stem: str) -> tuple[str, str]:
    """'Title (Author) (z-library.sk, ...)' -> (title, author)."""
    s = re.sub(r"\s*\((?:z-library|z-lib|1lib|libgen)[^)]*\)", "", stem, flags=re.I).strip()
    m = re.match(r"^(.*?)\s*\(([^()]{2,80})\)\s*$", s)
    if m and not re.search(r"\d{4}|edition|vol", m.group(2), re.I):
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^(.*?)\s+[-–]\s+(.+)$", s)
    if m and len(m.group(2).split()) <= 4:
        return m.group(1).strip(), m.group(2).strip()
    return s.replace("_", " ").strip(), ""


def good_meta(s: str) -> bool:
    if not s or len(s.strip()) < 2:
        return False
    if re.fullmatch(r"[A-Za-z]{0,4}[\d_.-]+\w*", s.strip()) or re.fullmatch(r"[a-z0-9_.]+", s.strip()):
        return False  # user names / ids like "a3366", "admin01"
    s = s.strip()
    if re.fullmatch(r"(?i)unknown|anonymous|author|user|admin|owner|creator|title|none|n/?a", s):
        return False
    return not re.search(r"(?i)^(untitled|microsoft word|document\d*|\S+\.(docx?|pdf|indd|qxd|tex))", s)


class EpubBuilder:
    """Assembles a fresh EPUB 3 (with NCX for older readers)."""

    def __init__(self, title: str, author: str, lang: str):
        self.title, self.author, self.lang = title or "Untitled", author, lang
        self.files: dict[str, bytes] = {}
        self.manifest: list[tuple[str, str, str, str]] = []   # id, href, media, props
        self.spine: list[str] = []
        self.toc: list[tuple[int, str, str]] = []
        self.cover_id = None
        self._n = 0
        self.add("Styles/book.css", b"", "text/css")

    def nid(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    def add(self, href, data, media, props="", spine=False, iid=None):
        iid = iid or self.nid("i")
        self.files["OEBPS/" + href] = data
        self.manifest.append((iid, href, media, props))
        if spine:
            self.spine.append(iid)
        return iid

    def add_image(self, im_bytes: bytes, media: str, name: str | None = None) -> str:
        ext = ".jpg" if media == "image/jpeg" else ".png"
        href = f"Images/{PRE}{name or uuid.uuid4().hex[:12]}{ext}"
        self.add(href, im_bytes, media)
        return href

    def page(self, href, title, body):
        data = xhtml_page(title, body, ["../Styles/book.css"], self.lang)
        return self.add("Text/" + href, data, "application/xhtml+xml", spine=True)

    def set_cover(self, im_bytes, media):
        ext = ".jpg" if media == "image/jpeg" else ".png"
        self.cover_id = self.add(f"Images/{PRE}cover{ext}", im_bytes, media, props="cover-image", iid="cover-image")
        body = f'<div class="ink-page"><img src="../Images/{PRE}cover{ext}" alt="Cover"/></div>'
        data = xhtml_page("Cover", body, ["../Styles/book.css"], self.lang)
        iid = self.add("Text/cover.xhtml", data, "application/xhtml+xml", iid="cover-page")
        self.spine.insert(0, iid)

    def build(self) -> Epub:
        uid = "urn:uuid:" + str(uuid.uuid4())
        man = "".join(f'<item id="{i}" href="{esc(h)}" media-type="{m}"{f" properties={chr(34)}{p}{chr(34)}" if p else ""}/>'
                      for i, h, m, p in self.manifest)
        man += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        man += '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        spine = "".join(f'<itemref idref="{i}"/>' for i in self.spine)
        cover_meta = f'<meta name="cover" content="{self.cover_id}"/>' if self.cover_id else ""
        opf = (f'<?xml version="1.0" encoding="utf-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">'
               f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="bookid">{uid}</dc:identifier>'
               f'<dc:title>{esc(self.title)}</dc:title>' + (f'<dc:creator>{esc(self.author)}</dc:creator>' if self.author else "") +
               f'<dc:language>{self.lang}</dc:language><meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>{cover_meta}</metadata>'
               f'<manifest>{man}</manifest><spine toc="ncx">{spine}</spine></package>')
        self.files["OEBPS/content.opf"] = opf.encode("utf-8")
        toc = self.toc or [(1, self.title, self._first_text_href())]
        # nav + ncx (the optimizer pipeline re-validates them)
        lis = "".join(f'<li><a href="{esc(h)}">{esc(t)}</a></li>' for _, t, h in toc)
        self.files["OEBPS/nav.xhtml"] = xhtml_page("Contents", f'<nav epub:type="toc" id="toc"><ol>{lis}</ol></nav>', lang=self.lang)
        pts = "".join(f'<navPoint id="n{k}" playOrder="{k}"><navLabel><text>{esc(t)}</text></navLabel><content src="{esc(h)}"/></navPoint>'
                      for k, (_, t, h) in enumerate(toc, 1))
        self.files["OEBPS/toc.ncx"] = (f'<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                                       f'<head><meta name="dtb:uid" content="{uid}"/></head><docTitle><text>{esc(self.title)}</text></docTitle>'
                                       f'<navMap>{pts}</navMap></ncx>').encode("utf-8")
        self.files["mimetype"] = b"application/epub+zip"
        self.files["META-INF/container.xml"] = (b'<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                                                b'<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        return Epub(self.files)

    def _first_text_href(self):
        for i, h, m, p in self.manifest:
            if m == "application/xhtml+xml" and i in self.spine and i != "cover-page":
                return h
        return "Text/cover.xhtml"


# ====================================================================== OCR clean-up
_SCAN_JUNK = re.compile(r"(?i)\b\d{4,}\.(?:tiff?|jpe?g|png|bmp)\b|^\s*[|_\-–—.,:;'\"~*]{1,6}\s*$")
_LATIN_TOKEN = re.compile(r"(?<![\w\u0C80-\u0CFF])[A-Za-z][A-Za-z'.]{0,5}[,;:!?.]*(?![\w\u0C80-\u0CFF])")


def _drop_latin_noise(line: str) -> str:
    """In Kannada OCR, short Latin 'words' between Kannada words are misreads (e.g. 'Bd,', 'sob!')."""
    kn = len(KANNADA_RE.findall(line))
    la = len(re.findall(r"[A-Za-z]", line))
    if kn < 6 or la > kn * 0.45:
        return line   # real English (or mixed) line: leave it
    out = _LATIN_TOKEN.sub(lambda m: re.sub(r"[A-Za-z'.]", "", m.group(0)), line)  # keep trailing punctuation
    return re.sub(r"[ \t]{2,}", " ", out).strip()


# ====================================================================== OCR availability
def _vendor_dir() -> str | None:
    """OCR engine + language data shipped inside InkForge.app (or ./vendor when run from source)."""
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(getattr(sys, "_MEIPASS", ""))
        roots.append(os.path.join(os.path.dirname(sys.executable), "..", "Resources"))
    roots.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    for r in roots:
        v = os.path.join(r, "vendor")
        if os.path.exists(os.path.join(v, "tesseract", "bin", "tesseract")):
            return os.path.abspath(v)
    return None


def tesseract_path() -> str | None:
    v = _vendor_dir()
    if v:
        exe = os.path.join(v, "tesseract", "bin", "tesseract")
        if not os.access(exe, os.X_OK):
            try:
                os.chmod(exe, 0o755)
            except OSError:
                pass
        return exe
    for p in (shutil.which("tesseract"), "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
        if p and os.path.exists(p):
            return p
    return None


def _ocr_tmp() -> str:
    """Private scratch folder for OCR page images (never bare /tmp)."""
    base = os.path.join(os.path.expanduser("~/Library/Caches"), "InkForge", "ocr")
    try:
        os.makedirs(base, exist_ok=True)
        return base
    except OSError:
        return tempfile.gettempdir()


def tesseract_env() -> dict:
    env = dict(os.environ)
    v = _vendor_dir()
    if v:
        env["TESSDATA_PREFIX"] = os.path.join(v, "tessdata")
    env["OMP_THREAD_LIMIT"] = "1"   # pages are already OCR'd in parallel
    return env


def tesseract_langs() -> set[str]:
    t = tesseract_path()
    if not t:
        return set()
    try:
        out = subprocess.run([t, "--list-langs"], capture_output=True, text=True, timeout=20, env=tesseract_env()).stdout
        return {l.strip() for l in out.splitlines()[1:] if l.strip()}
    except Exception:
        return set()


# ====================================================================== converter
class PdfConverter:
    def __init__(self, path: str, opts: Options, progress=None, log=None):
        self.path = path
        self.o = opts
        self.dev = get_device(opts.device)
        self.progress = progress or (lambda *a: None)
        self.log = log or (lambda *a: None)
        self.warnings: list[str] = []
        self.method = ""

    def step(self, label, frac):
        self.progress(0, label, frac)

    def open(self):
        pymupdf = _pymupdf()
        try:
            doc = pymupdf.open(self.path)
        except Exception as e:
            raise BookError(f"This PDF could not be opened ({e}). It may be damaged or not a real PDF.")
        if doc.needs_pass:
            if not doc.authenticate(""):
                raise BookError("This PDF is password-protected. Remove the password (e.g. in Preview: File ▸ Export) and try again.")
        if doc.page_count == 0:
            raise BookError("This PDF has no pages.")
        return doc

    # ------------------------------------------------------------------ analysis
    def analyse(self, doc) -> dict:
        n = doc.page_count
        idx = sorted(set(list(range(min(n, 12))) + [int(n * k / 20) for k in range(20)]))
        texts, scanned, chars = [], 0, 0
        for i in idx:
            pg = doc[i]
            t = pg.get_text()
            texts.append(t)
            c = len(t.strip())
            chars += c
            if c < 40:
                area = pg.rect.width * pg.rect.height
                img_area = sum(abs(b["bbox"][2] - b["bbox"][0]) * abs(b["bbox"][3] - b["bbox"][1]) for b in pg.get_image_info())
                if img_area > area * 0.5:
                    scanned += 1
        all_text = "\n".join(texts)
        kn_chars = len(KANNADA_RE.findall(all_text))
        legacy = looks_like_legacy_kannada(all_text)
        q = kannada_quality(all_text) if kn_chars > 100 else 1.0
        fonts = " ".join(f[3] for i in idx[:6] for f in doc[i].get_fonts())
        nudi = bool(re.search(r"(?i)nudi|baraha|brh|kedage|mallige|sampige|akshara", fonts))
        kannada_hint = kn_chars > 100 or legacy or nudi or bool(KANNADA_RE.search(os.path.basename(self.path)))
        broken = legacy or nudi and kn_chars < 50 or q < 0.9 or pua_ratio(all_text) > 0.05 or \
            all_text.count("�") > max(20, len(all_text) * 0.01) or "(cid:" in all_text
        tl = [l for l in all_text.splitlines() if len(l.strip()) > 20]
        explicit_spaces = bool(tl) and sum(1 for l in tl if " " in l.strip()) / len(tl) > 0.6
        return {"explicit_spaces": explicit_spaces, "pages": n, "scanned_ratio": scanned / max(1, len(idx)), "chars": chars, "kannada": kannada_hint,
                "legacy": legacy or nudi, "quality": q, "broken": broken, "sample_pages": idx}

    def run(self) -> tuple[Epub, dict]:
        doc = self.open()
        info = self.analyse(doc)
        meta = doc.metadata or {}
        ftitle, fauthor = parse_filename(os.path.splitext(os.path.basename(self.path))[0])
        title = meta.get("title") if good_meta(meta.get("title", "")) and len(meta.get("title", "")) > 3 else ftitle
        author = meta.get("author") if good_meta(meta.get("author", "")) else fauthor
        lang = "kn" if info["kannada"] else "en"
        b = EpubBuilder(normalize(title.strip()), normalize((author or "").strip()), lang)
        self.info = info
        self.explicit_spaces = info["explicit_spaces"]

        mode = self.o.pdf_mode
        if mode == "auto":
            if info["scanned_ratio"] > 0.5 or info["broken"] or info["chars"] < 200:
                mode = "ocr" if self._ocr_usable(doc, info) else "imgflow"
            else:
                mode = "reflow"
        elif mode == "reflow" and (info["scanned_ratio"] > 0.5 or info["chars"] < 200):
            mode = "ocr" if self._ocr_usable(doc, info) else "imgflow"
            self.warnings.append("This PDF has no usable text layer, so text reflow was not possible.")
        if info["legacy"] and mode in ("reflow",):
            self.warnings.append("This Kannada PDF uses a legacy (Nudi/Baraha) font; its text will look garbled.")
        why = ""
        if info["legacy"]:
            why = " (legacy Nudi/Baraha Kannada font)"
        elif info["broken"]:
            why = " (text layer is broken)"
        elif info["scanned_ratio"] > 0.5:
            why = " (scanned book)"
        self.method = {"reflow": "Text reflow", "ocr": "OCR", "imgflow": "Image reflow", "pages": "Page images"}[mode] + why
        self.log(f"PDF method: {self.method}")

        if info["kannada"]:
            b.lang = "kn"
        self._cover(doc, b, mode)
        if mode == "reflow":
            self._reflow(doc, b)
        elif mode == "ocr":
            self._ocr(doc, b)
        elif mode == "imgflow":
            self._imgflow(doc, b)
        else:
            self._pages(doc, b)
        return b.build(), {"method": self.method, "warnings": self.warnings}

    # ------------------------------------------------------------------ cover
    def _cover(self, doc, b: EpubBuilder, mode):
        pg = doc[0]
        text = pg.get_text().strip()
        imgs = pg.get_image_info()
        area = pg.rect.width * pg.rect.height
        img_area = sum(abs(i["bbox"][2] - i["bbox"][0]) * abs(i["bbox"][3] - i["bbox"][1]) for i in imgs)
        if len(text) < 400 or img_area > area * 0.4 or mode in ("pages", "imgflow", "ocr"):
            im = self.render(pg, self.dev.width * 2)
            im, _ = imglib.process(im, self.dev.width, self.dev.height, self.o.grayscale, False)
            data, mt = imglib.encode(im, max(75, self.o.jpeg_quality))
            b.set_cover(data, mt)
            self.cover_page = 0
        else:
            self.cover_page = None

    @staticmethod
    def render(page, target_w: int, clip=None) -> Image.Image:
        rect = clip or page.rect
        zoom = max(0.2, min(6.0, target_w / max(1, rect.width)))
        import pymupdf
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, colorspace=pymupdf.csGRAY, alpha=False)
        return Image.frombytes("L", (pix.width, pix.height), pix.samples)

    # ------------------------------------------------------------------ chapters
    def chapter_plan(self, doc) -> list[tuple[int, str, int]]:
        """[(start_page, title, level)] from PDF bookmarks."""
        toc = []
        try:
            toc = doc.get_toc(simple=True)
        except Exception:
            pass
        toc = [(l, normalize(" ".join(t.split())), p - 1) for l, t, p in toc if 1 <= p <= doc.page_count and t.strip()]
        return toc

    def split_points(self, doc, toc) -> list[tuple[int, str]]:
        if toc:
            lvl1 = [e for e in toc if e[0] == 1]
            use = lvl1 if len(lvl1) >= 3 else [e for e in toc if e[0] <= 2]
            pts = sorted({p: t for _, t, p in reversed(use)}.items())
            if pts and pts[0][0] > 0:
                pts.insert(0, (0, "Front matter"))
            return pts
        n = doc.page_count
        return [(p, f"Pages {p + 1}–{min(p + 20, n)}") for p in range(0, n, 20)]

    # ------------------------------------------------------------------ text reflow
    def _page_lines(self, page):
        """Lines and figure regions of one page, in reading order."""
        import pymupdf
        W, Hh = page.rect.width, page.rect.height
        d = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_PRESERVE_WHITESPACE |
                          pymupdf.TEXT_MEDIABOX_CLIP)
        lines = []
        for blk in d["blocks"]:
            if blk.get("type") != 0:
                continue
            for ln in blk["lines"]:
                if abs(ln["dir"][1]) > 0.2:
                    continue
                spans = [s for s in ln["spans"] if s["text"].strip()]
                if not spans:
                    continue
                x0 = min(s["bbox"][0] for s in spans)
                y0 = min(s["bbox"][1] for s in spans)
                x1 = max(s["bbox"][2] for s in spans)
                y1 = max(s["bbox"][3] for s in spans)
                lines.append({"bbox": [x0, y0, x1, y1], "spans": spans,
                              "size": max(s["size"] for s in spans), "page": page.number})
        # merge fragments that sit on the same baseline (fixes words split mid-way in Word/OCR PDFs)
        lines.sort(key=lambda l: (round(l["bbox"][1] / 2), l["bbox"][0]))
        merged = []
        for ln in lines:
            if merged:
                pv = merged[-1]
                ov = min(pv["bbox"][3], ln["bbox"][3]) - max(pv["bbox"][1], ln["bbox"][1])
                h = min(pv["bbox"][3] - pv["bbox"][1], ln["bbox"][3] - ln["bbox"][1])
                gap = ln["bbox"][0] - pv["bbox"][2]
                sz = max(pv["size"], ln["size"])
                crosses_mid = pv["bbox"][2] < W / 2 < ln["bbox"][0]
                if h > 0 and ov > 0.5 * h and -2 < gap < (1.2 if crosses_mid else 3.0) * sz:
                    if not getattr(self, "explicit_spaces", False) and gap > 0.18 * ln["size"] and \
                            not pv["spans"][-1]["text"].endswith(" ") and not ln["spans"][0]["text"].startswith(" "):
                        pv["spans"][-1] = dict(pv["spans"][-1], text=pv["spans"][-1]["text"] + " ")
                    pv["spans"].extend(ln["spans"])
                    pv["bbox"] = [min(pv["bbox"][0], ln["bbox"][0]), min(pv["bbox"][1], ln["bbox"][1]),
                                  max(pv["bbox"][2], ln["bbox"][2]), max(pv["bbox"][3], ln["bbox"][3])]
                    pv["size"] = max(pv["size"], ln["size"])
                    continue
            merged.append(ln)
        for ln in merged:
            ln["text"] = "".join(s["text"] for s in ln["spans"])

        figs = self._figures(page, merged)
        # drop text that lives inside a figure (labels are in the picture)
        def inside(l, f):
            cx, cy = (l["bbox"][0] + l["bbox"][2]) / 2, (l["bbox"][1] + l["bbox"][3]) / 2
            return f[0] <= cx <= f[2] and f[1] <= cy <= f[3]
        merged = [l for l in merged if not any(inside(l, f) for f in figs)]
        items = [dict(l, kind="line") for l in merged] + [{"kind": "fig", "bbox": list(f), "page": page.number} for f in figs]
        return self._reading_order(items, W)

    def _figures(self, page, lines):
        W, Hh = page.rect.width, page.rect.height
        area = W * Hh
        rects = []
        for info in page.get_image_info():
            x0, y0, x1, y1 = info["bbox"]
            x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(Hh, y1)
            w, h = x1 - x0, y1 - y0
            if w < 30 or h < 30 or w * h < area * 0.01:
                continue
            if w * h > area * 0.85 and len(lines) > 8:
                continue  # page-sized background behind real text
            rects.append([x0, y0, x1, y1])
        try:
            clusters = page.cluster_drawings(x_tolerance=6, y_tolerance=6)
        except Exception:
            clusters = []
        for r in clusters:
            w, h = r.width, r.height
            if w < 40 or h < 30 or w * h < area * 0.02 or w * h > area * 0.9:
                continue
            # a box drawn around ordinary paragraphs (sidebar) is not a figure
            inner = [l for l in lines if r.x0 <= l["bbox"][0] and l["bbox"][2] <= r.x1 and r.y0 <= l["bbox"][1] and l["bbox"][3] <= r.y1]
            total_chars = sum(len(l["text"]) for l in lines) or 1
            if sum(len(l["text"]) for l in inner) > 0.5 * total_chars and total_chars > 150:
                continue  # page border / frame around the text: decoration, not a figure
            if len(inner) > 6 and sum(len(l["text"]) for l in inner) > 400 and not self._grid_like(page, r):
                continue
            rects.append([r.x0, r.y0, r.x1, r.y1])
        # merge overlapping rects
        rects.sort()
        out = []
        for r in rects:
            for o in out:
                if r[0] < o[2] + 4 and r[2] > o[0] - 4 and r[1] < o[3] + 4 and r[3] > o[1] - 4:
                    o[0], o[1], o[2], o[3] = min(o[0], r[0]), min(o[1], r[1]), max(o[2], r[2]), max(o[3], r[3])
                    break
            else:
                out.append(list(r))
        return out

    @staticmethod
    def _grid_like(page, r):
        """Tables: many horizontal/vertical rules inside the region."""
        try:
            n = 0
            for d in page.get_drawings():
                rr = d["rect"]
                if rr.x0 >= r.x0 - 1 and rr.x1 <= r.x1 + 1 and rr.y0 >= r.y0 - 1 and rr.y1 <= r.y1 + 1:
                    if rr.height < 2 or rr.width < 2:
                        n += 1
            return n >= 6
        except Exception:
            return False

    @staticmethod
    def _reading_order(items, W):
        if not items:
            return items
        xs0 = min(i["bbox"][0] for i in items)
        xs1 = max(i["bbox"][2] for i in items)
        mid = (xs0 + xs1) / 2
        tol = (xs1 - xs0) * 0.03
        left = [i for i in items if i["bbox"][2] <= mid + tol]
        right = [i for i in items if i["bbox"][0] >= mid - tol]
        span = [i for i in items if i not in left and i not in right]
        lines = [i for i in items if i["kind"] == "line"]
        two_col = len([i for i in left if i["kind"] == "line"]) >= 6 and len([i for i in right if i["kind"] == "line"]) >= 6 \
            and len([i for i in span if i["kind"] == "line"]) < 0.3 * max(1, len(lines))
        if not two_col:
            return sorted(items, key=lambda i: (i["bbox"][1], i["bbox"][0]))
        out = []
        span.sort(key=lambda i: i["bbox"][1])
        bounds = [i["bbox"][1] for i in span] + [1e9]
        prev = -1e9
        for k, b in enumerate(bounds):
            band_l = sorted([i for i in left if prev <= i["bbox"][1] < b], key=lambda i: i["bbox"][1])
            band_r = sorted([i for i in right if prev <= i["bbox"][1] < b], key=lambda i: i["bbox"][1])
            out.extend(band_l + band_r)
            if k < len(span):
                out.append(span[k])
            prev = b
        return out

    def _furniture(self, doc, pages_items):
        """Running heads / page numbers: text repeated near the top/bottom of many pages."""
        cnt = Counter()
        n = 0
        for p, items in pages_items.items():
            Hh = doc[p].rect.height
            n += 1
            for it in items:
                if it["kind"] != "line":
                    continue
                y = it["bbox"][1]
                if y < Hh * 0.08 or it["bbox"][3] > Hh * 0.92:
                    cnt[self._norm_furn(it["text"])] += 1
        thresh = max(3, int(n * 0.2))
        return {k for k, v in cnt.items() if v >= thresh and k}

    @staticmethod
    def _norm_furn(t):
        t = re.sub(r"\d+", "#", t.lower())
        t = re.sub(r"\b[ivxlc]+\b", "#", t)
        return " ".join(t.split())

    def _reflow(self, doc, b: EpubBuilder):
        n = doc.page_count
        start = 1 if self.cover_page == 0 else 0
        pages_items = {}
        for p in range(start, n):
            self.step("Reading PDF text", (p + 1) / n * 0.7)
            try:
                pages_items[p] = self._page_lines(doc[p])
            except Exception as e:
                self.warnings.append(f"Page {p + 1} could not be read ({e}); it was rendered as an image.")
                pages_items[p] = [{"kind": "fig", "bbox": list(doc[p].rect), "page": p}]
        furn = self._furniture(doc, pages_items)
        sizes = Counter()
        for items in pages_items.values():
            for it in items:
                if it["kind"] == "line":
                    sizes[round(it["size"] * 2) / 2] += len(it["text"])
        body = sizes.most_common(1)[0][0] if sizes else 11
        heading_sizes = sorted({s for s in sizes if s >= body * 1.18 and sizes[s] < sizes[body] * 0.5}, reverse=True)[:3]
        kn = self.info["kannada"]

        def hlevel(sz):
            for k, s in enumerate(heading_sizes):
                if sz >= s - 0.25:
                    return k + 1
            return 0

        # build a flat list of blocks: ("h", lvl, html, page) | ("p", html, page) | ("pre", text, page) | ("fig", href, page)
        blocks = []
        cur = None  # current paragraph: dict(text_parts, page, last_line)
        line_gaps = []

        def flush():
            nonlocal cur
            if cur:
                html = self._spans_html(cur["spans"], kn)
                if html.strip():
                    if cur.get("mono"):
                        blocks.append(("pre", "\n".join(cur["raw"]), cur["page"]))
                    elif cur.get("h"):
                        blocks.append(("h", cur["h"], html, cur["page"]))
                    else:
                        blocks.append(("p", html, cur["page"]))
            cur = None

        for p in sorted(pages_items):
            Hh = doc[p].rect.height
            items = [it for it in pages_items[p] if not (it["kind"] == "line" and self._norm_furn(it["text"]) in furn
                     and (it["bbox"][1] < Hh * 0.08 or it["bbox"][3] > Hh * 0.92))]
            items = [it for it in items if not (it["kind"] == "line" and re.fullmatch(r"\s*[\divxlcIVXLC\-–—.]{1,6}\s*", it["text"])
                                               and (it["bbox"][1] < Hh * 0.1 or it["bbox"][3] > Hh * 0.9))]
            lines = [i for i in items if i["kind"] == "line"]
            if lines:
                col_x0 = statistics.median(sorted(l["bbox"][0] for l in lines)[: max(1, len(lines) // 3)])
                col_x1 = max(l["bbox"][2] for l in lines)
            else:
                col_x0 = col_x1 = 0
            prev = None
            for it in items:
                if it["kind"] == "fig":
                    flush()
                    href = self._fig(doc[p], it["bbox"], b)
                    if href:
                        blocks.append(("fig", href, p))
                    prev = None
                    continue
                text = it["text"]
                sz = round(it["size"] * 2) / 2
                lvl = hlevel(sz) if len(text) < 160 else 0
                mono = all(s["flags"] & 8 or re.search(r"(?i)mono|courier|consol|menlo", s["font"]) for s in it["spans"])
                if prev is not None:
                    gap = it["bbox"][1] - prev["bbox"][3]
                    lh = prev["bbox"][3] - prev["bbox"][1]
                else:
                    gap, lh = 0, 0
                new_para = (cur is None or prev is None or lvl != (cur.get("h") or 0) or bool(mono) != bool(cur.get("mono"))
                            or gap > max(2.0, lh * 0.55)
                            or (it["bbox"][0] - col_x0 > it["size"] * 0.9 and not mono and it["bbox"][0] < (col_x0 + col_x1) / 2)
                            or (prev["bbox"][2] < col_x1 - it["size"] * 4 and re.search(r"[.!?:;\"”’)।॥]\s*$", prev["text"]))
                            or re.match(r"^\s*(?:[•●▪■◦\-–]|\d{1,3}[.)])\s", text)
                            or it["bbox"][0] > (col_x0 + col_x1) / 2 or prev["bbox"][0] > (col_x0 + col_x1) / 2)
                if prev is not None and gap > 0 and lh > 0 and not new_para:
                    line_gaps.append(gap / lh)
                # a paragraph that ran over a page break continues if it did not end a sentence
                if prev is None and cur is not None and not cur.get("h") and not lvl and not mono:
                    last = cur["raw"][-1] if cur["raw"] else ""
                    starts_lower = text[:1].islower() or (kn and not re.match(r"^\s*[\d“\"(]", text))
                    if not re.search(r"[.!?:\"”’)।॥]\s*$", last) and (starts_lower or not re.match(r"^\s*[A-Z0-9“\"]", text)) \
                            and it["bbox"][0] - col_x0 < it["size"] * 0.9:
                        new_para = False
                    else:
                        new_para = True
                if new_para:
                    flush()
                    cur = {"spans": [], "raw": [], "page": p, "h": lvl, "mono": mono}
                self._append_line(cur, it, kn)
                prev = it
            # page boundary: keep cur open for continuation
        flush()
        self._write_chapters(doc, b, blocks)

    @staticmethod
    def _append_line(cur, line, kn):
        spans = [dict(s) for s in line["spans"]]
        if cur["spans"]:
            last = cur["spans"][-1]
            joined = dehyphenate_join(last["text"], spans[0]["text"]) if not kn else None
            if cur.get("mono"):
                pass
            elif joined is not None and joined != last["text"].rstrip() + " " + spans[0]["text"].lstrip() and \
                    joined == last["text"].rstrip()[:-1] + spans[0]["text"].lstrip():
                last["text"] = last["text"].rstrip()[:-1]
                spans[0]["text"] = spans[0]["text"].lstrip()
            else:
                last["text"] = last["text"].rstrip() + " "
                spans[0]["text"] = spans[0]["text"].lstrip()
        cur["spans"].extend(spans)
        cur["raw"].append(line["text"])

    @staticmethod
    def _spans_html(spans, kn):
        out = []
        prev_style = None
        buf = ""
        for s in spans:
            bold = bool(s["flags"] & 16) or bool(re.search(r"(?i)bold|black|heavy|semibold", s["font"]))
            ital = bool(s["flags"] & 2) or bool(re.search(r"(?i)italic|oblique", s["font"]))
            sup = bool(s["flags"] & 1) and len(s["text"].strip()) <= 4
            style = (bold, ital, sup)
            if style != prev_style and buf:
                out.append((prev_style, buf))
                buf = ""
            buf += s["text"]
            prev_style = style
        if buf:
            out.append((prev_style, buf))
        html = []
        all_bold = all(st[0] for st, _ in out) if out else False
        for (bold, ital, sup), t in out:
            if kn:
                t = dedupe_kannada(t)
            t = esc(cleanup_text(t, kn))
            if sup:
                t = f"<sup>{t}</sup>"
            if ital:
                t = f"<i>{t}</i>"
            if bold and not all_bold:
                t = f"<b>{t}</b>"
            html.append(t)
        res = "".join(html).strip()
        if all_bold and res:
            res = f"<b>{res}</b>"
        return re.sub(r"\s{2,}", " ", res)

    def _fig(self, page, bbox, b: EpubBuilder):
        import pymupdf
        r = pymupdf.Rect(bbox) & page.rect
        if r.is_empty or r.width < 5 or r.height < 5:
            return None
        im = self.render(page, min(self.dev.width * 2, int(r.width * 4)), clip=r)
        if imglib.is_blank(im):
            return None
        im, la = imglib.process(im, self.dev.width, self.dev.height, self.o.grayscale, self.o.contrast)
        data, mt = imglib.encode(im, self.o.jpeg_quality, prefer_png=la)
        return b.add_image(data, mt)

    def _write_chapters(self, doc, b: EpubBuilder, blocks):
        toc = self.chapter_plan(doc)
        splits = self.split_points(doc, toc)
        if not toc:
            # no bookmarks: split at top-level headings, fall back to every 20 pages
            hs = [(blk[-1], re.sub(r"<[^>]+>", "", blk[2])) for blk in blocks if blk[0] == "h" and blk[1] == 1]
            if 2 <= len(hs) <= 400:
                splits = sorted({p: t for p, t in reversed(hs)}.items())
                if splits[0][0] > (blocks[0][-1] if blocks else 0):
                    splits.insert(0, (blocks[0][-1], ""))
        starts = [p for p, _ in splits] or [0]
        chapters = [[] for _ in starts]
        for blk in blocks:
            p = blk[-1]
            k = max(0, max((j for j, s in enumerate(starts) if s <= p), default=0))
            chapters[k].append(blk)
        # write, splitting oversize chapters (keeps page turns fast on the reader)
        file_of_page = {}
        page_anchor_done = set()
        n = 0
        for (sp, title), chunk in zip(splits or [(0, "")], chapters):
            if not chunk:
                continue
            parts, cur, size = [], [], 0
            for blk in chunk:
                cur.append(blk)
                size += len(blk[1] if blk[0] != "h" else blk[2])
                if size > 120_000 and blk[0] == "p":
                    parts.append(cur)
                    cur, size = [], 0
            if cur:
                parts.append(cur)
            for k, part in enumerate(parts):
                n += 1
                href = f"ch{n:03d}.xhtml"
                html = []
                for blk in part:
                    pg = blk[-1]
                    if pg not in page_anchor_done:
                        html.append(f'<a id="pg{pg + 1}"></a>')
                        page_anchor_done.add(pg)
                        file_of_page[pg] = href
                    if blk[0] == "h":
                        lv = min(3, blk[1])
                        html.append(f'<h{lv} class="pdf-h{lv}">{blk[2]}</h{lv}>')
                    elif blk[0] == "p":
                        cls = "pdf-p"
                        if html and html[-1].startswith("<h"):
                            cls += " pdf-first"
                        html.append(f'<p class="{cls}">{blk[1]}</p>')
                    elif blk[0] == "pre":
                        html.append(f"<pre>{esc(blk[1])}</pre>")
                    else:
                        html.append(f'<div class="pdf-fig"><img src="../{blk[1]}" alt=""/></div>')
                b.page(href, title or b.title, "".join(html))
                if k == 0 and title:
                    b.toc.append((1, title, f"Text/{href}"))
        # deeper bookmark levels point at page anchors
        if toc:
            b.toc = []
            for lvl, t, p in toc:
                q = p
                while q >= 0 and q not in file_of_page:
                    q -= 1
                if q >= 0:
                    b.toc.append((lvl, t, f"Text/{file_of_page[q]}#pg{q + 1}"))
        if n == 0:
            raise BookError("No readable text was found in this PDF.")

    # ------------------------------------------------------------------ OCR
    def _ocr_langs(self, info) -> str | None:
        have = tesseract_langs()
        if not have:
            return None
        if info["kannada"] or (not info["chars"] and "kan" in have):
            # Kannada book, or a scan whose language is still unknown: try Kannada+English
            return "kan+eng" if "kan" in have and "eng" in have else ("kan" if "kan" in have else None)
        return "eng" if "eng" in have else None

    def _ocr_usable(self, doc, info) -> bool:
        lang = self._ocr_langs(info)
        if not lang:
            if info["kannada"]:
                self.warnings.append("Kannada OCR data is missing, so pages were re-flowed as images.")
            return False
        # trial on a few content pages: OCR must be confident
        n = doc.page_count
        pages = sorted({min(n - 1, int(n * f)) for f in (0.3, 0.5, 0.7)}) if n > 6 else list(range(n))[:3]
        confs, texts = [], []
        for p in pages:
            text, conf = self._ocr_page(doc[p], lang)
            texts.append(text)
            if conf is not None:
                confs.append(conf)
        trial = " ".join(texts)
        kn, la = len(KANNADA_RE.findall(trial)), len(re.findall(r"[A-Za-z]", trial))
        if "kan" in lang and not info["kannada"]:
            if kn > 40 and kn >= la * 0.5:
                info["kannada"] = True          # scanned Kannada book
                self.log("OCR trial: Kannada text detected")
            elif "eng" in tesseract_langs():
                lang = "eng"                     # English scan: English-only model is more accurate
                confs = [c for c in (self._ocr_page(doc[p], lang)[1] for p in pages) if c is not None]
        self.log(f"OCR trial: {kn} Kannada vs {la} Latin letters, lang={lang}")
        if "kan" in lang and info["kannada"] and la < kn * 0.25:
            lang = "kan"                         # almost no English: Kannada-only model avoids Latin misreads
        ok = bool(confs) and statistics.mean(confs) >= (65 if info["kannada"] else 75)
        if not ok:
            self.warnings.append("OCR could not read this scan reliably, so pages were re-flowed as images.")
        self.ocr_lang = lang
        return ok

    def _ocr_page(self, page, lang):
        im = self.render(page, int(page.rect.width * 300 / 72))
        with tempfile.TemporaryDirectory(dir=_ocr_tmp()) as td:
            ip = os.path.join(td, "p.png")
            im.save(ip)
            base = os.path.join(td, "out")
            try:
                subprocess.run([tesseract_path(), ip, base, "-l", lang, "--psm", "3", "txt", "tsv"],
                               capture_output=True, timeout=180, env=tesseract_env())
                text = open(base + ".txt", encoding="utf-8", errors="replace").read()
                confs = []
                for row in open(base + ".tsv", encoding="utf-8", errors="replace").read().splitlines()[1:]:
                    c = row.split("\t")
                    if len(c) >= 12 and c[11].strip():
                        try:
                            v = float(c[10])
                            if v >= 0:
                                confs.append(v)
                        except ValueError:
                            pass
                return text, (statistics.mean(confs) if confs else None)
            except Exception:
                return "", None

    def _ocr(self, doc, b: EpubBuilder):
        n = doc.page_count
        start = 1 if self.cover_page == 0 else 0
        kn = self.info["kannada"]
        results = {}

        def work(p):
            text, _ = self._ocr_page(doc[p], self.ocr_lang)
            return p, text
        # PyMuPDF documents are not thread-safe: render serially, OCR in parallel
        with ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 4) // 2)) as ex:
            futs = []
            for p in range(start, n):
                im = self.render(doc[p], int(doc[p].rect.width * 300 / 72))
                futs.append(ex.submit(self._ocr_image, p, im, self.ocr_lang))
                self.step("Rendering pages for OCR", (p + 1) / n * 0.3)
            for k, f in enumerate(futs):
                p, text = f.result()
                results[p] = text
                self.step(f"OCR page {k + 1}/{len(futs)}", 0.3 + 0.7 * (k + 1) / len(futs))
        blocks = []
        carry = None
        for p in sorted(results):
            text = results[p]
            paras = [x for x in re.split(r"\n\s*\n", text) if x.strip()]
            if len(text.strip()) < 40:
                href = self._fig(doc[p], list(doc[p].rect), b)
                if href:
                    blocks.append(("fig", href, p))
                continue
            all_lines = [l.strip() for l in text.splitlines() if l.strip()]
            page_w = max((len(l) for l in all_lines), default=1)
            # a page of short lines is poetry: every stanza keeps its line breaks
            page_verse = len(all_lines) >= 4 and statistics.median(len(l) for l in all_lines) < (30 if kn else 38)
            for k, para in enumerate(paras):
                lines = [l.strip() for l in para.splitlines() if l.strip()]
                lines = [l for l in lines if not _SCAN_JUNK.search(l)]
                if kn:
                    lines = [_drop_latin_noise(l) for l in lines]
                    lines = [l for l in lines if l.strip()]
                if not lines:
                    continue
                if re.fullmatch(r"[\d\-–—.ivxlc ]{1,8}", " ".join(lines), re.I):
                    continue  # page number
                # verse: several short lines (well under the page's full line length) keep their breaks
                punct_end = sum(1 for l in lines if re.search(r"[,;:!?.।॥]$", l)) / len(lines)
                verse = len(lines) >= 2 and (page_verse or statistics.median(len(l) for l in lines) < max(18, page_w * 0.62)
                                             or (len(lines) >= 3 and punct_end >= 0.6))
                if verse:
                    html = "<br/>".join(esc(cleanup_text(l, kn)) for l in lines)
                    blocks.append(("p", html, p))
                    continue
                joined = lines[0]
                for l in lines[1:]:
                    joined = dehyphenate_join(joined, l) if not kn else joined + " " + l
                joined = cleanup_text(joined, kn)
                if k == 0 and carry is not None and len(lines) > 1 and not page_verse and \
                        not re.search(r"[.!?:\"”।॥]$", blocks[carry][1]):
                    blocks[carry] = ("p", blocks[carry][1] + " " + esc(joined), blocks[carry][2])
                    continue
                if len(lines) == 1 and len(joined) < 60 and not re.search(r"[.,;:!?]$", joined) and \
                        (joined[:1].isupper() or kn and k == 0) and not (page_verse and k > 0):
                    blocks.append(("h", 2, esc(joined), p))
                else:
                    blocks.append(("p", esc(joined), p))
            carry = len(blocks) - 1 if blocks and blocks[-1][0] == "p" else None
        self._write_chapters(doc, b, blocks)

    def _ocr_image(self, p, im, lang):
        with tempfile.TemporaryDirectory(dir=_ocr_tmp()) as td:
            ip = os.path.join(td, "p.png")
            im.save(ip)
            base = os.path.join(td, "out")
            try:
                subprocess.run([tesseract_path(), ip, base, "-l", lang, "--psm", "3"], capture_output=True, timeout=300, env=tesseract_env())
                return p, open(base + ".txt", encoding="utf-8", errors="replace").read()
            except Exception:
                return p, ""

    # ------------------------------------------------------------------ image reflow
    def _imgflow(self, doc, b: EpubBuilder):
        from .imgreflow import reflow_page
        n = doc.page_count
        start = 1 if self.cover_page == 0 else 0
        target_w = self.dev.width - 16
        target_xh = 30 if self.info["kannada"] else 26   # px line height after re-wrapping
        chunks_per_file, html, files = 0, [], 0
        toc = self.chapter_plan(doc)
        file_of_page = {}

        def flush():
            nonlocal html, files, chunks_per_file
            if html:
                files += 1
                b.page(f"pg{files:04d}.xhtml", b.title, "".join(html))
            html, chunks_per_file = [], 0

        for p in range(start, n):
            self.step(f"Re-flowing page {p + 1}/{n}", (p + 1) / n)
            page = doc[p]
            try:
                im = self.render(page, int(page.rect.width * 200 / 72))
                pieces = reflow_page(im, target_w, target_xh, self.dev.height)
            except Exception as e:
                self.warnings.append(f"Page {p + 1}: {e}")
                pieces = None
            if pieces is None:   # not text-like (photo / full illustration): keep the page whole
                im = imglib.trim_margins(self.render(page, self.dev.width * 2))
                im, _ = imglib.process(im, self.dev.width, self.dev.height, self.o.grayscale, self.o.contrast)
                pieces = [("img", im)]
            if p not in file_of_page:
                file_of_page[p] = f"pg{files + 1:04d}.xhtml"
                html.append(f'<a id="pg{p + 1}"></a>')
            for kind, im in pieces:
                if kind == "break":
                    continue
                if kind == "text":
                    data, mt = imglib.encode_4level(im)
                else:
                    im, la = imglib.process(im, self.dev.width, self.dev.height, self.o.grayscale, self.o.contrast)
                    data, mt = imglib.encode(im, self.o.jpeg_quality, prefer_png=la)
                href = b.add_image(data, mt)
                cls = "kn-img" if kind == "text" else "pdf-fig"
                html.append(f'<div class="{cls}"><img src="../{href}" alt=""/></div>')
                chunks_per_file += 1
            if chunks_per_file > 60:
                flush()
        flush()
        if files == 0:
            raise BookError("Nothing readable was found in this PDF.")
        b.toc = []
        for lvl, t, p in toc:
            q = p
            while q >= start and q not in file_of_page:
                q -= 1
            if q >= start:
                b.toc.append((lvl, t, f"Text/{file_of_page[q]}#pg{q + 1}"))

    # ------------------------------------------------------------------ page images
    def _pages(self, doc, b: EpubBuilder):
        n = doc.page_count
        start = 1 if self.cover_page == 0 else 0
        toc = self.chapter_plan(doc)
        hrefs = {}
        for p in range(start, n):
            self.step(f"Rendering page {p + 1}/{n}", (p + 1) / n)
            page = doc[p]
            im = imglib.trim_margins(self.render(page, self.dev.width * 2.2))
            if self.o.light_novel and im.width > im.height * 1.05:
                im = im.rotate(-90, expand=True)
            im, la = imglib.process(im, self.dev.width, self.dev.height, self.o.grayscale, self.o.contrast)
            data, mt = imglib.encode(im, self.o.jpeg_quality, prefer_png=la)
            href = b.add_image(data, mt, f"p{p + 1:04d}")
            hrefs[p] = b.page(f"p{p + 1:04d}.xhtml", f"Page {p + 1}", f'<div class="ink-page"><img src="../{href}" alt="Page {p + 1}"/></div>')
        b.toc = []
        for lvl, t, p in toc:
            q = max(start, p)
            b.toc.append((lvl, t, f"Text/p{q + 1:04d}.xhtml"))
        if not b.toc:
            b.toc = [(1, b.title, f"Text/p{start + 1:04d}.xhtml")]
