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


_LEADER_LINE = re.compile(r"(?:\.\s?){5,}|…{3,}|·{4,}|_{5,}")
_CLEAN_EN_WORD = re.compile(r"(?<![\w\u00c0-\u00ff])[A-Za-z][a-z]{2,}(?![\w\u00c0-\u00ff])")
_CHAPTER_RE = re.compile(r"(?i)^\s*(?:part|chapter|schedule|book|appendix|section|ಭಾಗ|ಅಧ್ಯಾಯ|ಅನುಸೂಚಿ|ಪರಿಚ್ಛೇದ|ಪ್ರಕರಣ)\b")
_ARTICLE_START = re.compile(r"^\s*(?:\d{1,3}[A-Zಎಬಸಡ]{0,2}\s*[.)]|\(\d{1,2}\)|\([a-zಅ-ಹ][\u0CBE-\u0CCD]?\)|\([ivx]{1,4}\))")
_OCR_JUNK_LINE = re.compile(r"^[\W\d೦-೯_]{0,4}$|([\u0CBE-\u0CCD])\1{2,}|(?:[\u0CBE-\u0CCD]{4,})")


def _strip_leader_spans(line: dict):
    """'Name of the Union ........ 2' -> 'Name of the Union' (contents pages)."""
    full = "".join(s["text"] for s in line["spans"])
    m = _LEADER_LINE.search(full)
    cut = m.start() if m else len(full)
    out, pos = [], 0
    for s in line["spans"]:
        t = s["text"]
        if pos >= cut:
            break
        keep = t[: max(0, cut - pos)]
        if keep.strip():
            out.append(dict(s, text=keep))
        pos += len(t)
    line["spans"] = out or [dict(line["spans"][0], text="")]
    line["text"] = "".join(s["text"] for s in line["spans"]).rstrip()


def _ocr_garbage(t: str, conf: float) -> bool:
    """OCR noise from rules, ornaments and stains: many tiny 'words' or one repeated pattern."""
    toks = t.split()
    core = re.sub(r"[\s\W_]", "", "".join(dict.fromkeys(toks)))   # repeated whole words (ಮತ್ತೆ ಮತ್ತೆ) are fine
    if 6 <= len(core) <= 16 and len(set(core)) / len(core) < (0.45 if len(core) >= 8 else 0.35):
        return True   # short repeated pattern ('೯ಣರಣರಣಣ'); long real lines naturally reuse characters
    if len(toks) >= 3 and sum(len(x) for x in toks) / len(toks) < 2.6 and conf < 85:
        return True
    return False


def _fix_roman(t: str) -> str:
    """'ಭಾಗ - 11' / 'ಅಧ್ಯಾಯ 1V' -> Roman numerals as printed."""
    def fix(m):
        tok = m.group(2).upper().replace("1", "I").replace("L", "I").replace("|", "I").replace("!", "I")
        return m.group(1) + (tok if re.fullmatch(r"[IVXLC]+", tok) else m.group(2))
    return re.sub(r"((?:ಭಾಗ|ಅಧ್ಯಾಯ|PART|CHAPTER)\s*[-–]?\s*)([0-9IVXLCivxlc|!]{1,6})\b", fix, t)


def _norm_ocr_furn(t: str) -> str:
    t = re.sub(r"[\d೦-೯]+", "#", t.lower())
    t = re.sub(r"\b[ivxlc]+\b", "#", t)
    return " ".join(t.split())


