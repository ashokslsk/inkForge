"""Image reflow (k2pdfopt-style) for PDFs without a usable text layer.

Printed lines are found, cut into word images, scaled so a line is `target_lh` pixels tall and re-wrapped to
the screen width. Works for any script (legacy-font Kannada, scans) because nothing is recognised: the reader
sees the original print, just larger and re-wrapped.
"""
from __future__ import annotations

import numpy as np
from PIL import Image


def _runs(mask: np.ndarray):
    """Start/end (exclusive) indexes of True runs."""
    m = np.concatenate(([False], mask, [False]))
    d = np.diff(m.astype(np.int8))
    return list(zip(np.where(d == 1)[0], np.where(d == -1)[0]))


def reflow_page(im: Image.Image, target_w: int, target_lh: int, screen_h: int):
    """Return [("text"|"img", PIL.Image)], [] for a blank page, or None if the page is not text-like."""
    g = np.asarray(im.convert("L"))
    ink = g < 150
    H, W = ink.shape
    if ink.mean() < 0.002:
        return []
    rows = ink.sum(axis=1) > max(1, W * 0.001)
    bands = [[a, b] for a, b in _runs(rows)]
    # close tiny gaps (broken glyphs)
    merged = []
    for b in bands:
        if merged and b[0] - merged[-1][1] <= 2:
            merged[-1][1] = b[1]
        else:
            merged.append(b)
    bands = merged
    if len(bands) < 3:
        return None
    hs = np.array([b[1] - b[0] for b in bands])
    M = float(np.median(hs[hs > 4])) if (hs > 4).any() else float(np.median(hs))
    # attach thin bands (Kannada subscript conjuncts, matras above) to the nearest line
    out = []
    for b in bands:
        h = b[1] - b[0]
        if out and h < 0.45 * M and b[0] - out[-1][1] < 0.45 * M:
            out[-1][1] = b[1]
        else:
            out.append(b)
    fixed = []
    for b in out:
        h = b[1] - b[0]
        if fixed and (fixed[-1][1] - fixed[-1][0]) < 0.45 * M and b[0] - fixed[-1][1] < 0.45 * M:
            fixed[-1][1] = b[1]
        else:
            fixed.append(b)
    bands = fixed
    hs = np.array([b[1] - b[0] for b in bands])
    M = float(np.median(hs))
    text_like = (hs < 2.2 * M).sum()
    if text_like < 0.5 * len(bands) or M < 6:
        return None

    lines = []  # dicts: y0,y1,x0,x1,words[(x0,x1)],fig
    for y0, y1 in bands:
        sl = ink[y0:y1]
        cols = sl.any(axis=0)
        xr = _runs(cols)
        if not xr:
            continue
        x0, x1 = xr[0][0], xr[-1][1]
        h = y1 - y0
        if h > 2.2 * M:
            lines.append({"y0": y0, "y1": y1, "x0": x0, "x1": x1, "fig": True})
            continue
        gap_thr = max(3, 0.28 * M)
        words, cur = [], list(xr[0])
        for a, b in xr[1:]:
            if a - cur[1] < gap_thr:
                cur[1] = b
            else:
                words.append(tuple(cur))
                cur = [a, b]
        words.append(tuple(cur))
        # baseline: lowest row of the dense core of the line
        dens = sl.sum(axis=1)
        core = np.where(dens >= 0.45 * dens.max())[0]
        base = int(core[-1]) + 1 if len(core) else h
        lines.append({"y0": y0, "y1": y1, "x0": x0, "x1": x1, "words": words, "base": base, "fig": False})
    if not lines:
        return []
    tl = [l for l in lines if not l["fig"]]
    if not tl:
        return None
    L = float(np.percentile([l["x0"] for l in tl], 15))
    R = float(np.percentile([l["x1"] for l in tl], 85))
    # drop running heads / page numbers: a short line at the very top or bottom
    def furniture(l, k):
        short = (l["x1"] - l["x0"]) < (R - L) * 0.35
        return short and ((k == 0 and l["y0"] < H * 0.09) or (k == len(lines) - 1 and l["y1"] > H * 0.9))
    lines = [l for k, l in enumerate(lines) if not furniture(l, k)]
    gaps = [lines[k + 1]["y0"] - lines[k]["y1"] for k in range(len(lines) - 1)]
    med_gap = float(np.median(gaps)) if gaps else M * 0.4

    s = max(0.35, min(3.0, target_lh / M))
    space = int(0.38 * target_lh)
    indent = int(1.1 * target_lh)
    src = im.convert("L")

    # group into paragraphs
    paras, cur = [], None
    for k, l in enumerate(lines):
        if l["fig"]:
            if cur:
                paras.append(cur)
                cur = None
            paras.append({"fig": l})
            continue
        prev = lines[k - 1] if k > 0 and not lines[k - 1]["fig"] else None
        centered = l["x0"] > L + 2 * M and l["x1"] < R - 2 * M
        start = (cur is None or prev is None or l["x0"] > L + 0.8 * M or centered or cur.get("centered")
                 or prev["x1"] < R - 2.5 * M or (l["y0"] - prev["y1"]) > med_gap * 1.8 + 2)
        if start:
            if cur:
                paras.append(cur)
            cur = {"lines": [], "centered": centered, "indent": l["x0"] > L + 0.8 * M and not centered}
        cur["lines"].append(l)
    if cur:
        paras.append(cur)

    pieces = []
    chunk_max = int(screen_h * 0.3)
    lead = int(target_lh * 0.22)
    para_gap = int(target_lh * 0.45)
    chunk, ch_h = [], 0          # [(line_image, gap_before)]

    def flush_chunk():
        nonlocal chunk, ch_h
        if chunk:
            pieces.append(("text", _stack(chunk, target_w)))
        chunk, ch_h = [], 0

    for para in paras:
        if "fig" in para:
            flush_chunk()
            f = para["fig"]
            crop = src.crop((max(0, f["x0"] - 4), f["y0"], min(W, f["x1"] + 4), f["y1"]))
            w = min(target_w, int(crop.width * s * 1.2))
            hh = max(1, int(crop.height * w / crop.width))
            if hh > screen_h * 0.9:
                hh = int(screen_h * 0.9)
                w = max(1, int(crop.width * hh / crop.height))
            pieces.append(("img", crop.resize((w, hh), Image.LANCZOS)))
            continue
        # word images scaled, each with its baseline offset
        words = []
        for l in para["lines"]:
            for (a, b) in l["words"]:
                crop = src.crop((a, l["y0"], b, l["y1"]))
                nw, nh = max(1, int(crop.width * s)), max(1, int(crop.height * s))
                words.append((crop.resize((nw, nh), Image.LANCZOS), int(l["base"] * s)))
        # wrap
        out_lines, cur_l, cur_w = [], [], indent if para["indent"] else 0
        first_line = True
        for w_im, base in words:
            ww = w_im.width
            if ww > target_w:  # very long word: scale it down to fit
                f = target_w / ww
                w_im = w_im.resize((target_w, max(1, int(w_im.height * f))), Image.LANCZOS)
                base = int(base * f)
                ww = target_w
            need = ww + (space if cur_l else 0)
            if cur_l and cur_w + need > target_w:
                out_lines.append((cur_l, first_line))
                first_line = False
                cur_l, cur_w = [], 0
                need = ww
            cur_l.append((w_im, base))
            cur_w += need
        if cur_l:
            out_lines.append((cur_l, first_line))
        for li, (ws, first) in enumerate(out_lines):
            above = max(b for _, b in ws)
            below = max(im_.height - b for im_, b in ws)
            line_im = Image.new("L", (target_w, above + below), 255)
            x = indent if (first and para["indent"]) else 0
            total = sum(i.width for i, _ in ws) + space * (len(ws) - 1)
            if para["centered"]:
                x = max(0, (target_w - total) // 2)
            for i, b in ws:
                line_im.paste(i, (x, above - b))
                x += i.width + space
            gap = (para_gap if li == 0 else lead) if chunk else 0
            if chunk and ch_h + gap + line_im.height > chunk_max:
                flush_chunk()
                gap = 0
            chunk.append((line_im, gap))
            ch_h += gap + line_im.height
    flush_chunk()
    return pieces


def _stack(lines, width):
    h = sum(l.height + g for l, g in lines)
    out = Image.new("L", (width, max(1, h)), 255)
    y = 0
    for l, g in lines:
        y += g
        out.paste(l, (0, y))
        y += l.height
    return out
