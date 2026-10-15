"""User-facing options and presets."""
from dataclasses import dataclass, asdict, field

QUALITY_PRESETS = {"low": 45, "med": 60, "high": 70, "max": 88}

TOGGLES = [
    # key, title, subtitle
    ("grayscale", "Grayscale", "Convert images for e-ink display"),
    ("contrast", "Contrast Boost", "Improve readability on e-ink"),
    ("remove_fonts", "Remove Fonts", "Strip embedded fonts"),
    ("clean_css", "Clean CSS", "Remove unused stylesheets"),
    ("gen_cover", "Generate Cover", "Create one if missing"),
    ("clean_meta", "Clean Metadata", "Strip store/calibre tags"),
    ("text_cleanup", "Text Cleanup", "Fix spacing, OCR errors, encoding"),
]


@dataclass
class Options:
    device: str = "x4"
    mode: str = "full"                # quick | full | custom
    grayscale: bool = True
    contrast: bool = True
    remove_fonts: bool = True
    clean_css: bool = True
    gen_cover: bool = True
    clean_meta: bool = True
    text_cleanup: bool = True
    light_novel: bool = False
    jpeg_quality: int = 70
    naming: str = "author_title"      # original | title_author | author_title | title | custom
    custom_pattern: str = "{author} - {title}"
    # Kannada books: keep shaped text (PocketDeck-OS / Kannada-capable firmware)
    # or render Kannada paragraphs as images (stock firmware with no Kannada shaper).
    kannada_mode: str = "text"        # text | image
    pdf_mode: str = "auto"            # auto | reflow | pages
    make_zip: bool = True
    keep_folders: bool = True
    organize: str = "category"        # category (auto-sorted) | folders (as dropped) | flat
    ask_save: bool = True             # ask for the destination folder when a batch starts
    output_dir: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        o = cls()
        for k, v in (d or {}).items():
            if hasattr(o, k):
                setattr(o, k, v)
        return o


def apply_preset(o: Options, mode: str) -> Options:
    o.mode = mode
    if mode == "quick":
        # Images + text only: leave styling, fonts, metadata alone.
        o.grayscale, o.contrast, o.text_cleanup = True, False, True
        o.remove_fonts = o.clean_css = o.gen_cover = o.clean_meta = False
    elif mode == "full":
        o.grayscale = o.contrast = o.remove_fonts = o.clean_css = True
        o.gen_cover = o.clean_meta = o.text_cleanup = True
    return o


class BookError(Exception):
    """A problem with one book, worded for the user (shown as-is in the UI)."""
