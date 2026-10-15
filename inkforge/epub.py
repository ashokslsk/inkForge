"""Minimal, forgiving EPUB container model: read (with zip salvage), edit, write."""
from __future__ import annotations

import io
import posixpath
import re
import struct
import zipfile
import zlib
from urllib.parse import unquote, quote

from lxml import etree

from .options import BookError

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "enc": "http://www.w3.org/2001/04/xmlenc#",
}
OPF = "{%s}" % NS["opf"]
DC = "{%s}" % NS["dc"]
XHTML_NS = NS["xhtml"]

XML_PARSER = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=True)

MEDIA_TYPES = {
    ".xhtml": "application/xhtml+xml", ".html": "application/xhtml+xml", ".htm": "application/xhtml+xml",
    ".css": "text/css", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp", ".bmp": "image/bmp",
    ".tif": "image/tiff", ".tiff": "image/tiff",
    ".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2",
    ".ncx": "application/x-dtbncx+xml", ".opf": "application/oebps-package+xml",
    ".js": "application/javascript", ".xpgt": "application/adobe-page-template+xml",
    ".smil": "application/smil+xml", ".mp3": "audio/mpeg", ".mp4": "video/mp4",
}


def guess_media_type(href: str) -> str:
    return MEDIA_TYPES.get(posixpath.splitext(href.lower())[1], "application/octet-stream")


def _salvage_zip(data: bytes) -> dict[str, bytes]:
    """Recover entries from a damaged ZIP by walking local file headers."""
    files: dict[str, bytes] = {}
    pos = 0
    while True:
        pos = data.find(b"PK\x03\x04", pos)
        if pos < 0 or pos + 30 > len(data):
            break
        try:
            (_, ver, flag, method, _, _, crc, csize, usize, nlen, xlen) = struct.unpack("<IHHHHHIIIHH", data[pos:pos + 30])
            name = data[pos + 30:pos + 30 + nlen].decode("utf-8", "replace")
            start = pos + 30 + nlen + xlen
            if flag & 0x08 or csize == 0:  # sizes in data descriptor: find next header
                nxt = data.find(b"PK\x03\x04", start)
                cen = data.find(b"PK\x01\x02", start)
                ends = [x for x in (nxt, cen) if x > 0]
                end = min(ends) if ends else len(data)
                raw = data[start:end]
            else:
                raw = data[start:start + csize]
            if method == 0:
                body = raw if not (flag & 0x08) else raw[:usize or len(raw)]
            elif method == 8:
                d = zlib.decompressobj(-15)
                body = d.decompress(raw)
            else:
                body = None
            if body is not None and not name.endswith("/"):
                files[name] = body
            pos = start
        except Exception:
            pos += 4
    return files


def read_zip(path: str) -> dict[str, bytes]:
    with open(path, "rb") as fh:
        return read_zip_bytes(fh.read())


def read_zip_bytes(data: bytes) -> dict[str, bytes]:
    if not data:
        raise BookError("The file is empty (0 bytes).")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            out = {}
            for info in zf.infolist():
                if info.is_dir():
                    continue
                try:
                    out[info.filename] = zf.read(info)
                except Exception:
                    pass  # one bad entry should not sink the book
            if out:
                return out
    except zipfile.BadZipFile:
        pass
    files = _salvage_zip(data)
    if not files:
        raise BookError("This file is not a valid EPUB (it is not a ZIP archive, or it is too damaged to recover).")
    return files


class Item:
    __slots__ = ("id", "href", "media_type", "properties", "el")

    def __init__(self, id, href, media_type, properties, el):
        self.id, self.href, self.media_type, self.properties, self.el = id, href, media_type, properties, el

    def has_prop(self, p):
        return p in (self.properties or "").split()


