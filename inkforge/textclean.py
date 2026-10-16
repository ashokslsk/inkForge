"""Text cleanup for English and Kannada: encoding repair, Unicode normalization, spacing, OCR fixes."""
from __future__ import annotations

import re
import unicodedata

KANNADA_RE = re.compile(r"[ಀ-೿]")
LATIN_RE = re.compile(r"[A-Za-z]")

# mojibake: UTF-8 bytes decoded as cp1252 / latin-1 (e.g. "â€™" for "’", "Ã©" for "é")
_MOJIBAKE_RE = re.compile(
    "[Â-ô][\u0080-¿ŒœŠšŸŽžƒˆ˜"
    "–—‘’‚“”„†‡•…‰‹›€™]{1,3}")

LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}
# characters that small e-reader fonts often lack -> safe equivalents
CHAR_MAP = {
    "‐": "-", "‑": "-", "‒": "–", "―": "—", "−": "-", "⁃": "-",
    "  ": " ", " ": " ", " ": " ", "​": "", "﻿": "", "⁠": "",
    "­­": "­", " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ", "‧": "·",
    "￼": "",
}
CHAR_MAP["\ufffd"] = ""   # replacement characters carry no information
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_PUA_RE = re.compile(r"[-]")


def fix_mojibake(s: str) -> str:
    if not _MOJIBAKE_RE.search(s):
        return s

    def repl(m):
        seg = m.group(0)
        for enc in ("cp1252", "latin-1"):
            try:
                return seg.encode(enc).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        return seg
    return _MOJIBAKE_RE.sub(repl, s)


def normalize(s: str) -> str:
    """NFC (composes Kannada two-part vowel signs, e.g. ೆ+ೂ -> ೊ) and remove junk control chars.
    ZWJ / ZWNJ are kept: they change how Kannada conjuncts are drawn."""
    s = unicodedata.normalize("NFC", s)
    return _CTRL_RE.sub("", s)


_SPACE_BEFORE_PUNCT = re.compile(r"(?<=[\wಀ-೿\)\]”’\"'])[ \t]+([,.;:!?)\]](?!\w))")
_SPACE_AFTER_OPEN = re.compile(r"([(\[“])[ \t]+")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_MISSING_SPACE = re.compile(r"(?<=[a-z]{2})([.!?;])(?=[A-Z][a-z])")
_APOS_GAP = re.compile(r"(\w) ?[’'] (s|t|ll|re|ve|d|m)\b")
_DOUBLE_PUNCT = re.compile(r"([,;:])\1+")
_KN_ZERO_ANUSVARA = re.compile(r"(?<=[\u0C85-\u0CB9\u0CBC-\u0CCC])\u0CE6")
_KN_DANDA_SPACE = re.compile(r"[ \t]+([।॥])")


def cleanup_text(s: str, kannada: bool = False) -> str:
    """Spacing/OCR fixes for a run of text inside one element (no markup)."""
    if not s:
        return s
    s = fix_mojibake(s)
    s = normalize(s)
    for a, b in LIGATURES.items():
        if a in s:
            s = s.replace(a, b)
    for a, b in CHAR_MAP.items():
        if a in s:
            s = s.replace(a, b)
    s = _MULTI_SPACE.sub(" ", s)
    s = _SPACE_BEFORE_PUNCT.sub(r"\1", s)
    s = _SPACE_AFTER_OPEN.sub(r"\1", s)
    s = _DOUBLE_PUNCT.sub(r"\1", s)
    s = _APOS_GAP.sub(r"\1’\2", s)
    if not kannada:
        s = _MISSING_SPACE.sub(r"\1 ", s)
    else:
        s = _KN_DANDA_SPACE.sub(r"\1", s)
        s = _KN_ZERO_ANUSVARA.sub("\u0C82", s)   # OCR: Kannada digit ೦ after a letter is really anusvara ಂ
    return s


def script_stats(text: str) -> tuple[int, int]:
    return len(KANNADA_RE.findall(text)), len(LATIN_RE.findall(text))


def is_kannada_text(text: str, threshold: float = 0.25) -> bool:
    kn, la = script_stats(text)
    return kn > 50 and kn / max(1, kn + la) >= threshold


def pua_ratio(text: str) -> float:
    t = re.sub(r"\s", "", text)
    return len(_PUA_RE.findall(t)) / max(1, len(t))


def dehyphenate_join(a: str, b: str) -> str:
    """Join two PDF lines, removing an end-of-line hyphen when the word continues."""
    a = a.rstrip()
    b = b.lstrip()
    if not a:
        return b
    if not b:
        return a
    if a.endswith("-") and len(a) > 1 and a[-2].isalpha() and b[:1].islower():
        return a[:-1] + b
    if a.endswith("­"):
        return a[:-1] + b
    return a + " " + b


