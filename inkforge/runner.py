"""Batch processing: dispatch by format, convert, optimize, name, write, zip."""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
import tempfile
import traceback
import zipfile
from dataclasses import dataclass, field

from .devices import get_device
from .epub import Epub, read_zip, esc
from .options import Options, BookError
from .pipeline import Optimizer, STEPS, strip_junk

EPUB_EXT = {".epub"}
PDF_EXT = {".pdf"}
MOBI_EXT = {".mobi", ".azw", ".azw3", ".prc", ".kf8"}
TXT_EXT = {".txt"}
CALIBRE_EXT = {".fb2", ".docx", ".rtf", ".odt", ".htmlz", ".lit", ".pdb", ".cbz", ".cbr", ".html", ".htm"}
SUPPORTED = EPUB_EXT | PDF_EXT | MOBI_EXT | TXT_EXT | CALIBRE_EXT


@dataclass
class Job:
    path: str
    rel_dir: str = ""          # sub-folder inside a dropped folder (kept in the output)
    status: str = "queued"     # queued | working | done | failed | skipped
    message: str = ""
    out_path: str = ""
    before: int = 0
    after: int = 0
    method: str = ""
    warnings: list = field(default_factory=list)
    category: str = ""

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def ext(self):
        return os.path.splitext(self.path)[1].lower()


def collect(paths: list[str]) -> list[Job]:
    """Expand dropped files and folders into jobs (folders recursively, structure remembered)."""
    jobs, seen = [], set()
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            root_name = os.path.basename(p.rstrip("/"))
            for dp, dns, fns in os.walk(p):
                dns[:] = sorted(d for d in dns if not d.startswith("."))
                for fn in sorted(fns):
                    if fn.startswith(".") or os.path.splitext(fn)[1].lower() not in SUPPORTED:
                        continue
                    fp = os.path.join(dp, fn)
                    if fp in seen:
                        continue
                    seen.add(fp)
                    rel = os.path.relpath(dp, p)
                    jobs.append(Job(fp, os.path.normpath(os.path.join(root_name, rel)) if rel != "." else root_name))
        elif os.path.isfile(p) and p not in seen:
            seen.add(p)
            j = Job(p)
            if j.ext not in SUPPORTED:
                j.status, j.message = "skipped", "Unsupported file type"
            jobs.append(j)
    return jobs


def calibre_convert() -> str | None:
    for c in ("/Applications/calibre.app/Contents/MacOS/ebook-convert", shutil.which("ebook-convert") or ""):
        if c and os.path.exists(c):
            return c
    return None


# ---------------------------------------------------------------------- format converters
def convert_with_calibre(path: str, tmp: str) -> Epub:
    exe = calibre_convert()
    if not exe:
        raise BookError("This format needs Calibre's converter. Install Calibre (free) from calibre-ebook.com, "
                        "or convert the book to EPUB first.")
    out = os.path.join(tmp, "calibre.epub")
    r = subprocess.run([exe, path, out, "--no-default-epub-cover"], capture_output=True, text=True, timeout=1200)
    if r.returncode != 0 or not os.path.exists(out):
        msg = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["unknown error"]
        if "DRM" in (r.stderr + r.stdout):
            raise BookError("This book is DRM-protected. Only DRM-free books can be converted.")
        raise BookError(f"Calibre could not convert this file ({msg[0][:160]}).")
    return Epub.load(out)