class Epub:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.opf_path = self._find_opf()
        self.opf_dir = posixpath.dirname(self.opf_path)
        self.opf = etree.fromstring(self.files[self.opf_path], XML_PARSER)
        if self.opf is None or etree.QName(self.opf).localname != "package":
            raise BookError("The book's package file (OPF) is unreadable.")
        self._ensure_sections()
        self.check_drm()

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str) -> "Epub":
        return cls(read_zip(path))

    def _find_opf(self) -> str:
        cont = self.files.get("META-INF/container.xml")
        if cont:
            try:
                root = etree.fromstring(cont, XML_PARSER)
                for rf in root.iter("{%s}rootfile" % NS["c"]):
                    p = unquote(rf.get("full-path", ""))
                    if p in self.files:
                        return p
            except Exception:
                pass
        opfs = sorted((n for n in self.files if n.lower().endswith(".opf")), key=len)
        if not opfs:
            raise BookError("This EPUB has no package file (OPF): it is missing its table of contents and spine.")
        return opfs[0]

    def _ensure_sections(self):
        for tag in ("metadata", "manifest", "spine"):
            if self.opf.find(OPF + tag) is None:
                # some books use no namespace on children
                el = self.opf.find(tag)
                if el is not None:
                    for e in self.opf.iter():
                        if isinstance(e.tag, str) and not e.tag.startswith("{"):
                            e.tag = OPF + e.tag
                else:
                    etree.SubElement(self.opf, OPF + tag)
        self.metadata = self.opf.find(OPF + "metadata")
        self.manifest = self.opf.find(OPF + "manifest")
        self.spine = self.opf.find(OPF + "spine")

    def check_drm(self):
        if "META-INF/rights.xml" in self.files:
            raise BookError("This EPUB is DRM-protected (Adobe ADEPT). Only DRM-free books can be optimized.")
        enc = self.files.get("META-INF/encryption.xml")
        if not enc:
            return
        try:
            root = etree.fromstring(enc, XML_PARSER)
        except Exception:
            return
        font_algos = ("http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC")
        for ed in root.iter("{%s}EncryptedData" % NS["enc"]):
            m = ed.find("{%s}EncryptionMethod" % NS["enc"])
            algo = m.get("Algorithm", "") if m is not None else ""
            cr = ed.find(".//{%s}CipherReference" % NS["enc"])
            uri = unquote(cr.get("URI", "")) if cr is not None else ""
            if algo not in font_algos and not re.search(r"\.(ttf|otf|woff2?)$", uri, re.I):
                raise BookError("This EPUB is DRM-protected (its content is encrypted). Only DRM-free books can be optimized.")

    # ------------------------------------------------------------------ paths
    def abs(self, href: str, base: str | None = None) -> str:
        """Resolve an href (relative to base file, or the OPF) to a zip path."""
        href = unquote(href.split("#", 1)[0])
        b = posixpath.dirname(base) if base is not None else self.opf_dir
        return posixpath.normpath(posixpath.join(b, href)).lstrip("/") if href else (base or "")

    def rel(self, target: str, base: str) -> str:
        r = posixpath.relpath(target, posixpath.dirname(base) or ".")
        return quote(r, safe="/#-_.~!$&'()*+,;=:@")

    def opf_href(self, path: str) -> str:
        return posixpath.relpath(path, self.opf_dir or ".")

    # ------------------------------------------------------------------ manifest
    def items(self) -> list[Item]:
        out = []
        for el in self.manifest.findall(OPF + "item"):
            href = el.get("href")
            if not href:
                continue
            out.append(Item(el.get("id"), self.abs(href), el.get("media-type") or guess_media_type(href),
                            el.get("properties"), el))
        return out

    def item_by_id(self, iid):
        for it in self.items():
            if it.id == iid:
                return it
        return None

    def item_by_path(self, path):
        for it in self.items():
            if it.href == path:
                return it
        return None

    def spine_items(self) -> list[Item]:
        byid = {it.id: it for it in self.items()}
        out = []
        for ref in self.spine.findall(OPF + "itemref"):
            it = byid.get(ref.get("idref"))
            if it is not None and it.href in self.files:
                out.append(it)
        return out

    def new_id(self, base: str) -> str:
        ids = {el.get("id") for el in self.opf.iter() if isinstance(el.tag, str)}
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", base) or "id"
        if not base[0].isalpha():
            base = "x" + base
        cand, n = base, 1
        while cand in ids:
            n += 1
            cand = f"{base}_{n}"
        return cand

    def add_item(self, path: str, data: bytes, media_type: str | None = None, properties: str | None = None,
                 iid: str | None = None) -> Item:
        self.files[path] = data
        existing = self.item_by_path(path)
        if existing is not None:
            if media_type:
                existing.el.set("media-type", media_type)
            return existing
        el = etree.SubElement(self.manifest, OPF + "item")
        el.set("id", iid or self.new_id(posixpath.basename(path)))
        el.set("href", quote(self.opf_href(path), safe="/-_.~!$&'()*+,;=:@"))
        el.set("media-type", media_type or guess_media_type(path))
        if properties:
            el.set("properties", properties)
        return Item(el.get("id"), path, el.get("media-type"), properties, el)

    def remove_item(self, it: Item):
        self.files.pop(it.href, None)
        if it.el.getparent() is not None:
            it.el.getparent().remove(it.el)
        for ref in list(self.spine.findall(OPF + "itemref")):
            if ref.get("idref") == it.id:
                self.spine.remove(ref)

    def rename_item(self, it: Item, new_path: str, media_type: str | None = None):
        data = self.files.pop(it.href)
        self.files[new_path] = data
        it.el.set("href", quote(self.opf_href(new_path), safe="/-_.~!$&'()*+,;=:@"))
        if media_type:
            it.el.set("media-type", media_type)
        it.href = new_path

    @property
    def version(self) -> str:
        return self.opf.get("version", "2.0")

    # ------------------------------------------------------------------ metadata helpers
    def dc(self, name) -> list:
        return self.metadata.findall(DC + name)

    def dc_text(self, name) -> str:
        for el in self.dc(name):
            t = "".join(el.itertext()).strip()
            if t:
                return t
        return ""

    def title(self):
        return self.dc_text("title")

    def authors(self):
        return [("".join(e.itertext())).strip() for e in self.dc("creator") if "".join(e.itertext()).strip()]

    # ------------------------------------------------------------------ writing
    def serialize_opf(self) -> bytes:
        return etree.tostring(self.opf, xml_declaration=True, encoding="utf-8", pretty_print=True)

    def to_bytes(self) -> bytes:
        self.files[self.opf_path] = self.serialize_opf()
        self.files["META-INF/container.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            f'  <rootfiles><rootfile full-path="{self.opf_path}" media-type="application/oebps-package+xml"/></rootfiles>\n'
            '</container>\n').encode()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
            order = ["META-INF/container.xml", self.opf_path]
            rest = sorted(n for n in self.files if n not in order and n != "mimetype")
            for name in order + rest:
                if name not in self.files:
                    continue
                data = self.files[name]
                comp = zipfile.ZIP_STORED if re.search(r"\.(jpe?g|png|gif)$", name, re.I) else zipfile.ZIP_DEFLATED
                zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                zi.compress_type = comp
                zf.writestr(zi, data, compresslevel=9 if comp == zipfile.ZIP_DEFLATED else None)
        return buf.getvalue()