# ---------------------------------------------------------------------- Kannada text-layer validation
_KN_SIGN = "಼ಾ-್ೕೖೢೣ"           # nukta, vowel signs, virama, length marks
_KN_CONS = "ಕ-ಹೞ"
_BAD_KN = re.compile(
    rf"(?:(?<![{_KN_CONS}಼‌‍])[{_KN_SIGN}])"         # sign not after a consonant
    rf"|(?:[ಾ-ೌ][ಾ-್])"                          # two signs in a row
    rf"|(?:್[ಾ-ೌ])"                                   # virama followed by vowel sign
    rf"|(?:(?<![ಀ-೿‌‍])[ಂಃ])"            # anusvara/visarga alone
    r"|�")


def kannada_quality(text: str) -> float:
    """Share of Kannada characters in well-formed positions (1.0 = clean Unicode text layer)."""
    kn = len(KANNADA_RE.findall(text))
    if kn == 0:
        return 1.0
    bad = len(_BAD_KN.findall(text))
    return max(0.0, 1.0 - bad / kn * 4)


_LEGACY_LATIN = re.compile(r"[À-ÿ¡-¿]")


def looks_like_legacy_kannada(text: str) -> bool:
    """Nudi/Baraha ASCII-encoded Kannada shows up as dense Latin-1 accented letters."""
    t = re.sub(r"\s", "", text)
    if len(t) < 80:
        return False
    return len(_LEGACY_LATIN.findall(t)) / len(t) > 0.12


# ---------------------------------------------------------------------- Word/ActualText duplicate repair
_REPEAT = re.compile(r"(\S[^\n]{1,40}?)([ \t]*)\1")
_ORPHAN_SIGNS = re.compile(r"(?<=\s)[ಾ-್ಂಃೕೖ]+(?=\s|$)")
_PUNCT = re.compile(r"[.,:;()\[\]!?\-–—\"“”'’।॥]")


def dedupe_kannada(s: str) -> str:
    """Remove fragments that some PDF producers (MS Word + Indic fonts) emit twice:
    'ಸಾರಾಂಶ:ಾರಾಂಶ:-' -> 'ಸಾರಾಂಶ:-', 'ಆತ್ಮಪ್ರಿಯತೆ ಯತೆ' -> 'ಆತ್ಮಪ್ರಿಯತೆ', '(ವೇದ). (ವೇದ).' -> '(ವೇದ).'
    Genuine reduplication between whole words ('ಮತ್ತೆ ಮತ್ತೆ') is kept."""
    if not KANNADA_RE.search(s):
        return s
    s = _ORPHAN_SIGNS.sub("", s)
    for _ in range(6):
        changed = False

        def repl(m):
            nonlocal changed
            frag, sep = m.group(1), m.group(2)
            if not KANNADA_RE.search(frag):
                return m.group(0)
            starts_sign = "\u0CBE" <= frag[0] <= "\u0CCD"
            if len(frag) < 3 and not starts_sign:
                return m.group(0)
            start = m.start()
            mid_word = start > 0 and not m.string[start - 1].isspace()
            if mid_word or _PUNCT.search(frag) or len(sep) >= 2 or frag[0] in "ಾಿೀುೂೃೆೇೈೊೋೌ್":
                changed = True
                return frag
            return m.group(0)
        s2 = _REPEAT.sub(repl, s)
        s = s2
        if not changed:
            break
    # single-akshara echo after a word: 'ಶ್ಲೋಕ ಕ', 'ಮನುಸ್ಮೃತಿಯ ಯ'
    s = _ECHO.sub(lambda m: m.group(1) + m.group(2), s)
    # doubled final syllable: 'ಬೆಳೆಯುತ್ತದೆದೆ' -> 'ಬೆಳೆಯುತ್ತದೆ'
    s = _TAIL2.sub(lambda m: m.group(1) + m.group(2), s)
    return re.sub(r"[ \t]{2,}", " ", s)


_AKSHARA = r"[\u0C95-\u0CB9\u0CDE](?:\u0CCD[\u0C95-\u0CB9])*[\u0CBE-\u0CCC\u0C82\u0C83]*"
_ECHO = re.compile(rf"(\S*[\u0C80-\u0CFF])({_AKSHARA})[ \t]+\2(?=[\s.,;:!?)\]]|$)")
_TAIL2 = re.compile(rf"(\S*[\u0C80-\u0CFF])([\u0C95-\u0CB9][\u0CBE-\u0CCC])\2(?=[\s.,;:!?)\]]|$)")
