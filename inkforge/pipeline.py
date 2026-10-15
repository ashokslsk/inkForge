"""The 20-step EPUB optimization pipeline."""
from __future__ import annotations

import io
import posixpath
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

from lxml import etree

from . import css as csslib
from . import images as imglib
from .devices import get_device
from .epub import (Epub, NS, OPF, DC, XHTML_NS, parse_xhtml, serialize_xhtml, xhtml_page, esc, XML_PARSER,
                   guess_media_type, read_zip_bytes)
from .options import Options, BookError
from .textclean import cleanup_text, normalize, is_kannada_text, KANNADA_RE

STEPS = [
    "Reading book", "Checking structure", "Repairing manifest", "Cleaning metadata", "Detecting language",
    "Cleaning XHTML", "Text cleanup", "Normalizing Unicode", "Removing fonts", "Cleaning CSS",
    "E-ink stylesheet", "Converting image formats", "Resizing images", "Grayscale & contrast",
    "Light novel mode", "Encoding images", "Kannada rendering", "Cover", "Table of contents", "Packaging & verifying",
]

H = "{%s}" % XHTML_NS
XLINK = "{http://www.w3.org/1999/xlink}href"
SKIP_TEXT_TAGS = {H + "pre", H + "code", H + "script", H + "style", H + "kbd", H + "samp", H + "math"}
BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "div", "dd", "dt", "figcaption", "caption", "td", "th"}

JUNK_PATTERNS = [
    r"\s*\((?:z-library|z-lib|1lib|libgen|b-ok|booksc)[^)]*\)",
    r"\s*\[(?:z-library|z-lib|1lib|libgen)[^\]]*\]",
    r"\s*[-–|]?\s*(?:OceanofPDF\.com|www\.ebook3000\.com|libgen\.\w+|z-lib\.\w+|dokumen\.pub|ebookee\.\w+|vk\.com/\S+)",
]
WATERMARK_PARA = re.compile(
    r"^\s*(?:OceanofPDF\.com|www\.ebook3000\.com|z-library|z-lib\.org|1lib\.\w+|dokumen\.pub|"
    r"This page intentionally left blank\.?|Downloaded from \S+)\s*$", re.I)


def strip_junk(s: str) -> str:
    for p in JUNK_PATTERNS:
        s = re.sub(p, "", s, flags=re.I)
    return " ".join(s.split())


def normalize_author(a: str) -> str:
    a = strip_junk(a)
    a = re.sub(r"\s*\[[^\]]*\]\s*$", "", a).strip()  # "Wes Lee [Lee, Wes]"
    a = re.sub(r",?\s*\b(?:Ph\.?\s?D|M\.?D|MBA|Dr)\.?(?=$|,)", "", a).strip(" ,;")
    m = re.fullmatch(r"([^,]{2,40}),\s*([^,]{2,40})", a)
    if m and not re.search(r"\b(?:jr|sr|ii|iii)\.?$", m.group(2), re.I):
        a = f"{m.group(2).strip()} {m.group(1).strip()}"
    return a.strip(" ,;")