# ---------------------------------------------------------------------- XHTML helpers
def parse_xhtml(data: bytes):
    """Parse XHTML/HTML forgivingly; always returns an XHTML-namespaced tree root."""
    root = None
    try:
        root = etree.fromstring(data, XML_PARSER)
    except Exception:
        root = None
    if root is None or etree.QName(root).localname.lower() != "html" or not root.tag.startswith("{"):
        from lxml import html as lhtml
        try:
            doc = lhtml.document_fromstring(data)
        except Exception:
            text = data.decode("utf-8", "replace")
            doc = lhtml.document_fromstring("<html><body><pre>%s</pre></body></html>" % (
                text.replace("&", "&amp;").replace("<", "&lt;")))
        root = html_to_xhtml(doc)
    return root


def html_to_xhtml(doc):
    """Copy an lxml.html tree into a namespaced XHTML tree."""
    def conv(src, parent=None):
        if not isinstance(src.tag, str):
            return None  # comments / processing instructions
        tag = src.tag.split("}")[-1].lower()
        if not re.match(r"^[a-z][a-z0-9_-]*$", tag):
            tag = "span"
        qn = "{%s}%s" % (XHTML_NS, tag)
        el = etree.Element(qn, nsmap={None: XHTML_NS}) if parent is None else etree.SubElement(parent, qn)
        for k, v in src.attrib.items():
            if re.match(r"^[A-Za-z_][\w.-]*$", k):
                try:
                    el.set(k, v)
                except Exception:
                    pass
        el.text = src.text
        for ch in src:
            c = conv(ch, el)
            if c is None and ch.tail:
                # keep tail text of dropped nodes
                if len(el):
                    el[-1].tail = (el[-1].tail or "") + ch.tail
                else:
                    el.text = (el.text or "") + ch.tail
            elif c is not None:
                c.tail = ch.tail
        return el
    return conv(doc)


def serialize_xhtml(root) -> bytes:
    body = etree.tostring(root, encoding="utf-8", xml_declaration=False, method="xml")
    return b'<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n' + body


def xhtml_page(title: str, body_html: str, css_hrefs=(), lang: str = "en") -> bytes:
    links = "".join(f'<link rel="stylesheet" type="text/css" href="{h}"/>' for h in css_hrefs)
    t = (title or "").replace("&", "&amp;").replace("<", "&lt;")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">'
            f'<head><meta charset="utf-8"/><title>{t}</title>{links}</head><body>{body_html}</body></html>').encode("utf-8")


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