def mobi_is_drm(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(78)
            if head[60:68] not in (b"BOOKMOBI", b"TEXtREAd"):
                return False
            f.seek(76)
            nrec = int.from_bytes(f.read(2), "big")
            f.seek(78)
            rec0 = int.from_bytes(f.read(4), "big")
            f.seek(rec0 + 12)
            enc = int.from_bytes(f.read(2), "big")
            return enc != 0 and nrec > 0
    except Exception:
        return False


def convert_mobi(path: str, tmp: str) -> Epub:
    if mobi_is_drm(path):
        raise BookError("This Kindle book is DRM-protected. Only DRM-free MOBI/AZW3 files can be converted.")
    if calibre_convert():
        try:
            return convert_with_calibre(path, tmp)
        except BookError:
            pass  # fall back to the built-in unpacker
    try:
        import mobi
    except ImportError:
        raise BookError("MOBI support is missing (install the 'mobi' package or Calibre).")
    try:
        tdir, fp = mobi.extract(path)
    except Exception as e:
        raise BookError(f"This MOBI file could not be unpacked ({e}). It may be damaged or DRM-protected.")
    try:
        if fp.lower().endswith(".epub"):
            return Epub(read_zip(fp))
        if fp.lower().endswith(".pdf"):
            raise BookError("This Kindle file wraps a PDF; drop that PDF instead.")
        base = os.path.dirname(fp)
        if os.path.exists(os.path.join(base, "content.opf")):
            files = {}
            for dp, _, fns in os.walk(base):
                for fn in fns:
                    full = os.path.join(dp, fn)
                    files[os.path.relpath(full, base).replace(os.sep, "/")] = open(full, "rb").read()
            files.pop("mimetype", None)
            files["META-INF/container.xml"] = (b'<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                                               b'<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            return Epub(files)
        return html_to_epub(open(fp, "rb").read(), os.path.dirname(fp), os.path.splitext(os.path.basename(path))[0])
    finally:
        shutil.rmtree(tdir, ignore_errors=True)


def html_to_epub(html: bytes, res_dir: str, stem: str) -> Epub:
    from .pdfconv import EpubBuilder, parse_filename
    from .epub import parse_xhtml, XHTML_NS
    from lxml import etree
    title, author = parse_filename(stem)
    text = html.decode("utf-8", "replace")
    parts = re.split(r"<mbp:pagebreak\s*/?>", text, flags=re.I)
    b = EpubBuilder(title, author, "en")
    for k, part in enumerate(parts):
        root = parse_xhtml(("<html><body>" + part + "</body></html>").encode("utf-8"))
        body = root.find("{%s}body" % XHTML_NS)
        for img in body.iter("{%s}img" % XHTML_NS):
            src = img.get("src") or img.get("recindex") or ""
            fp = os.path.join(res_dir, src)
            if src and os.path.exists(fp):
                href = b.add_image(open(fp, "rb").read(), "image/jpeg" if fp.lower().endswith((".jpg", ".jpeg")) else "image/png")
                img.set("src", "../" + href)
        inner = "".join(etree.tostring(c, encoding="unicode") for c in body)
        inner = (esc(body.text or "")) + inner
        if inner.strip():
            b.page(f"part{k + 1:03d}.xhtml", title, inner)
    return b.build()


def convert_txt(path: str) -> Epub:
    from .pdfconv import EpubBuilder, parse_filename
    raw = open(path, "rb").read()
    text = None
    for enc in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise BookError("This text file's encoding could not be recognised.")
    title, author = parse_filename(os.path.splitext(os.path.basename(path))[0])
    from .textclean import is_kannada_text
    b = EpubBuilder(title, author, "kn" if is_kannada_text(text) else "en")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paras) < 3:  # one line per paragraph
        paras = [p.strip() for p in text.split("\n") if p.strip()]
    chap_re = re.compile(r"^(chapter|part|book|ಅಧ್ಯಾಯ|ಭಾಗ|ಪ್ರಕರಣ)\b.{0,60}$|^[IVXLC]+\.?$|^\d{1,3}\.?\s*$", re.I)
    chapters, cur, ctitle = [], [], ""
    for p in paras:
        one = " ".join(p.split())
        if chap_re.match(one) and len(one) < 80:
            if cur:
                chapters.append((ctitle, cur))
            cur, ctitle = [], one
            continue
        cur.append(one)
        if sum(len(x) for x in cur) > 100_000:
            chapters.append((ctitle, cur))
            cur, ctitle = [], ""
    if cur or not chapters:
        chapters.append((ctitle, cur))
    for k, (t, ps) in enumerate(chapters, 1):
        body = (f"<h2>{esc(t)}</h2>" if t else "") + "".join(f"<p>{esc(x)}</p>" for x in ps)
        b.page(f"ch{k:03d}.xhtml", t or title, body)
        if t:
            b.toc.append((1, t, f"Text/ch{k:03d}.xhtml"))
    return b.build()


# ---------------------------------------------------------------------- naming
_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(s: str, limit: int = 120) -> str:
    s = _BAD.sub(" ", s)
    s = " ".join(s.split()).strip(" .")
    if len(s.encode("utf-8")) > limit * 2:
        s = s.encode("utf-8")[: limit * 2].decode("utf-8", "ignore").rstrip(" .")
    return s or "book"


def output_name(job: Job, book: Epub, o: Options) -> str:
    title = strip_junk(book.title()) or os.path.splitext(job.name)[0]
    if len(title) > 60 and ":" in title[8:]:
        title = title.split(":", 1)[0].strip()          # drop long subtitles
    if len(title) > 80:
        title = title[:80].rsplit(" ", 1)[0].rstrip(" ,;-–[(")
    author = ", ".join(a.strip(" ,;") for a in book.authors()[:2])
    stem = os.path.splitext(job.name)[0]
    mode = o.naming
    if mode == "original":
        name = stem
    elif mode == "title_author":
        name = f"{title} - {author}" if author else title
    elif mode == "author_title":
        name = f"{author} - {title}" if author else title
    elif mode == "title":
        name = title
    else:
        pat = o.custom_pattern or "{title}"
        name = pat.replace("{title}", title).replace("{author}", author or "Unknown") \
            .replace("{device}", get_device(o.device).name).replace("{original}", stem) \
            .replace("{lang}", book.dc_text("language") or "")
        name = re.sub(r"\s*-\s*$|^\s*-\s*", "", name)
    return safe_name(name) + ".epub"


def unique(path: str) -> str:
    if not os.path.exists(path):
        return path
    b, e = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{b} ({n}){e}"):
        n += 1
    return f"{b} ({n}){e}"


# ---------------------------------------------------------------------- categories
def book_category(book: Epub, rep: dict, job: Job) -> str:
    from .categorize import categorize
    from lxml import etree
    subjects = ["".join(e.itertext()) for e in book.dc("subject")]
    desc = book.dc_text("description")
    sample = []
    n = 0
    for it in book.spine_items()[:8]:
        try:
            t = " ".join(etree.fromstring(book.files[it.href], etree.XMLParser(recover=True)).itertext())
        except Exception:
            continue
        sample.append(t)
        n += len(t)
        if n > 8000:
            break
    hint = os.path.basename(job.rel_dir) if job.rel_dir else ""
    return categorize(book.title() + " " + hint, subjects, desc, " ".join(sample), bool(rep.get("kannada")))


# ---------------------------------------------------------------------- one job
def process_job(job: Job, o: Options, out_root: str, progress=None, log=None) -> Job:
    """progress(fraction_0_to_1, label)"""
    prog = progress or (lambda *a: None)
    job.status, job.message = "working", ""
    job.before = os.path.getsize(job.path) if os.path.exists(job.path) else 0
    tmp = tempfile.mkdtemp(prefix="inkforge_")
    conv_share = 0.0
    try:
        ext = job.ext
        if ext in PDF_EXT:
            from .pdfconv import PdfConverter
            conv_share = 0.6
            c = PdfConverter(job.path, o, progress=lambda i, label, f: prog(conv_share * f, label), log=log)
            book, info = c.run()
            job.method = info["method"]
            job.warnings.extend(info["warnings"])
        elif ext in MOBI_EXT:
            prog(0.02, "Unpacking Kindle book")
            book = convert_mobi(job.path, tmp)
            job.method = "MOBI → EPUB"
            conv_share = 0.2
        elif ext in TXT_EXT:
            book = convert_txt(job.path)
            job.method = "Text → EPUB"
            conv_share = 0.05
        elif ext in CALIBRE_EXT:
            prog(0.02, "Converting with Calibre")
            book = convert_with_calibre(job.path, tmp)
            job.method = f"{ext[1:].upper()} → EPUB"
            conv_share = 0.3
        else:
            book = Epub.load(job.path)
            job.method = "EPUB"

        def p2(i, label, frac):
            prog(conv_share + (1 - conv_share) * ((i + frac) / len(STEPS)), f"Step {i + 1}/20 · {label}")
        data, rep = Optimizer(o, progress=p2, log=log).run(book, job.name)
        job.warnings.extend(rep["warnings"])
        job.category = book_category(book, rep, job)
        day = os.path.join(out_root, dt.date.today().isoformat())
        if o.organize == "category":
            out_dir = os.path.join(day, safe_name(job.category, 60))
        elif o.organize == "folders" and job.rel_dir:
            out_dir = os.path.join(day, *[safe_name(x, 60) for x in job.rel_dir.split(os.sep)])
        else:
            out_dir = day
        os.makedirs(out_dir, exist_ok=True)
        out = unique(os.path.join(out_dir, output_name(job, book, o)))
        with open(out, "wb") as fh:
            fh.write(data)
        job.out_path, job.after = out, len(data)
        job.status = "done"
        lang = "Kannada" if rep.get("kannada") else ""
        job.message = " · ".join(x for x in (job.method, lang) if x)
        prog(1.0, "Done")
    except BookError as e:
        job.status, job.message = "failed", str(e)
    except MemoryError:
        job.status, job.message = "failed", "The book is too large to process in memory."
    except Exception as e:
        job.status = "failed"
        job.message = f"Unexpected problem: {e.__class__.__name__}: {e}"
        if log:
            log(traceback.format_exc())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return job


def make_output_root(o: Options) -> str:
    base = o.output_dir or os.path.join(os.path.expanduser("~/Downloads"), "Xteink Optimized")
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H.%M")  # one dated batch folder
    root = unique(os.path.join(base, f"Xteink {get_device(o.device).name} – {stamp}"))
    os.makedirs(root, exist_ok=True)
    return root


def zip_folder(root: str) -> str:
    zpath = unique(root + ".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as zf:
        for dp, _, fns in os.walk(root):
            for fn in sorted(fns):
                if fn.endswith(".epub"):
                    full = os.path.join(dp, fn)
                    zf.write(full, os.path.relpath(full, root))   # zip: YYYY-MM-DD/Category/book.epub
    return zpath