def _clean_ocr_line(t: str) -> str:
    """Remove dot leaders, scanner file names and OCR garbage from one recognised line."""
    t = _SCAN_JUNK.sub("", t)
    m = _LEADER_LINE.search(t)
    if m:
        t = t[: m.start()]
    t = re.sub(r"\s*[.,:;]{4,}.*$", "", t)              # leader dots read as , . :
    t = re.sub(r"(?:\s+[\d೦-೯]{1,4}[\-–]?){1,}\s*$", lambda m: "" if len(m.group(0).split()) >= 2 else m.group(0), t)
    t = t.strip(" |_~")
    if not t or _OCR_JUNK_LINE.search(t):
        return ""
    if KANNADA_RE.search(t) and kannada_quality(t) < 0.6:
        return ""
    # digit soup ('2೭ ೦ 24೬'): mostly digits/symbols with few letters
    letters = len(re.findall(r"[A-Za-z\u0C85-\u0CB9]", t))
    if letters < max(2, len(t) * 0.25):
        return ""
    return t


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
        self.plan = self._classify_pages(doc)
        n_scan = sum(1 for k, _ in self.plan.values() if k == "scan")
        if n_scan and n_scan < doc.page_count * 0.5:
            # a few image-only pages in a text book are covers / full-page figures, not scans: keep the picture
            for p_, (k, l) in list(self.plan.items()):
                if k == "scan":
                    self.plan[p_] = ("text", l)
        kinds = Counter(k for k, _ in self.plan.values())
        needs_ocr = kinds["ocr"] + kinds["scan"]
        self.bilingual = False
        if mode == "auto":
            if needs_ocr == 0 and info["chars"] >= 200:
                mode = "reflow"
            elif self._ocr_usable(doc, info) or (kinds["text"] and not kinds["scan"]):
                mode = "hybrid"
            else:
                mode = "imgflow"
        elif mode == "reflow" and (info["scanned_ratio"] > 0.5 or info["chars"] < 200):
            mode = "hybrid" if self._ocr_usable(doc, info) else "imgflow"
            self.warnings.append("This PDF has no usable text layer, so text reflow was not possible.")
        if mode == "hybrid" and not tesseract_path():
            mode = "imgflow"
        why = ""
        if info["legacy"]:
            why = " (legacy Nudi/Baraha Kannada font)"
        elif info["broken"]:
            why = " (text layer is broken)"
        elif info["scanned_ratio"] > 0.5:
            why = " (scanned book)"
        if mode == "hybrid":
            parts = []
            if kinds["text"]:
                parts.append(f"{kinds['text']} text pages")
            if needs_ocr:
                parts.append(f"{needs_ocr} OCR pages")
            label = "OCR" if not kinds["text"] else "Text + OCR"
            self.method = f"{label}{why}"
            self.log("PDF plan: " + ", ".join(parts) + (f", {kinds['contents']} contents pages as images" if kinds["contents"] else ""))
        else:
            self.method = {"reflow": "Text reflow", "imgflow": "Image reflow", "pages": "Page images"}[mode] + why
        self.log(f"PDF method: {self.method}")

        if info["kannada"]:
            b.lang = "kn"
        self._cover(doc, b, "ocr" if mode == "hybrid" else mode)
        if mode == "reflow":
            self._write_chapters(doc, b, self._reflow_blocks(doc, b, self._pages_from_start(doc)))
        elif mode == "hybrid":
            self._hybrid(doc, b)
        elif mode == "imgflow":
            self._imgflow(doc, b)
        else:
            self._pages(doc, b)
        return b.build(), {"method": self.method, "warnings": self.warnings}

    def _pages_from_start(self, doc):
        return list(range(1 if getattr(self, "cover_page", None) == 0 else 0, doc.page_count))

    # ------------------------------------------------------------------ per-page plan
    def _classify_pages(self, doc) -> dict:
        """page -> (kind, lang). kind: text | ocr (bad text layer) | scan (no text) | contents | blank"""
        plan = {}
        self.contents_pages = set()
        for p in range(doc.page_count):
            page = doc[p]
            t = page.get_text()
            s = re.sub(r"\s", "", t)
            leaders = len(_LEADER_LINE.findall(t))
            if len(s) < 30:
                area = page.rect.width * page.rect.height
                img_area = sum(abs(i["bbox"][2] - i["bbox"][0]) * abs(i["bbox"][3] - i["bbox"][1]) for i in page.get_image_info())
                plan[p] = ("scan", "") if img_area > area * 0.5 else ("blank", "")
                continue
            kn = len(KANNADA_RE.findall(t))
            legacy = looks_like_legacy_kannada(t)
            bad = legacy or (kn > 40 and kannada_quality(t) < 0.9) or pua_ratio(t) > 0.05 or \
                t.count("\ufffd") > 10 or "(cid:" in t
            if bad:
                english_words = len(_CLEAN_EN_WORD.findall(t))
                lang = "kan+eng" if english_words >= 12 else "kan"
                plan[p] = ("contents" if leaders >= 4 else "ocr", lang)
            else:
                la = len(re.findall(r"[A-Za-z]", t))
                plan[p] = ("text", "kn" if kn > la else "en")
                if leaders >= 4:
                    self.contents_pages.add(p)
        return plan

    # ------------------------------------------------------------------ hybrid: text pages + OCR pages
    def _hybrid(self, doc, b: EpubBuilder):
        pages = self._pages_from_start(doc)
        text_pages = [p for p in pages if self.plan[p][0] in ("text", "blank")]
        ocr_pages = [p for p in pages if self.plan[p][0] in ("ocr", "scan")]
        img_pages = [p for p in pages if self.plan[p][0] == "contents"]
        blocks = []
        if text_pages:
            blocks += self._reflow_blocks(doc, b, text_pages)
        if ocr_pages:
            blocks += self._ocr_blocks(doc, b, ocr_pages)
        for p in img_pages:
            im = imglib.trim_margins(self.render(doc[p], self.dev.width * 2.2))
            im, la = imglib.process(im, self.dev.width, self.dev.height, self.o.grayscale, self.o.contrast)
            data, mt = imglib.encode(im, self.o.jpeg_quality, prefer_png=la)
            blocks.append(("fig", b.add_image(data, mt), p))
        # page language, for bilingual books
        self.page_lang = {}
        for p in pages:
            kind, lang = self.plan[p]
            self.page_lang[p] = "kn" if lang.startswith("kn") or lang.startswith("kan") else ("en" if lang else "")
        langs = Counter(v for v in self.page_lang.values() if v)
        self.bilingual = len(langs) == 2 and min(langs.values()) >= 0.25 * sum(langs.values())
        if self.bilingual:
            self.log(f"Bilingual book: {langs['kn']} Kannada + {langs['en']} English pages, grouped by language")
        blocks.sort(key=lambda x: x[-1])          # stable: keeps in-page order
        self._write_chapters(doc, b, blocks)

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
                if y < Hh * 0.12 or it["bbox"][3] > Hh * 0.9:
                    cnt[self._norm_furn(it["text"])] += 1
        thresh = max(3, int(n * 0.2))
        return {k for k, v in cnt.items() if v >= thresh and k}

    @staticmethod
    def _norm_furn(t):
        t = re.sub(r"\d+", "#", t.lower())
        t = re.sub(r"\b[ivxlc]+\b", "#", t)
        return " ".join(t.split())

    def _reflow_blocks(self, doc, b: EpubBuilder, pages) -> list:
        n = doc.page_count
        pages_items = {}
        for k, p in enumerate(pages):
            self.step("Reading PDF text", (k + 1) / len(pages) * 0.5)
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
        heading_sizes = sorted({s for s in sizes if s >= body * 1.15 and sizes[s] < sizes[body] * 0.5}, reverse=True)[:5]
        kn = self.info["kannada"] and not getattr(self, "plan", None) or \
            bool(getattr(self, "plan", None)) and sum(1 for p in pages if self.plan[p][1] == "kn") > len(pages) / 2

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
                    elif cur.get("fn"):
                        blocks.append(("fn", html, cur["page"]))
                    else:
                        blocks.append(("p", html, cur["page"]))
            cur = None

        for p in sorted(pages_items):
            Hh = doc[p].rect.height
            items = [it for it in pages_items[p] if not (it["kind"] == "line" and self._norm_furn(it["text"]) in furn
                     and (it["bbox"][1] < Hh * 0.12 or it["bbox"][3] > Hh * 0.9))]
            items = [it for it in items if not (it["kind"] == "line" and re.fullmatch(r"\s*[\divxlcIVXLC\-–—.]{1,6}\s*", it["text"])
                                               and (it["bbox"][1] < Hh * 0.12 or it["bbox"][3] > Hh * 0.9))]
            contents = p in getattr(self, "contents_pages", set())
            if contents:   # page numbers of a printed contents list mean nothing in an EPUB
                items = [it for it in items if not (it["kind"] == "line" and
                                                    re.fullmatch(r"\s*(?:PAGES?|ಪುಟ(?:ಗಳು)?)?\s*[\d೦-೯ivxlcIVXLC\-–, ]*\s*", it["text"]))]
            lines = [i for i in items if i["kind"] == "line"]
            if lines:
                col_x0 = statistics.median(sorted(l["bbox"][0] for l in lines)[: max(1, len(lines) // 3)])
                col_x1 = max(l["bbox"][2] for l in lines)
            else:
                col_x0 = col_x1 = 0
            ln_only = [i for i in items if i["kind"] == "line"]
            gaps = sorted(g for g in (ln_only[k + 1]["bbox"][1] - ln_only[k]["bbox"][3] for k in range(len(ln_only) - 1)) if g > -2)
            med_gap = gaps[len(gaps) // 3] if gaps else 2.0
            col_mid = (col_x0 + col_x1) / 2
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
                if _LEADER_LINE.search(text):
                    _strip_leader_spans(it)
                    text = it["text"]
                    if not text.strip():
                        continue
                sz = round(it["size"] * 2) / 2
                lvl = hlevel(sz) if len(text) < 160 else 0
                w = it["bbox"][2] - it["bbox"][0]
                centered = abs((it["bbox"][0] + it["bbox"][2]) / 2 - col_mid) < (col_x1 - col_x0) * 0.08 and \
                    w < (col_x1 - col_x0) * 0.7
                all_bold = all(sp["flags"] & 16 or re.search(r"(?i)bold|black|heavy", sp["font"]) for sp in it["spans"])
                if not lvl and len(text.strip()) < 70 and not contents and \
                        (_CHAPTER_RE.match(text) and (all_bold or centered) or centered and all_bold):
                    lvl = 2 if _CHAPTER_RE.match(text) else 3
                fn = sz <= body * 0.88 and it["bbox"][1] > Hh * 0.5 and not lvl
                mono = all(s["flags"] & 8 or re.search(r"(?i)mono|courier|consol|menlo", s["font"]) for s in it["spans"])
                if prev is not None:
                    gap = it["bbox"][1] - prev["bbox"][3]
                    lh = prev["bbox"][3] - prev["bbox"][1]
                else:
                    gap, lh = 0, 0
                new_para = (cur is None or prev is None or lvl != (cur.get("h") or 0) or bool(mono) != bool(cur.get("mono"))
                            or fn != bool(cur.get("fn")) or (fn and re.match(r"^\s*(?:\d{1,3}[.)]|\*)", text))
                            or gap > med_gap * 1.8 + 1.5
                            or (prev is not None and it["bbox"][0] - prev["bbox"][0] > it["size"] * 0.8 and not mono
                                and it["bbox"][0] < (col_x0 + col_x1) / 2 and not contents
                                and (re.search(r"[.:;!?)”\"’—-]\s*$", prev["text"]) or prev["bbox"][2] < col_x1 - it["size"] * 3))
                            or (prev["bbox"][2] < col_x1 - it["size"] * 4 and re.search(r"[.!?:;\"”’)।॥]\s*$", prev["text"]))
                            or re.match(r"^\s*(?:[•●▪■◦\-–]|\d{1,3}[.)])\s", text)
                            or (it["bbox"][0] > (col_x0 + col_x1) / 2 or prev["bbox"][0] > (col_x0 + col_x1) / 2) and not centered)
                if fn and cur is not None and cur.get("fn"):
                    new_para = bool(re.match(r"^\s*(?:\d{1,3}\s*[.)]|\*)", text))   # footnotes break only at a new note
                if contents and cur is not None and prev is not None and not lvl:
                    # contents entry: a new entry starts with a number or a PART/CHAPTER line, else it wraps
                    caps = re.sub(r"[^A-Za-z]", "", text)
                    new_para = bool(re.match(r"^\s*(?:\d{1,3}[A-Z]{0,2}\.|[IVXLC]+\.|PART|CHAPTER|SCHEDULE|APPENDIX|PREAMBLE)", text)) \
                        or bool(cur.get("h")) or (len(caps) > 3 and caps.isupper()) or all_bold
                if prev is not None and gap > 0 and lh > 0 and not new_para:
                    line_gaps.append(gap / lh)
                # a paragraph that ran over a page break continues if it did not end a sentence
                if prev is None and cur is not None and not cur.get("h") and not lvl and not mono and not fn and not cur.get("fn"):
                    last = cur["raw"][-1] if cur["raw"] else ""
                    starts_lower = text[:1].islower() or (kn and not re.match(r"^\s*[\d“\"(]", text))
                    if not re.search(r"[.!?:\"”’)।॥]\s*$", last) and (starts_lower or not re.match(r"^\s*[A-Z0-9“\"]", text)) \
                            and it["bbox"][0] - col_x0 < it["size"] * 0.9:
                        new_para = False
                    else:
                        new_para = True
                if new_para:
                    flush()
                    cur = {"spans": [], "raw": [], "page": p, "h": lvl, "mono": mono, "fn": fn}
                self._append_line(cur, it, kn)
                prev = it
            # page boundary: keep cur open for continuation (footnotes never continue)
            if cur is not None and cur.get("fn"):
                flush()
        flush()
        return blocks

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
        """Split blocks into chapter files and build the table of contents.

        Bilingual books (e.g. a Kannada page facing its English page) are regrouped so each language reads
        continuously: all Kannada pages first, then all English pages, each a top-level TOC section."""
        if not blocks:
            raise BookError("No readable text was found in this PDF.")
        toc = self.chapter_plan(doc)
        bilingual = getattr(self, "bilingual", False)
        page_lang = getattr(self, "page_lang", {})
        if bilingual:
            rank = {"kn": 0, "en": 1}
            blocks = sorted(blocks, key=lambda x: (rank.get(page_lang.get(x[-1], ""), 2), x[-1]))

        def plain(html):
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()

        # chapter starts: (block index, title, level)
        starts = []
        if toc and not bilingual:
            for lvl, t, pg in toc:
                if lvl > 2:
                    continue
                i = next((i for i, blk in enumerate(blocks) if blk[-1] >= pg), None)
                if i is not None:
                    starts.append((i, t, lvl))
        else:
            for i, blk in enumerate(blocks):
                if blk[0] == "h" and _CHAPTER_RE.match(plain(blk[2])):
                    title = plain(blk[2])
                    nxt = blocks[i + 1] if i + 1 < len(blocks) else None
                    if nxt and nxt[0] == "h" and not _CHAPTER_RE.match(plain(nxt[2])) and len(plain(nxt[2])) < 70:
                        title = f"{title} — {plain(nxt[2])}"
                    starts.append((i, title[:90], 2 if bilingual else 1))
            if len(starts) < 2:
                starts = []
                last = None
                for i, blk in enumerate(blocks):
                    chunk = blk[-1] // 20
                    if chunk != last:
                        starts.append((i, f"Pages {chunk * 20 + 1}–{min(chunk * 20 + 20, doc.page_count)}", 2 if bilingual else 1))
                        last = chunk
        if bilingual:
            for lang, title in (("kn", "ಕನ್ನಡ ಆವೃತ್ತಿ"), ("en", "English version")):
                i = next((i for i, blk in enumerate(blocks) if page_lang.get(blk[-1]) == lang), None)
                if i is not None:
                    starts.append((i, title, 1))
        if not starts or starts[0][0] != 0:
            starts.append((0, b.title, 1))
        starts.sort(key=lambda x: (x[0], x[2]))
        # merge starts at the same block (section + first part): keep both in the TOC, one file
        file_starts = sorted({i for i, _, _ in starts})
        toc_entries = []
        n = 0
        file_of_block = {}
        for fi, st in enumerate(file_starts):
            end = file_starts[fi + 1] if fi + 1 < len(file_starts) else len(blocks)
            chunk = blocks[st:end]
            parts, cur, size = [], [], 0
            for blk in chunk:
                cur.append(blk)
                size += len(blk[1] if blk[0] != "h" else blk[2])
                if size > 100_000 and blk[0] == "p":
                    parts.append(cur)
                    cur, size = [], 0
            if cur:
                parts.append(cur)
            for k, part in enumerate(parts):
                n += 1
                href = f"ch{n:03d}.xhtml"
                if k == 0:
                    file_of_block[st] = href
                html, seen_pages, fn_open = [], set(), False
                for blk in part:
                    pg = blk[-1]
                    if pg not in seen_pages:
                        html.append(f'<a id="pg{pg + 1}"></a>')
                        seen_pages.add(pg)
                    if blk[0] != "fn":
                        fn_open = False
                    if blk[0] == "h":
                        lv = min(3, blk[1])
                        html.append(f'<h{lv} class="pdf-h{lv}">{blk[2]}</h{lv}>')
                    elif blk[0] == "p":
                        cls = "pdf-p" + (" pdf-first" if html and html[-1].startswith("<h") else "")
                        html.append(f'<p class="{cls}">{blk[1]}</p>')
                    elif blk[0] == "fn":
                        if not fn_open:
                            html.append('<hr class="fn-rule"/>')
                            fn_open = True
                        html.append(f'<p class="fn">{blk[1]}</p>')
                    elif blk[0] == "pre":
                        html.append(f"<pre>{esc(blk[1])}</pre>")
                    else:
                        html.append(f'<div class="pdf-fig"><img src="../{blk[1]}" alt=""/></div>')
                title = next((t for i, t, l in starts if i == st), b.title)
                b.page(href, title, "".join(html))
        for i, t, lvl in starts:
            if i in file_of_block:
                toc_entries.append((lvl, t, f"Text/{file_of_block[i]}"))
        b.toc = toc_entries
        # PDF bookmarks deeper than level 2 point at page anchors
        if toc and not bilingual:
            page_file = {}
            for i, blk in enumerate(blocks):
                st = max(x for x in file_starts if x <= i)
                page_file.setdefault(blk[-1], file_of_block.get(st))
            b.toc = []
            for lvl, t, pg in toc:
                q = pg
                while q >= 0 and q not in page_file:
                    q -= 1
                if q >= 0 and page_file[q]:
                    b.toc.append((lvl, t, f"Text/{page_file[q]}#pg{q + 1}"))

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

    def _ocr_blocks(self, doc, b: EpubBuilder, pages) -> list:
        self.dropped_pages = []
        """OCR pages (per-page language) and rebuild paragraphs, headings and footnotes from Tesseract TSV."""
        default_lang = getattr(self, "ocr_lang", None) or ("kan+eng" if self.info["kannada"] else "eng")
        results = {}
        with ThreadPoolExecutor(max_workers=max(2, (os.cpu_count() or 4) // 2)) as ex:
            futs = []
            for k, p in enumerate(pages):
                lang = self.plan.get(p, ("", ""))[1] if hasattr(self, "plan") else ""
                lang = lang if lang in ("kan", "kan+eng", "eng") else default_lang
                im = self.render(doc[p], int(doc[p].rect.width * 300 / 72))
                futs.append(ex.submit(self._ocr_tsv, p, im, lang))
                self.step("Rendering pages for OCR", (k + 1) / len(pages) * 0.25)
            for k, f in enumerate(futs):
                p, lines, page_h = f.result()
                results[p] = (lines, page_h)
                self.step(f"OCR page {k + 1}/{len(futs)}", 0.25 + 0.7 * (k + 1) / len(futs))

        if len(self.dropped_pages) > max(2, len(pages) * 0.03):
            self.log(f"OCR filters removed confident text on pages {self.dropped_pages[:10]}")
        # running heads / page numbers: first and last lines repeated across pages
        furn = Counter()
        for p, (lines, ph) in results.items():
            for l in lines:
                if l["top"] < ph * 0.12 or l["bottom"] > ph * 0.9:
                    furn[_norm_ocr_furn(l["text"])] += 1
        thresh = max(3, int(len(results) * 0.12))
        furn = {k for k, v in furn.items() if v >= thresh and k and re.search(r"[A-Za-z\u0C85-\u0CB9]", k)}
        furn_re = [re.compile(r"\s*" + re.escape(f).replace("\\#", r"[\d೦-೯]+").replace("\\ ", r"\s*") + r"\s*$", re.I)
                   for f in furn if len(f) >= 6]

        blocks = []
        carry = None
        for p in sorted(results):
            lines, ph = results[p]
            kn_page = any(KANNADA_RE.search(l["text"]) for l in lines)
            kept = []
            for l in lines:
                t = l["text"]
                edge = l["top"] < ph * 0.12 or l["bottom"] > ph * 0.9
                if _norm_ocr_furn(t) in furn or edge and re.fullmatch(r"[\[\]()\d೦-೯ivxlcIVXLC\-–—.,: ]{1,8}", t):
                    continue
                for fr in furn_re:            # running head glued to the end of a body line
                    t2 = fr.sub("", t)
                    if t2 != t and len(t2) > 3:
                        l = dict(l, text=t2)
                        break
                kept.append(l)
            if not kept:
                if not lines:
                    href = self._fig(doc[p], list(doc[p].rect), b)
                    if href:
                        blocks.append(("fig", href, p))
                continue
            hs = sorted(l["height"] for l in kept)
            h_med = hs[len(hs) // 2]
            lefts = sorted(l["left"] for l in kept)
            x0, x1 = lefts[len(lefts) // 10], max(l["right"] for l in kept)
            # group into tesseract paragraphs
            paras, cur_key = [], None
            # footnote zone: everything below the first small "1." / "*" line in the lower part of the page
            fn_tops = [l["top"] for l in kept if l["height"] < h_med * 0.84 and l["top"] > ph * 0.55 and
                       re.match(r"^\s*(?:[\d೦-೯]{1,3}\s*[.)…]|\*|[\d೦-೯]{1,3}\.{2,})", l["text"])]
            fn_top = min(fn_tops) if fn_tops else None
            for l in kept:
                key = (l["block"], l["par"])
                fn = fn_top is not None and l["top"] >= fn_top - 2
                if key != cur_key or (paras and paras[-1]["fn"] != fn):
                    paras.append({"lines": [], "fn": fn})
                    cur_key = key
                paras[-1]["lines"].append(l)
            all_text = [l["text"] for l in kept]
            page_w = max((len(t) for t in all_text), default=1)
            page_verse = len(all_text) >= 4 and statistics.median(len(t) for t in all_text) < (30 if kn_page else 38)
            # legal / list clauses "(ಎ) …", "(1) …", "12. …" always start their own paragraph
            split = []
            for para in paras:
                cur_l = []
                for l in para["lines"]:
                    if cur_l and _ARTICLE_START.match(l["text"]) and not para["fn"]:
                        split.append({"lines": cur_l, "fn": para["fn"]})
                        cur_l = []
                    cur_l.append(l)
                if cur_l:
                    split.append({"lines": cur_l, "fn": para["fn"]})
            paras = split
            fn_texts = [l["text"] for para in paras if para["fn"] for l in para["lines"]]
            paras = [para for para in paras if not para["fn"]]
            split = []
            for para in paras:
                ls_ = para["lines"]
                while ls_ and _CHAPTER_RE.match(ls_[0]["text"]) and len(ls_[0]["text"]) < 60:
                    split.append({"lines": [ls_[0]], "fn": False, "chapter": True})
                    ls_ = ls_[1:]
                    if ls_ and len(ls_[0]["text"]) < 45 and not re.search(r"[.,;:]$", ls_[0]["text"]) and \
                            not _ARTICLE_START.match(ls_[0]["text"]):
                        split.append({"lines": [ls_[0]], "fn": False, "title": True})
                        ls_ = ls_[1:]
                if ls_:
                    split.append(dict(para, lines=ls_))
            paras = split
            for k, para in enumerate(paras):
                pl = para["lines"]
                texts = [l["text"] for l in pl]
                punct_end = sum(1 for t in texts if re.search(r"[,;:!?.।॥]$", t)) / len(texts)
                verse = len(texts) >= 2 and not any(_ARTICLE_START.match(t) for t in texts) and \
                    (page_verse or statistics.median(len(t) for t in texts) < max(18, page_w * 0.55)
                     or (len(texts) >= 3 and punct_end >= 0.6 and statistics.median(len(t) for t in texts) < page_w * 0.75))
                single = len(pl) == 1
                centered = single and pl[0]["left"] > x0 + (x1 - x0) * 0.12 and pl[0]["right"] < x1 - (x1 - x0) * 0.12 and \
                    abs((pl[0]["left"] + pl[0]["right"]) / 2 - (x0 + x1) / 2) < (x1 - x0) * 0.1
                tall = single and pl[0]["height"] > h_med * 1.5 and len(texts[0]) < 50 and \
                    not re.search(r"[;,]$|ಮತ್ತು$|ಅಥವಾ$|\b(and|or)$", texts[0])
                if para.get("chapter"):
                    blocks.append(("h", 2, esc(cleanup_text(_fix_roman(texts[0]), kn_page)), p))
                    carry = None
                    continue
                if para.get("title"):
                    blocks.append(("h", 3, esc(cleanup_text(texts[0], kn_page)), p))
                    carry = None
                    continue
                joined_raw = " ".join(texts)
                is_heading = single and len(joined_raw) < 90 and not re.search(r"[.,;:]$", joined_raw) and \
                    (centered or tall or _CHAPTER_RE.match(joined_raw)) and not (page_verse and k > 0 and not tall)
                if is_heading:
                    blocks.append(("h", 2 if (tall or _CHAPTER_RE.match(joined_raw)) else 3, esc(cleanup_text(joined_raw, kn_page)), p))
                    carry = None
                    continue
                if verse and not _ARTICLE_START.match(texts[0]):
                    blocks.append(("p", "<br/>".join(esc(cleanup_text(t, kn_page)) for t in texts), p))
                    carry = None
                    continue
                joined = texts[0]
                for t in texts[1:]:
                    joined = dehyphenate_join(joined, t) if not kn_page else joined + " " + t
                joined = cleanup_text(joined, kn_page)
                # paragraph running over from the previous OCR page
                if k == 0 and carry is not None and not page_verse and not _ARTICLE_START.match(joined) and \
                        not re.search(r"[.!?:\"”।॥\]]$", blocks[carry][1]):
                    blocks[carry] = ("p", blocks[carry][1] + " " + esc(joined), blocks[carry][2])
                    continue
                blocks.append(("p", esc(joined), p))
            last_p = max((i for i, blk in enumerate(blocks) if blk[-1] == p and blk[0] == "p"), default=None)
            carry = last_p
            # footnotes go after the page's text; a new note starts at "1." / "2…" / "*"
            note = []
            for t in fn_texts:
                if note and re.match(r"^\s*(?:[\d೦-೯]{1,3}\s*(?:[.)…]|\.{2,})|\*)", t):
                    blocks.append(("fn", esc(cleanup_text(" ".join(note), kn_page)), p))
                    note = []
                note.append(t)
            if note:
                blocks.append(("fn", esc(cleanup_text(" ".join(note), kn_page)), p))
        return blocks

    def _ocr_tsv(self, p, im, lang):
        """Run tesseract (TSV) and return cleaned lines with geometry and confidence."""
        with tempfile.TemporaryDirectory(dir=_ocr_tmp()) as td:
            ip = os.path.join(td, "p.png")
            im.save(ip)
            base = os.path.join(td, "out")
            try:
                subprocess.run([tesseract_path(), ip, base, "-l", lang, "--psm", "3", "tsv"],
                               capture_output=True, timeout=300, env=tesseract_env())
                rows = open(base + ".tsv", encoding="utf-8", errors="replace").read().splitlines()[1:]
            except Exception:
                return p, [], im.height
        lines = {}
        order = []
        for row in rows:
            c = row.split("\t")
            if len(c) < 12 or c[0] != "5":
                continue
            word = c[11].strip()
            try:
                conf = float(c[10])
            except ValueError:
                continue
            if not word or conf < 0:
                continue
            key = (int(c[2]), int(c[3]), int(c[4]))
            if key not in lines:
                lines[key] = []
                order.append(key)
            lines[key].append((word, conf, int(c[6]), int(c[7]), int(c[8]), int(c[9])))
        out = []
        raw_chars = sum(len(w[0]) for key in order for w in lines[key] if w[1] >= 60)
        for key in order:
            ws = [w for w in lines[key] if w[1] >= 12 or KANNADA_RE.search(w[0]) and w[1] >= 5]
            if not ws:
                continue
            text = " ".join(w[0] for w in ws)
            conf = statistics.mean(w[1] for w in ws)
            text = _clean_ocr_line(text)
            if not text or conf < 35 or _ocr_garbage(text, conf):
                continue
            heights = sorted(w[5] for w in ws)
            out.append({"text": text, "conf": conf, "block": key[0], "par": key[1],
                        "left": min(w[2] for w in ws), "right": max(w[2] + w[4] for w in ws),
                        "top": min(w[3] for w in ws), "bottom": max(w[3] + w[5] for w in ws),
                        "height": heights[len(heights) // 2]})
        kept_chars = sum(len(l["text"]) for l in out)
        if raw_chars > 400 and kept_chars < raw_chars * 0.88:
            self.dropped_pages.append(p + 1)      # filters removed confident text: reported, never silent
        # chapter headings often carry Roman numerals set in a Latin font: re-read those lines with kan+eng
        if lang == "kan" and "eng" in tesseract_langs():
            for l in out:
                if _CHAPTER_RE.match(l["text"]) and len(l["text"]) < 80:
                    pad = max(4, l["height"] // 3)
                    crop = im.crop((max(0, l["left"] - pad), max(0, l["top"] - pad),
                                    min(im.width, l["right"] + pad), min(im.height, l["bottom"] + pad)))
                    t2 = self._ocr_line(crop, "kan+eng")
                    if t2 and _CHAPTER_RE.match(t2):
                        l["text"] = _fix_roman(t2)
        return p, out, im.height

    def _ocr_line(self, im, lang) -> str:
        with tempfile.TemporaryDirectory(dir=_ocr_tmp()) as td:
            ip = os.path.join(td, "l.png")
            im.save(ip)
            try:
                r = subprocess.run([tesseract_path(), ip, "stdout", "-l", lang, "--psm", "7"], capture_output=True,
                                   timeout=60, env=tesseract_env())
                return _clean_ocr_line(r.stdout.decode("utf-8", "replace").strip())
            except Exception:
                return ""

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