class Optimizer:
    def __init__(self, opts: Options, progress=None, log=None):
        self.o = opts
        self.dev = get_device(opts.device)
        self.progress = progress or (lambda *a: None)
        self.log = log or (lambda *a: None)
        self.warnings: list[str] = []

    def step(self, i: int, frac: float = 0.0):
        self.progress(i, STEPS[i], frac)

    # ====================================================================== main entry
    def run(self, book: Epub, source_name: str = "") -> tuple[bytes, dict]:
        o = self.o
        t0 = time.time()
        self.book = book
        self.step(0)
        before = sum(len(v) for v in book.files.values())

        self.step(1)
        self._check_structure()
        self.step(2)
        self._repair_manifest()
        self.docs = self._parse_docs()

        self.step(3)
        if o.clean_meta:
            self._clean_metadata()
        self.step(4)
        self.kannada = self._detect_language()

        self.step(5)
        self._clean_xhtml()
        self.step(6)
        if o.text_cleanup:
            self._text_cleanup()
        self.step(7)
        self._normalize_unicode()

        self.step(8)
        if o.remove_fonts:
            self._remove_fonts()
        self.step(9)
        if o.clean_css:
            self._clean_css()
        self.step(10)
        self._add_eink_css()

        self.step(11)
        self._images()          # steps 11-15 report progress inside
        self.step(16)
        if self.kannada and o.kannada_mode == "image":
            self._render_kannada()
        self.step(17)
        self._cover()
        self.step(18)
        self._toc()
        self.step(19)
        if o.clean_css:
            self._drop_unused()
        data = self._package()
        report = {
            "title": book.title(), "authors": book.authors(), "language": book.dc_text("language"),
            "kannada": self.kannada, "before": before, "after": len(data), "warnings": self.warnings,
            "seconds": round(time.time() - t0, 1), "chapters": len(book.spine_items()),
        }
        return data, report

    # ====================================================================== 1-2 structure
    def _check_structure(self):
        b = self.book
        if not b.spine_items():
            # rebuild spine from manifest XHTML in file order
            xs = [it for it in b.items() if it.media_type == "application/xhtml+xml" and it.href in b.files
                  and not it.has_prop("nav")]
            if not xs:
                xs_paths = sorted(n for n in b.files if re.search(r"\.x?html?$", n, re.I))
                if not xs_paths:
                    raise BookError("This EPUB contains no readable chapters.")
                for pth in xs_paths:
                    xs.append(b.add_item(pth, b.files[pth], "application/xhtml+xml"))
            for it in xs:
                etree.SubElement(b.spine, OPF + "itemref").set("idref", it.id)
            self.warnings.append("Rebuilt the missing reading order (spine).")

    # ====================================================================== 3 manifest
    def _repair_manifest(self):
        b = self.book
        fix = {"text/html": "application/xhtml+xml", "image/jpg": "image/jpeg", "application/xhtml": "application/xhtml+xml",
               "text/xml": "application/xhtml+xml"}
        seen_ids, seen_paths = set(), set()
        for it in b.items():
            if it.href not in b.files:
                # case-insensitive / url-encoding repair
                cand = next((n for n in b.files if n.lower() == it.href.lower()), None)
                if cand:
                    it.el.set("href", b.opf_href(cand))
                    it.href = cand
                else:
                    b.remove_item(it)
                    continue
            if it.href in seen_paths:
                b.remove_item(it)
                continue
            if it.id in seen_ids or not it.id:
                it.el.set("id", b.new_id(it.id or "item"))
                it.id = it.el.get("id")
            seen_ids.add(it.id)
            seen_paths.add(it.href)
            mt = it.media_type
            if mt in fix:
                it.el.set("media-type", fix[mt])
            ext_mt = guess_media_type(it.href)
            if mt.startswith("image/") and ext_mt.startswith("image/") and ext_mt != mt:
                it.el.set("media-type", ext_mt)
        for it in b.items():
            if re.search(r"\.(js)$", it.href, re.I):
                b.remove_item(it)
        if self.book.spine.get("toc") and not b.item_by_id(self.book.spine.get("toc")):
            del self.book.spine.attrib["toc"]

    def _parse_docs(self) -> dict:
        docs = {}
        for it in self.book.items():
            if it.media_type == "application/xhtml+xml" and it.href in self.book.files:
                try:
                    docs[it.href] = parse_xhtml(self.book.files[it.href])
                except Exception as e:  # pragma: no cover
                    self.warnings.append(f"Could not parse {posixpath.basename(it.href)}: {e}")
        if not docs:
            raise BookError("None of the chapters in this EPUB could be read.")
        return docs

    # ====================================================================== 4 metadata
    def _clean_metadata(self):
        b = self.book
        md = b.metadata
        for el in list(md):
            if not isinstance(el.tag, str):
                md.remove(el)
                continue
            name = (el.get("name") or "") + " " + (el.get("property") or "")
            content = el.get("content") or "".join(el.itertext())
            local = etree.QName(el).localname
            if re.search(r"calibre|Sigil|amzn|AmazonAuthor|book-type|kindle|readium|ibooks:|rendition:(?!layout)", name, re.I):
                md.remove(el)
                continue
            if local == "contributor" and re.search(r"calibre|sigil|aspose|kindlegen|adobe|abbyy|pandoc|pdf", content, re.I):
                md.remove(el)
                continue
            if local == "identifier" and (re.search(r"calibre|mobi-asin|amazon", (el.get("{%s}scheme" % NS["opf"]) or "") + content, re.I)):
                if len(b.dc("identifier")) > 1 and el.get("id") != b.opf.get("unique-identifier"):
                    md.remove(el)
                    continue
            if local in ("title", "creator", "publisher", "subject"):
                txt = "".join(el.itertext())
                new = normalize_author(txt) if local == "creator" else strip_junk(txt)
                if local == "creator" and el.get("{%s}file-as" % NS["opf"]):
                    el.attrib.pop("{%s}file-as" % NS["opf"], None)
                for ch in list(el):
                    el.remove(ch)
                el.text = new
                if not new:
                    md.remove(el)
            if local == "description":
                txt = re.sub(r"<[^>]+>", " ", "".join(el.itertext()))
                el.text = " ".join(txt.split())[:2000]
                for ch in list(el):
                    el.remove(ch)
            if local == "meta" and el.get("property") in ("file-as",) :
                md.remove(el)
        # remove refines that point to removed elements
        ids = {e.get("id") for e in md if isinstance(e.tag, str) and e.get("id")}
        for el in list(md):
            ref = (el.get("refines") or "").lstrip("#")
            if ref and ref not in ids:
                md.remove(el)
        # de-duplicate creators
        seen = set()
        for el in b.dc("creator"):
            k = (el.text or "").lower()
            if k in seen:
                md.remove(el)
            seen.add(k)
        self._ensure_identifier()

    def _ensure_identifier(self):
        b = self.book
        uid = b.opf.get("unique-identifier")
        ids = b.dc("identifier")
        if uid and any(e.get("id") == uid for e in ids):
            return
        if ids:
            el = ids[0]
            if not el.get("id"):
                el.set("id", b.new_id("bookid"))
            b.opf.set("unique-identifier", el.get("id"))
        else:
            el = etree.SubElement(b.metadata, DC + "identifier")
            el.set("id", "bookid")
            el.text = "urn:uuid:" + str(uuid.uuid4())
            b.opf.set("unique-identifier", "bookid")

    # ====================================================================== 5 language
    def _detect_language(self) -> bool:
        b = self.book
        sample = []
        n = 0
        for path, root in self.docs.items():
            t = "".join(root.itertext())
            sample.append(t)
            n += len(t)
            if n > 300_000:
                break
        text = " ".join(sample) + " " + b.title()
        kannada = is_kannada_text(text) or bool(KANNADA_RE.search(b.title()) and is_kannada_text(text, 0.1)) or \
            b.dc_text("language").lower().startswith(("kn", "kan"))
        langs = b.dc("language")
        if kannada:
            # PocketDeck-OS turns its Kannada shaper on when the language is "kn"
            if langs:
                langs[0].text = "kn"
                for extra in langs[1:]:
                    b.metadata.remove(extra)
            else:
                etree.SubElement(b.metadata, DC + "language").text = "kn"
            for root in self.docs.values():
                root.set("lang", "kn")
                root.set("{http://www.w3.org/XML/1998/namespace}lang", "kn")
        elif not langs or not (langs[0].text or "").strip() or (langs[0].text or "").strip().lower() in ("und", "unknown", "zxx"):
            if langs:
                langs[0].text = "en"
            else:
                etree.SubElement(b.metadata, DC + "language").text = "en"
        if not b.dc("title"):
            etree.SubElement(b.metadata, DC + "title").text = "Untitled"
        return kannada

    # ====================================================================== 6 XHTML
    def _clean_xhtml(self):
        for path, root in self.docs.items():
            for el in list(root.iter()):
                if not isinstance(el.tag, str):
                    if el.tag is etree.Comment or el.tag is etree.ProcessingInstruction:
                        self._drop(el, keep_tail=True)
                    continue
                local = etree.QName(el).localname.lower()
                if local in ("script", "iframe", "form", "input", "button", "select", "textarea", "noscript", "embed"):
                    self._drop(el, keep_tail=True)
                    continue
                if local in ("video", "audio", "object"):
                    # keep fallback content
                    self._unwrap(el)
                    continue
                for k in list(el.attrib):
                    if k.lower().startswith("on"):
                        del el.attrib[k]
                if self.o.clean_css and el.get("style") is not None:
                    st = csslib.clean_declarations(el.get("style"), self.o.remove_fonts)
                    if st:
                        el.set("style", st)
                    else:
                        del el.attrib["style"]
            self._fix_svg(path, root)
            head = root.find(H + "head")
            if head is None:
                head = etree.Element(H + "head")
                root.insert(0, head)
            if head.find(H + "title") is None:
                t = etree.SubElement(head, H + "title")
                t.text = self.book.title() or "Chapter"
            if root.find(H + "body") is None:
                etree.SubElement(root, H + "body")
            if self.o.text_cleanup:
                for p in root.iter(H + "p"):
                    if len(p) == 0 and p.text and WATERMARK_PARA.match(p.text):
                        self._drop(p, keep_tail=False)

    def _drop(self, el, keep_tail=True):
        parent = el.getparent()
        if parent is None:
            return
        if keep_tail and el.tail:
            prev = el.getprevious()
            if prev is not None:
                prev.tail = (prev.tail or "") + el.tail
            else:
                parent.text = (parent.text or "") + el.tail
        parent.remove(el)

    def _unwrap(self, el):
        """Replace an element by its children, keeping all text."""
        parent = el.getparent()
        if parent is None:
            return
        idx = parent.index(el)
        prev = el.getprevious()
        if el.text:
            if prev is not None:
                prev.tail = (prev.tail or "") + el.text
            else:
                parent.text = (parent.text or "") + el.text
        kids = list(el)
        tail = el.tail
        parent.remove(el)
        for i, ch in enumerate(kids):
            parent.insert(idx + i, ch)
        if tail:
            anchor = kids[-1] if kids else prev
            if anchor is not None:
                anchor.tail = (anchor.tail or "") + tail
            else:
                parent.text = (parent.text or "") + tail

    def _fix_svg(self, path, root):
        """Firmware skips <svg>: turn SVG-wrapped images into <img>, rasterize real vector art."""
        for svg in list(root.iter("{http://www.w3.org/2000/svg}svg")):
            imgs = list(svg.iter("{http://www.w3.org/2000/svg}image"))
            drawing = [e for e in svg.iter() if isinstance(e.tag, str) and etree.QName(e).localname in
                       ("path", "rect", "circle", "line", "polygon", "polyline", "ellipse", "text")]
            parent = svg.getparent()
            if parent is None:
                continue
            if len(imgs) == 1 and not drawing:
                href = imgs[0].get(XLINK) or imgs[0].get("href")
                if href:
                    img = etree.Element(H + "img")
                    img.set("src", href)
                    img.set("alt", "")
                    img.tail = svg.tail
                    parent.replace(svg, img)
                    continue
            data = etree.tostring(svg)
            pil = imglib.render_svg(data, max_w=self.dev.width * 2)
            if pil is None:
                continue
            name = self._unique_path(posixpath.join(posixpath.dirname(path), "images", "svg_%s.png" % uuid.uuid4().hex[:8]))
            buf = io.BytesIO()
            pil.save(buf, "PNG")
            self.book.add_item(name, buf.getvalue(), "image/png")
            img = etree.Element(H + "img")
            img.set("src", self.book.rel(name, path))
            img.set("alt", "")
            img.tail = svg.tail
            parent.replace(svg, img)

    def _unique_path(self, path):
        base, ext = posixpath.splitext(path)
        n, cand = 1, path
        while cand in self.book.files:
            n += 1
            cand = f"{base}_{n}{ext}"
        return cand

    # ====================================================================== 7-8 text
    def _text_nodes(self, root):
        """Yield (element, attr) pairs where attr is 'text' or 'tail' for editable text."""
        def walk(el, skip):
            if not isinstance(el.tag, str):
                return
            s = skip or el.tag in SKIP_TEXT_TAGS
            if el.tag in (H + "head",):
                return
            if not s and el.text:
                yield el, "text"
            for ch in el:
                yield from walk(ch, s)
                if not skip and ch.tail and isinstance(ch.tag, str):
                    yield ch, "tail"
        body = root.find(H + "body")
        if body is not None:
            yield from walk(body, False)

    def _text_cleanup(self):
        kn = self.kannada
        for root in self.docs.values():
            for el, attr in list(self._text_nodes(root)):
                v = getattr(el, attr)
                nv = cleanup_text(v, kannada=kn)
                if nv != v:
                    setattr(el, attr, nv)

    def _normalize_unicode(self):
        for root in self.docs.values():
            for el in root.iter():
                if isinstance(el.tag, str):
                    if el.text:
                        el.text = normalize(el.text)
                    if el.tail:
                        el.tail = normalize(el.tail)
        for el in self.book.metadata:
            if isinstance(el.tag, str) and el.text:
                el.text = normalize(el.text).strip()

    # ====================================================================== 9 fonts
    def _remove_fonts(self):
        b = self.book
        removed = 0
        for it in b.items():
            if it.media_type.startswith("font/") or "font" in it.media_type or "opentype" in it.media_type or \
                    re.search(r"\.(ttf|otf|woff2?|eot)$", it.href, re.I):
                b.remove_item(it)
                removed += 1
        b.files.pop("META-INF/encryption.xml", None)
        for it in b.items():
            if it.media_type == "text/css":
                css = b.files[it.href].decode("utf-8", "replace")
                b.files[it.href] = csslib.remove_font_family(css).encode("utf-8")
        for root in self.docs.values():
            for st in root.iter(H + "style"):
                if st.text:
                    st.text = csslib.remove_font_family(st.text)
            for el in root.iter():
                if isinstance(el.tag, str) and el.get("style") and "font-family" in el.get("style"):
                    el.set("style", re.sub(r"font-family\s*:[^;]*;?", "", el.get("style"), flags=re.I))
                if isinstance(el.tag, str) and el.tag == H + "font" and el.get("face"):
                    del el.attrib["face"]
        if removed:
            self.log(f"removed {removed} embedded fonts")

    # ====================================================================== 10 CSS
    def _clean_css(self):
        b = self.book
        for it in b.items():
            if it.media_type == "text/css":
                css = b.files[it.href].decode("utf-8", "replace")
                b.files[it.href] = csslib.clean_css(css, self.o.remove_fonts).encode("utf-8")
        for root in self.docs.values():
            for st in root.iter(H + "style"):
                if st.text:
                    st.text = csslib.clean_css(st.text, self.o.remove_fonts)

    def _add_eink_css(self):
        b = self.book
        path = posixpath.join(b.opf_dir, "Styles", "inkforge.css") if b.opf_dir else "Styles/inkforge.css"
        path = path.lstrip("/")
        b.add_item(path, csslib.EINK_CSS.encode("utf-8"), "text/css", iid="inkforge-css")
        self.eink_css = path
        for p, root in self.docs.items():
            head = root.find(H + "head")
            link = etree.SubElement(head, H + "link")
            link.set("rel", "stylesheet")
            link.set("type", "text/css")
            link.set("href", b.rel(path, p))

    # ====================================================================== 11-15 images
    def _image_refs(self):
        """Map image zip path -> list of (doc_path, element, attr)."""
        refs = {}
        for p, root in self.docs.items():
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue
                for attr in ("src", XLINK, "href", "data", "poster"):
                    v = el.get(attr)
                    if not v or attr == "href" and etree.QName(el).localname not in ("image",):
                        continue
                    if re.match(r"^[a-z]+:", v) and not v.startswith("file:"):
                        continue
                    target = self.book.abs(v, p)
                    refs.setdefault(target, []).append((p, el, attr))
        return refs

    def _css_refs(self):
        out = {}
        for it in self.book.items():
            if it.media_type == "text/css":
                css = self.book.files[it.href].decode("utf-8", "replace")
                for u in csslib.css_urls(css):
                    out.setdefault(self.book.abs(u, it.href), []).append(it.href)
        return out

    def _images(self):
        b, o, dev = self.book, self.o, self.dev
        cover_item = self._find_cover_item()
        self.cover_path = cover_item.href if cover_item is not None else None
        refs = self._image_refs()
        rtl = (b.spine.get("page-progression-direction") or "").lower() == "rtl"
        items = [it for it in b.items() if it.media_type.startswith("image/") and it.href in b.files
                 and not posixpath.basename(it.href).startswith("inkpre_")]
        if not items:
            for s in range(12, 16):
                self.step(s, 1.0)
            return
        quality = int(o.jpeg_quality)

        def work(it):
            data = b.files[it.href]
            im = imglib.open_image(data, it.media_type)
            if im is None:
                return it, None, "unreadable"
            is_cover = it.href == self.cover_path
            ln = []
            try:
                if o.light_novel and not is_cover and im.width > im.height * 1.05 and im.width >= dev.width * 0.8:
                    if im.width >= im.height * 1.3:
                        half = im.width // 2
                        left, right = im.crop((0, 0, half, im.height)), im.crop((half, 0, im.width, im.height))
                        parts = [right, left] if rtl else [left, right]
                        ln = [imglib.process(p_, dev.width, dev.height, o.grayscale, o.contrast) for p_ in parts]
                    else:
                        im = im.rotate(-90, expand=True)
                proc, line_art = imglib.process(im, dev.width, dev.height, o.grayscale, o.contrast and not is_cover)
                if is_cover and o.grayscale and o.contrast:
                    proc = imglib.ImageOps.autocontrast(proc, cutoff=0.5)
                enc = self._encode_best(proc, line_art, quality, data, it.media_type, im)
                ln_enc = [self._encode_best(p_, la, quality, None, None, None) for p_, la in ln]
                return it, (enc, ln_enc), None
            except Exception as e:  # keep original on failure
                return it, None, str(e)

        results = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            for k, res in enumerate(ex.map(work, items)):
                results.append(res)
                frac = (k + 1) / len(items)
                self.step(12 if frac < 0.5 else 13, frac)
        self.step(14, 1.0)
        renames = {}
        for it, out, err in results:
            if out is None:
                if it.media_type not in ("image/jpeg", "image/png"):
                    self.warnings.append(f"Removed unreadable image {posixpath.basename(it.href)}")
                    b.remove_item(it)
                    for (p, el, attr) in refs.get(it.href, []):
                        if etree.QName(el).localname == "img":
                            span = etree.Element(H + "span")
                            span.text = el.get("alt") or ""
                            span.tail = el.tail
                            if el.getparent() is not None:
                                el.getparent().replace(el, span)
                continue
            (data, mt), ln_parts = out
            ext = ".jpg" if mt == "image/jpeg" else ".png"
            old = it.href
            new = old if old.lower().endswith(ext) or (ext == ".jpg" and old.lower().endswith(".jpeg")) \
                else self._unique_path(posixpath.splitext(old)[0] + ext)
            b.files[old] = data
            if new != old:
                b.rename_item(it, new, mt)
                renames[old] = new
            else:
                it.el.set("media-type", mt)
            if ln_parts:
                self._insert_spread(it, new, ln_parts, refs.get(old, []))
        self.step(15, 1.0)
        if renames:
            self._apply_renames(renames, refs)
            if self.cover_path in renames:
                self.cover_path = renames[self.cover_path]

    def _encode_best(self, proc, line_art, quality, orig_data, orig_mt, orig_im):
        cand = [imglib.encode(proc, quality)]
        if line_art or proc.mode == "L" and proc.width * proc.height < 60_000:
            cand.append(imglib.encode(proc, quality, prefer_png=True))
        best = min(cand, key=lambda c: len(c[0]))
        # never make an already-small image bigger when nothing visible changed
        if orig_data and orig_mt in ("image/jpeg", "image/png") and orig_im is not None and \
                orig_im.size == proc.size and len(orig_data) <= len(best[0]) and not self.o.grayscale:
            return orig_data, orig_mt
        return best

    def _insert_spread(self, it, path, parts, refs):
        """Light-novel mode: show a two-page spread as two portrait images in reading order."""
        b = self.book
        base = posixpath.splitext(path)[0]
        new_paths = []
        for k, (data, mt) in enumerate(parts):
            np_ = self._unique_path(f"{base}_part{k + 1}{'.jpg' if mt == 'image/jpeg' else '.png'}")
            b.add_item(np_, data, mt)
            new_paths.append(np_)
        for (p, el, attr) in refs:
            if etree.QName(el).localname != "img" or el.getparent() is None:
                continue
            parent = el.getparent()
            idx = parent.index(el)
            tail = el.tail
            parent.remove(el)
            for k, np_ in enumerate(new_paths):
                d = etree.Element(H + "div")
                d.set("class", "ink-spread")
                im = etree.SubElement(d, H + "img")
                im.set("src", b.rel(np_, p))
                im.set("alt", el.get("alt") or "")
                parent.insert(idx + k, d)
            if tail:
                parent[idx + len(new_paths) - 1].tail = tail

    def _apply_renames(self, renames, refs):
        b = self.book
        for old, new in renames.items():
            for (p, el, attr) in refs.get(old, []):
                frag = ""
                v = el.get(attr) or ""
                if "#" in v:
                    frag = "#" + v.split("#", 1)[1]
                el.set(attr, b.rel(new, p) + frag)
        css_refs = self._css_refs()
        for it in b.items():
            if it.media_type != "text/css":
                continue
            css = b.files[it.href].decode("utf-8", "replace")
            changed = False

            def repl(m):
                nonlocal changed
                u = m.group(1).strip("'\" ")
                tgt = b.abs(u, it.href)
                if tgt in renames:
                    changed = True
                    return f'url("{b.rel(renames[tgt], it.href)}")'
                return m.group(0)
            css2 = re.sub(r"url\(([^)]*)\)", repl, css)
            if changed:
                b.files[it.href] = css2.encode("utf-8")
        for root_path, root in self.docs.items():
            for el in root.iter():
                if isinstance(el.tag, str) and el.get("style") and "url(" in el.get("style"):
                    def repl2(m, rp=root_path):
                        tgt = b.abs(m.group(1).strip("'\" "), rp)
                        return f'url("{b.rel(renames[tgt], rp)}")' if tgt in renames else m.group(0)
                    el.set("style", re.sub(r"url\(([^)]*)\)", repl2, el.get("style")))
        # guide references
        for ref in b.opf.iter(OPF + "reference"):
            tgt = b.abs(ref.get("href", ""))
            if tgt in renames:
                ref.set("href", b.opf_href(renames[tgt]))

    # ====================================================================== 16 Kannada as images
    def _render_kannada(self):
        from . import qtrender
        from PIL import Image
        b, dev = self.book, self.dev
        width = dev.width - 24
        max_h = int(dev.height * 0.82)
        sizes = {"h1": (36, True), "h2": (32, True), "h3": (30, True), "h4": (28, True), "h5": (27, True), "h6": (26, True)}
        total = len(self.docs)
        for n, (p, root) in enumerate(self.docs.items()):
            self.step(16, n / max(1, total))
            body = root.find(H + "body")
            if body is None:
                continue
            for el in list(body.iter()):
                if not isinstance(el.tag, str) or el.getparent() is None:
                    continue
                local = etree.QName(el).localname
                if local not in BLOCK_TAGS:
                    continue
                if any(isinstance(c.tag, str) and etree.QName(c).localname in BLOCK_TAGS | {"table", "ul", "ol", "img"}
                       for c in el.iter() if c is not el):
                    continue
                text = "".join(el.itertext()).strip()
                if not text or not KANNADA_RE.search(text):
                    continue
                inner = self._inline_html(el)
                px, bold = sizes.get(local, (26, False))
                align = "center" if (el.get("style") or "").find("center") >= 0 or local in ("h1", "h2") else "left"
                try:
                    im = qtrender.render_html(inner, width, px, bold=bold, align=align)
                except Exception as e:
                    self.warnings.append(f"Kannada rendering failed for a paragraph: {e}")
                    continue
                slices = self._slice_tall(im, max_h)
                div = etree.Element(H + "div")
                div.set("class", "kn-img")
                for k, sl in enumerate(slices):
                    data, mt = imglib.encode(sl, 80, prefer_png=True)
                    ip = self._unique_path(posixpath.join(posixpath.dirname(p), "kn", f"kn_{uuid.uuid4().hex[:10]}.png"))
                    b.add_item(ip, data, mt)
                    img = etree.SubElement(div, H + "img")
                    img.set("src", b.rel(ip, p))
                    img.set("alt", text if k == 0 else "")
                if el.get("id"):
                    div.set("id", el.get("id"))
                div.tail = el.tail
                if local == "li" or local in ("td", "th", "dd", "dt"):
                    for ch in list(el):
                        el.remove(ch)
                    el.text = None
                    el.append(div)
                    div.tail = None
                else:
                    el.getparent().replace(el, div)

    @staticmethod
    def _inline_html(el) -> str:
        parts = [esc(el.text or "")]
        for ch in el:
            if isinstance(ch.tag, str):
                local = etree.QName(ch).localname
                inner = Optimizer._inline_html(ch)
                if local in ("b", "strong"):
                    parts.append(f"<b>{inner}</b>")
                elif local in ("i", "em", "cite"):
                    parts.append(f"<i>{inner}</i>")
                elif local == "br":
                    parts.append("<br/>")
                elif local in ("sup", "sub"):
                    parts.append(f"<{local}>{inner}</{local}>")
                else:
                    parts.append(inner)
            parts.append(esc(ch.tail or ""))
        return "".join(parts)

    @staticmethod
    def _slice_tall(im, max_h):
        if im.height <= max_h:
            return [im]
        g = im.convert("L")
        w = g.width
        px = g.load()
        blank = []
        for y in range(g.height):
            dark = 0
            for x in range(0, w, 3):
                if px[x, y] < 200:
                    dark += 1
                    break
            blank.append(dark == 0)
        out, top = [], 0
        while g.height - top > max_h:
            cut = top + max_h
            y = cut
            while y > top + max_h // 3 and not blank[y]:
                y -= 1
            if y <= top + max_h // 3:
                y = cut
            out.append(im.crop((0, top, w, y)))
            top = y
        out.append(im.crop((0, top, w, g.height)))
        return out

    # ====================================================================== 17 cover
    def _find_cover_item(self):
        b = self.book
        for it in b.items():
            if it.has_prop("cover-image") and it.href in b.files:
                return it
        for m in b.metadata.iter(OPF + "meta"):
            if m.get("name") == "cover":
                it = b.item_by_id(m.get("content"))
                if it is not None and it.media_type.startswith("image/"):
                    return it
                it = b.item_by_path(b.abs(m.get("content") or ""))
                if it is not None and it.media_type.startswith("image/"):
                    return it
        for ref in b.opf.iter(OPF + "reference"):
            if (ref.get("type") or "").lower() == "cover":
                tgt = b.abs(ref.get("href", ""))
                it = b.item_by_path(tgt)
                if it is not None and it.media_type.startswith("image/"):
                    return it
                if tgt in b.files and re.search(r"\.x?html?$", tgt):
                    root = parse_xhtml(b.files[tgt])
                    for el in root.iter():
                        if isinstance(el.tag, str) and etree.QName(el).localname in ("img", "image"):
                            src = el.get("src") or el.get(XLINK) or el.get("href")
                            if src:
                                it = b.item_by_path(b.abs(src, tgt))
                                if it is not None:
                                    return it
        imgs = [it for it in b.items() if it.media_type.startswith("image/")]
        for it in imgs:
            if re.search(r"cover", posixpath.basename(it.href) + (it.id or ""), re.I):
                return it
        # first image in the first spine document, if that page is basically just the image
        sp = b.spine_items()
        if sp:
            root = self.docs.get(sp[0].href) if hasattr(self, "docs") else None
            if root is not None:
                text = "".join(root.find(H + "body").itertext() if root.find(H + "body") is not None else "").strip()
                for el in root.iter(H + "img"):
                    if len(text) < 200:
                        return b.item_by_path(b.abs(el.get("src", ""), sp[0].href))
        return None

    def _cover(self):
        b, o, dev = self.book, self.o, self.dev
        it = b.item_by_path(self.cover_path) if self.cover_path else None
        if it is None and o.gen_cover:
            from . import qtrender
            try:
                im = qtrender.make_cover(b.title(), ", ".join(b.authors()[:2]), dev.width, dev.height, self.kannada)
            except Exception as e:
                self.warnings.append(f"Could not generate a cover: {e}")
                return
            data, mt = imglib.encode(im, max(80, o.jpeg_quality))
            base = posixpath.join(b.opf_dir, "Images") if b.opf_dir else "Images"
            ipath = self._unique_path(posixpath.join(base, "inkforge-cover.jpg"))
            it = b.add_item(ipath, data, mt, iid="inkforge-cover-image")
            tpath = self._unique_path(posixpath.join(b.opf_dir, "Text", "inkforge-cover.xhtml").lstrip("/"))
            page = xhtml_page("Cover", f'<div class="ink-page"><img src="{b.rel(ipath, tpath)}" alt="Cover"/></div>',
                              [b.rel(self.eink_css, tpath)], "kn" if self.kannada else "en")
            t_it = b.add_item(tpath, page, "application/xhtml+xml", iid="inkforge-cover-page")
            self.docs[tpath] = parse_xhtml(page)
            ref = etree.Element(OPF + "itemref")
            ref.set("idref", t_it.id)
            b.spine.insert(0, ref)
            self.cover_path = ipath
            self.log("generated cover")
        if it is None:
            return
        # mark cover the EPUB2 and EPUB3 way
        for m in list(b.metadata.iter(OPF + "meta")):
            if m.get("name") == "cover":
                m.getparent().remove(m)
        m = etree.SubElement(b.metadata, OPF + "meta")
        m.set("name", "cover")
        m.set("content", it.id)
        if b.version.startswith("3"):
            for other in b.items():
                if other.has_prop("cover-image") and other.id != it.id:
                    props = " ".join(x for x in other.properties.split() if x != "cover-image")
                    if props:
                        other.el.set("properties", props)
                    else:
                        del other.el.attrib["properties"]
            if not it.has_prop("cover-image"):
                it.el.set("properties", ((it.properties or "") + " cover-image").strip())

    # ====================================================================== 18 TOC
    def _toc(self):
        b = self.book
        ncx_item = None
        tid = b.spine.get("toc")
        if tid:
            ncx_item = b.item_by_id(tid)
        if ncx_item is None:
            ncx_item = next((it for it in b.items() if it.media_type == "application/x-dtbncx+xml"), None)
        nav_item = next((it for it in b.items() if it.has_prop("nav")), None)

        entries = None
        if ncx_item is not None and ncx_item.href in b.files:
            entries = self._read_ncx(ncx_item.href)
        if not entries and nav_item is not None:
            entries = self._read_nav(nav_item.href)
        if not entries:
            entries = self._toc_from_headings()
            if entries:
                self.warnings.append("Built a table of contents from chapter headings.")
        # keep only entries that point to existing documents
        entries = [(lvl, normalize(" ".join(t.split())), tgt) for lvl, t, tgt in entries
                   if b.abs(tgt.split("#")[0]) in b.files and t.strip()]
        ncx = self._build_ncx(entries)
        if ncx_item is None:
            path = posixpath.join(b.opf_dir, "toc.ncx").lstrip("/")
            ncx_item = b.add_item(self._unique_path(path), ncx, "application/x-dtbncx+xml", iid=b.new_id("ncx"))
        else:
            b.files[ncx_item.href] = ncx
        b.spine.set("toc", ncx_item.id)
        if b.version.startswith("3") and nav_item is None:
            path = self._unique_path(posixpath.join(b.opf_dir, "nav.xhtml").lstrip("/"))
            nav = self._build_nav(entries, path)
            b.add_item(path, nav, "application/xhtml+xml", properties="nav", iid=b.new_id("nav"))

    def _read_ncx(self, path):
        b = self.book
        try:
            root = etree.fromstring(b.files[path], XML_PARSER)
        except Exception:
            return []
        out = []

        def walk(el, lvl):
            for np in el.findall("{%s}navPoint" % NS["ncx"]):
                lab = np.find(".//{%s}text" % NS["ncx"])
                con = np.find("{%s}content" % NS["ncx"])
                if con is not None and con.get("src"):
                    tgt = b.abs(con.get("src"), path)
                    frag = con.get("src").split("#", 1)[1] if "#" in con.get("src") else ""
                    out.append((lvl, "".join(lab.itertext()) if lab is not None else "", b.opf_href(tgt) + ("#" + frag if frag else "")))
                walk(np, lvl + 1)
        nm = root.find("{%s}navMap" % NS["ncx"])
        if nm is not None:
            walk(nm, 1)
        return out

    def _read_nav(self, path):
        b = self.book
        root = self.docs.get(path)
        if root is None:
            return []
        out = []
        navs = [n for n in root.iter(H + "nav") if "toc" in (n.get("{%s}type" % NS["epub"]) or "")] or list(root.iter(H + "nav"))
        if not navs:
            return []

        def walk(ol, lvl):
            for li in ol.findall(H + "li"):
                a = li.find(H + "a")
                if a is not None and a.get("href"):
                    href = a.get("href")
                    tgt = b.abs(href, path)
                    frag = href.split("#", 1)[1] if "#" in href else ""
                    out.append((lvl, "".join(a.itertext()), b.opf_href(tgt) + ("#" + frag if frag else "")))
                sub = li.find(H + "ol")
                if sub is not None:
                    walk(sub, lvl + 1)
        ol = navs[0].find(H + "ol")
        if ol is not None:
            walk(ol, 1)
        return out

    def _toc_from_headings(self):
        b = self.book
        out = []
        for it in b.spine_items():
            root = self.docs.get(it.href)
            if root is None:
                continue
            found = False
            for el in root.iter(H + "h1", H + "h2"):
                t = " ".join("".join(el.itertext()).split())
                if not t or len(t) > 120:
                    continue
                if not el.get("id"):
                    el.set("id", "ink_" + uuid.uuid4().hex[:8])
                lvl = 1 if el.tag == H + "h1" else 2
                out.append((lvl, t, b.opf_href(it.href) + "#" + el.get("id")))
                found = True
            if not found:
                t = root.find(H + "head/" + H + "title")
                title = " ".join("".join(t.itertext()).split()) if t is not None else ""
                if title and title != b.title() and len(title) < 120:
                    out.append((1, title, b.opf_href(it.href)))
        if not out:
            for k, it in enumerate(b.spine_items()):
                out.append((1, f"Section {k + 1}", b.opf_href(it.href)))
        # normalize levels so the first level is 1
        if out and min(l for l, _, _ in out) > 1:
            m = min(l for l, _, _ in out)
            out = [(l - m + 1, t, h) for l, t, h in out]
        return out

    def _build_ncx(self, entries):
        b = self.book
        uid = next((("".join(e.itertext())).strip() for e in b.dc("identifier")), "urn:uuid:" + str(uuid.uuid4()))
        ncx_dir = b.opf_dir
        parts = ['<?xml version="1.0" encoding="utf-8"?>\n<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">',
                 f'<head><meta name="dtb:uid" content="{esc(uid)}"/><meta name="dtb:depth" content="{max([l for l, _, _ in entries] or [1])}"/>'
                 '<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>',
                 f'<docTitle><text>{esc(b.title())}</text></docTitle><navMap>']
        stack = []
        n = 0
        for lvl, title, href in entries:
            lvl = max(1, min(lvl, (stack[-1] + 1) if stack else 1))
            while stack and stack[-1] >= lvl:
                parts.append("</navPoint>")
                stack.pop()
            n += 1
            parts.append(f'<navPoint id="np{n}" playOrder="{n}"><navLabel><text>{esc(title)}</text></navLabel>'
                         f'<content src="{esc(href)}"/>')
            stack.append(lvl)
        parts.extend("</navPoint>" for _ in stack)
        parts.append("</navMap></ncx>")
        _ = ncx_dir
        return "".join(parts).encode("utf-8")

    def _build_nav(self, entries, nav_path):
        b = self.book
        html = ['<nav epub:type="toc" id="toc"><h1>Contents</h1><ol>']
        stack = [1]
        first = True
        for lvl, title, href in entries:
            tgt = b.abs(href.split("#")[0])
            frag = "#" + href.split("#", 1)[1] if "#" in href else ""
            link = b.rel(tgt, nav_path) + frag
            if not first:
                if lvl > stack[-1]:
                    html.append("<ol>")
                    stack.append(stack[-1] + 1)
                else:
                    html.append("</li>")
                    while lvl < stack[-1] and len(stack) > 1:
                        html.append("</ol></li>")
                        stack.pop()
            html.append(f'<li><a href="{esc(link)}">{esc(title)}</a>')
            first = False
        if not first:
            html.append("</li>")
        while len(stack) > 1:
            html.append("</ol></li>")
            stack.pop()
        html.append("</ol></nav>")
        return xhtml_page("Contents", "".join(html), lang="kn" if self.kannada else "en")

    # ====================================================================== 19 package
    def _drop_unused(self):
        b = self.book
        used = set()
        for p, root in self.docs.items():
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue
                for attr in ("src", "href", XLINK, "data", "poster"):
                    v = el.get(attr)
                    if v and not re.match(r"^[a-z]+:", v):
                        used.add(b.abs(v, p))
                if el.get("style") and "url(" in el.get("style"):
                    for u in csslib.css_urls(el.get("style")):
                        used.add(b.abs(u, p))
            for st in root.iter(H + "style"):
                for u in csslib.css_urls(st.text or ""):
                    used.add(b.abs(u, p))
        for it in b.items():
            if it.media_type == "text/css" and it.href in used:
                for u in csslib.css_urls(b.files[it.href].decode("utf-8", "replace")):
                    used.add(b.abs(u, it.href))
        if self.cover_path:
            used.add(self.cover_path)
        removed = 0
        for it in b.items():
            if it.has_prop("nav") or it.has_prop("cover-image") or it.media_type in ("application/xhtml+xml", "application/x-dtbncx+xml"):
                continue
            if (it.media_type == "text/css" or it.media_type.startswith("image/")) and it.href not in used:
                b.remove_item(it)
                removed += 1
        if removed:
            self.log(f"removed {removed} unused files")

    def _package(self) -> bytes:
        b = self.book
        for p, root in self.docs.items():
            if p in b.files:
                b.files[p] = serialize_xhtml(root)
        # remove zip entries no longer in the manifest (except META-INF)
        keep = {it.href for it in b.items()} | {b.opf_path}
        for name in list(b.files):
            if not name.startswith("META-INF/") and name not in keep:
                del b.files[name]
        data = b.to_bytes()
        # verify: the result must open and every chapter must parse
        try:
            check = Epub(read_zip_bytes(data))
            for it in check.spine_items():
                etree.fromstring(check.files[it.href], XML_PARSER)
            if not check.spine_items():
                raise BookError("verification failed: no chapters")
        except BookError:
            raise
        except Exception as e:
            raise BookError(f"The optimized book failed its final check ({e}).")
        return data
