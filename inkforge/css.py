"""CSS simplification for Xteink firmware (CrossPoint/CrossInk understands a small CSS subset)."""
from __future__ import annotations

import re

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_FONT_FACE = re.compile(r"@font-face\s*\{[^}]*\}", re.I)
_IMPORT = re.compile(r"@import[^;]*;", re.I)
_CHARSET = re.compile(r"@charset[^;]*;", re.I)
_NAMESPACE = re.compile(r"@namespace[^;]*;", re.I)

KEEP_PROPS = {
    "text-align", "text-align-last", "text-indent", "font-style", "font-weight", "font-size", "font-variant",
    "text-decoration", "text-decoration-line", "text-transform", "vertical-align", "line-height", "display",
    "white-space", "list-style", "list-style-type", "list-style-position",
    "margin", "margin-top", "margin-bottom", "margin-left", "margin-right",
    "padding", "padding-top", "padding-bottom", "padding-left", "padding-right",
    "page-break-before", "page-break-after", "page-break-inside", "break-before", "break-after", "break-inside",
    "border", "border-top", "border-bottom", "border-left", "border-right", "border-collapse", "border-width",
    "border-style", "border-spacing", "width", "height", "max-width", "max-height", "min-height",
    "hyphens", "-webkit-hyphens", "-epub-hyphens", "widows", "orphans", "font-family", "caption-side",
    "empty-cells", "table-layout", "clear",
}
HORIZONTAL = {"margin-left", "margin-right", "padding-left", "padding-right"}
_LEN = re.compile(r"(-?\d*\.?\d+)(px|pt|pc|cm|mm|in|em|rem|%)?", re.I)


def _to_em(num: float, unit: str) -> float | None:
    unit = (unit or "").lower()
    return {"px": num / 16, "pt": num / 12, "pc": num, "cm": num * 2.36, "mm": num * 0.236,
            "in": num * 6, "em": num, "rem": num, "": None}.get(unit)


def _fmt(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def clean_value(prop: str, value: str, remove_fonts: bool) -> str | None:
    v = value.strip()
    low = v.lower()
    if not v or "expression(" in low or "url(" in low and prop not in ("list-style",):
        return None
    if prop == "font-family":
        if remove_fonts:
            return "monospace" if "mono" in low or "courier" in low else None
        return v
    if prop == "display" and low not in ("none", "block", "inline", "inline-block", "list-item", "table",
                                         "table-row", "table-cell", "table-header-group", "table-row-group"):
        return None
    if prop in ("position",):
        return None
    if prop == "font-size":
        m = _LEN.fullmatch(low.replace(" ", ""))
        if m and m.group(2) and m.group(2) not in ("em", "rem", "%"):
            em = _to_em(float(m.group(1)), m.group(2))
            if em is None:
                return None
            em = min(max(em, 0.6), 2.4)
            return _fmt(em) + "em"
        if m and m.group(2) in ("em", "rem"):
            em = min(max(float(m.group(1)), 0.6), 2.4)
            return _fmt(em) + "em"
        return v
    if prop == "line-height":
        m = _LEN.fullmatch(low.replace(" ", ""))
        if m and m.group(2) in ("px", "pt"):
            return None
        return v
    if prop in ("width", "height", "max-width", "max-height", "min-height"):
        # fixed pixel boxes break on a 480 px screen
        if re.search(r"\d(px|pt|cm|mm|in)", low):
            return None
        return v
    if prop in HORIZONTAL or prop in ("margin", "padding"):
        def clamp(m):
            num, unit = float(m.group(1)), (m.group(2) or "").lower()
            if unit == "%":
                return _fmt(min(num, 4)) + "%"
            em = _to_em(num, unit)
            if em is None:
                return m.group(0)
            limit = 1.5 if prop in HORIZONTAL or prop in ("margin", "padding") else 3
            return _fmt(max(min(em, limit), -limit)) + "em" if em else "0"
        return _LEN.sub(clamp, v)
    return v


def clean_declarations(block: str, remove_fonts: bool) -> str:
    out = []
    for decl in block.split(";"):
        if ":" not in decl:
            continue
        prop, val = decl.split(":", 1)
        prop = prop.strip().lower()
        if prop not in KEEP_PROPS:
            continue
        important = "!important" in val
        val = val.replace("!important", "")
        nv = clean_value(prop, val, remove_fonts)
        if nv is None:
            continue
        out.append(f"{prop}:{nv}{' !important' if important else ''}")
    return ";".join(out)


def strip_font_faces(css: str) -> str:
    return _FONT_FACE.sub("", css)


def _flatten_media(css: str) -> str:
    """Unwrap @media blocks meant for screens/e-readers, drop print and Kindle-MOBI-only ones."""
    out, i = [], 0
    while True:
        m = re.search(r"@media([^{]*)\{", css[i:], re.I)
        if not m:
            out.append(css[i:])
            break
        start = i + m.start()
        out.append(css[i:start])
        depth, j = 1, i + m.end()
        while j < len(css) and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        query = m.group(1).lower()
        inner = css[i + m.end(): j - 1]
        if not any(x in query for x in ("print", "amzn-mobi", "amzn-kf8", "speech", "min-width", "orientation: landscape")):
            out.append(inner)
        i = j
    return "".join(out)


def clean_css(css: str, remove_fonts: bool) -> str:
    css = _COMMENT.sub("", css)
    css = _IMPORT.sub("", css)
    css = _CHARSET.sub("", css)
    css = _NAMESPACE.sub("", css)
    if remove_fonts:
        css = strip_font_faces(css)
    css = _flatten_media(css)
    css = re.sub(r"@(page|-webkit-keyframes|keyframes|supports)[^{]*\{(?:[^{}]*\{[^}]*\})*[^}]*\}", "", css, flags=re.I)
    rules = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        sel = " ".join(sel.split())
        if not sel or sel.startswith("@"):
            if sel.lower().startswith("@font-face"):
                rules.append(f"{sel}{{{body.strip()}}}")
            continue
        decls = clean_declarations(body, remove_fonts)
        if decls:
            rules.append(f"{sel}{{{decls}}}")
    return "\n".join(rules) + "\n"


def remove_font_family(css: str) -> str:
    """Remove-fonts without full CSS cleanup: drop @font-face and font-family only."""
    css = strip_font_faces(css)
    return re.sub(r"font-family\s*:[^;}]*;?", "", css, flags=re.I)


def css_urls(css: str) -> list[str]:
    return [u.strip("'\" ") for u in re.findall(r"url\(([^)]*)\)", css)]


EINK_CSS = """/* InkForge e-ink base styles */
img{max-width:100%;height:auto}
table{border-collapse:collapse;max-width:100%}
td,th{padding:0.15em 0.3em}
pre{white-space:pre-wrap;font-size:0.85em}
code{font-size:0.9em}
sup,sub{font-size:0.75em;line-height:0}
.ink-page{text-align:center;margin:0;padding:0;text-indent:0}
.ink-page img{max-width:100%;max-height:100%}
.ink-spread{page-break-before:always;break-before:page;text-align:center;text-indent:0}
.kn-img{margin:0 0 0.35em 0;padding:0;text-indent:0;text-align:left}
.kn-img img{max-width:100%}
.pdf-h1,.pdf-h2,.pdf-h3{text-indent:0}
p.pdf-p{text-indent:1.2em;margin:0}
p.pdf-first{text-indent:0}
.pdf-fig{text-align:center;text-indent:0;margin:0.5em 0}
"""
