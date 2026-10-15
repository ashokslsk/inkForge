"""InkForge desktop UI (PySide6)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback

from PySide6.QtCore import Qt, QThread, Signal, QSettings, QSize, QEvent, QObject, QTimer
from PySide6.QtGui import QColor, QPalette, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QApplication, QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
                               QPushButton, QFrame, QScrollArea, QFileDialog, QButtonGroup, QSlider, QLineEdit,
                               QProgressBar, QCheckBox, QSizePolicy, QMessageBox, QToolButton)

from . import __version__, APP_NAME
from .devices import DEVICES
from .options import Options, TOGGLES, QUALITY_PRESETS, apply_preset
from .runner import collect, process_job, make_output_root, zip_folder, SUPPORTED, Job

QSS = """
* { font-family: "SF Pro Text", "Helvetica Neue", Arial; color: #1c1c1e; }
QMainWindow, QWidget#root, QScrollArea, QWidget#scrollBody { background: #f2f2f4; }
QFrame#card { background: #ffffff; border: 1px solid #e3e3e8; border-radius: 18px; }
QLabel#h1 { font-size: 26px; font-weight: 700; }
QLabel#sub { font-size: 13px; color: #6e6e73; }
QLabel#h2 { font-size: 19px; font-weight: 600; }
QLabel#h3 { font-size: 15px; font-weight: 600; }
QLabel#muted { color: #8e8e93; font-size: 12px; }
QFrame#drop { background: #fafafa; border: 2px dashed #c7c7cc; border-radius: 16px; }
QFrame#drop[hover="true"] { background: #f0f0f2; border: 2px dashed #1c1c1e; }
QPushButton#seg { background: #ffffff; border: 1px solid #d8d8dd; border-radius: 10px; padding: 9px 6px; font-size: 14px; }
QPushButton#seg:hover { border-color: #8e8e93; }
QPushButton#seg:checked { background: #1c1c1e; color: #ffffff; border-color: #1c1c1e; }
QPushButton#big { background: #1c1c1e; color: #ffffff; border: none; border-radius: 16px; padding: 18px; font-size: 18px; font-weight: 600; }
QPushButton#big:hover { background: #333336; }
QPushButton#big:disabled { background: #b8b8bd; }
QPushButton#ghost { background: #ffffff; border: 1px solid #d1d1d6; border-radius: 10px; padding: 8px 14px; font-size: 13px; }
QPushButton#ghost:hover { border-color: #1c1c1e; }
QPushButton#link { background: transparent; border: none; color: #0a6cff; font-size: 13px; padding: 2px 4px; }
QToolButton#x { background: transparent; border: none; color: #8e8e93; font-size: 14px; }
QToolButton#x:hover { color: #ff3b30; }
QLineEdit { background: #ffffff; border: 1px solid #d1d1d6; border-radius: 9px; padding: 8px 10px; font-size: 13px; }
QProgressBar { background: #e5e5ea; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }
QProgressBar::chunk { background: #1c1c1e; border-radius: 3px; }
QProgressBar#overall { max-height: 10px; min-height: 10px; border-radius: 5px; }
QProgressBar#overall::chunk { border-radius: 5px; }
QSlider::groove:horizontal { height: 4px; background: #d1d1d6; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #1c1c1e; border-radius: 2px; }
QSlider::handle:horizontal { background: #1c1c1e; width: 22px; height: 22px; margin: -9px 0; border-radius: 11px; }
QScrollBar:vertical { width: 10px; background: transparent; }
QScrollBar::handle:vertical { background: #c7c7cc; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QCheckBox { font-size: 13px; spacing: 8px; }
"""


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def card() -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(26, 24, 26, 24)
    lay.setSpacing(16)
    return f, lay


def label(text, name=None, wrap=False):
    l = QLabel(text)
    if name:
        l.setObjectName(name)
    l.setWordWrap(wrap)
    return l


# ====================================================================== widgets
class Segmented(QWidget):
    """Row of exclusive buttons. items: [(key, html_or_text)]"""
    changed = Signal(str)

    def __init__(self, items, height=42, rich=False):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = {}
        for key, text in items:
            b = QPushButton()
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(height)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            if rich:
                inner = QLabel(text)
                inner.setAlignment(Qt.AlignCenter)
                inner.setAttribute(Qt.WA_TransparentForMouseEvents)
                inner.setStyleSheet("background: transparent; color: inherit;")
                bl = QVBoxLayout(b)
                bl.setContentsMargins(4, 4, 4, 4)
                bl.addWidget(inner)
                b._inner = inner
                b._text = text
            else:
                b.setText(text)
            self.group.addButton(b)
            self.buttons[key] = b
            b.clicked.connect(lambda _=False, k=key: self._clicked(k))
            lay.addWidget(b)
        self.group.buttonToggled.connect(lambda *_: self._restyle())

    def _restyle(self):
        for b in self.buttons.values():
            if hasattr(b, "_inner"):
                col = "#ffffff" if b.isChecked() else "#1c1c1e"
                sub = "#d1d1d6" if b.isChecked() else "#6e6e73"
                b._inner.setText(b._text.replace("{c}", col).replace("{s}", sub))

    def _clicked(self, key):
        self.changed.emit(key)

    def set(self, key):
        if key in self.buttons:
            self.buttons[key].setChecked(True)
        self._restyle()

    def value(self):
        for k, b in self.buttons.items():
            if b.isChecked():
                return k
        return None


class CheckBox(QWidget):
    """Drawn checkbox square (consistent look on every macOS theme)."""
    def __init__(self):
        super().__init__()
        self.checked = False
        self.setFixedSize(22, 22)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        if self.checked:
            p.setBrush(QColor("#1c1c1e"))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(r, 5, 5)
            pen = QPen(QColor("white"), 2.4)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.drawPolyline([r.topLeft() + type(r.topLeft())(5, 11), r.topLeft() + type(r.topLeft())(9, 15),
                            r.topLeft() + type(r.topLeft())(16, 6)])
        else:
            p.setBrush(QColor("white"))
            p.setPen(QPen(QColor("#8e8e93"), 1.5))
            p.drawRoundedRect(r, 5, 5)


class ToggleCard(QFrame):
    toggled = Signal(str, bool)

    def __init__(self, key, title, subtitle):
        super().__init__()
        self.key = key
        self.setCursor(Qt.PointingHandCursor)
        self.box = CheckBox()
        t = QLabel(title)
        t.setStyleSheet("font-size: 16px; font-weight: 500; background: transparent;")
        s = QLabel(subtitle)
        s.setStyleSheet("font-size: 13px; color: #8e8e93; background: transparent;")
        s.setWordWrap(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(14)
        lay.addWidget(self.box, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(t)
        col.addWidget(s)
        lay.addLayout(col, 1)
        self.set(False)

    def set(self, v: bool):
        self.box.checked = v
        self.box.update()
        bg = "#f5f5f7" if v else "#ffffff"
        self.setStyleSheet(f"ToggleCard {{ background: {bg}; border: 1px solid {'#c7c7cc' if v else '#e3e3e8'}; border-radius: 12px; }}")

    def value(self):
        return self.box.checked

    def mousePressEvent(self, e):
        self.set(not self.value())
        self.toggled.emit(self.key, self.value())


class DropZone(QFrame):
    dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("drop")
        self.setAcceptDrops(True)
        self.setMinimumHeight(190)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(8)
        icon = QLabel()
        icon.setPixmap(self._icon())
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet("background: transparent; border: none;")
        t = QLabel("Drop books or folders here")
        t.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent; border: none;")
        t.setAlignment(Qt.AlignCenter)
        s = QLabel("EPUB · PDF · MOBI · AZW3 · TXT  —  English & ಕನ್ನಡ  —  folders are scanned inside")
        s.setStyleSheet("font-size: 13px; color: #8e8e93; background: transparent; border: none;")
        s.setAlignment(Qt.AlignCenter)
        row = QHBoxLayout()
        row.setAlignment(Qt.AlignCenter)
        self.btn_files = QPushButton("Choose Files…")
        self.btn_folder = QPushButton("Choose Folder…")
        for b in (self.btn_files, self.btn_folder):
            b.setObjectName("ghost")
            b.setCursor(Qt.PointingHandCursor)
            row.addWidget(b)
        lay.addWidget(icon)
        lay.addWidget(t)
        lay.addWidget(s)
        lay.addSpacing(6)
        lay.addLayout(row)

    @staticmethod
    def _icon():
        pm = QPixmap(56, 56)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor("#1c1c1e"), 3)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawRoundedRect(12, 6, 32, 44, 4, 4)       # e-reader
        p.drawLine(18, 42, 38, 42)
        p.drawLine(28, 34, 28, 16)                   # arrow
        p.drawLine(21, 23, 28, 16)
        p.drawLine(35, 23, 28, 16)
        p.end()
        return pm

    def _hover(self, v):
        self.setProperty("hover", "true" if v else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._hover(True)

    def dragLeaveEvent(self, e):
        self._hover(False)

    def dropEvent(self, e):
        self._hover(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.dropped.emit(paths)


class JobRow(QFrame):
    remove = Signal(object)

    def __init__(self, job: Job):
        super().__init__()
        self.job = job
        self.setStyleSheet("JobRow { background: #ffffff; border-bottom: 1px solid #efeff2; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 9, 6, 9)
        lay.setSpacing(12)
        ext = job.ext[1:].upper() or "?"
        badge = QLabel(ext)
        badge.setFixedWidth(46)
        badge.setAlignment(Qt.AlignCenter)
        colors = {"EPUB": "#1c1c1e", "PDF": "#b3261e", "MOBI": "#8a5a00", "AZW3": "#8a5a00", "TXT": "#555"}
        badge.setStyleSheet(f"background: {colors.get(ext, '#555')}; color: white; border-radius: 6px; "
                            f"font-size: 11px; font-weight: 700; padding: 4px 0;")
        lay.addWidget(badge, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(3)
        self.name = QLabel(job.name)
        self.name.setStyleSheet("font-size: 14px; font-weight: 500;")
        self.name.setMinimumWidth(100)
        self.name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        size = os.path.getsize(job.path) if os.path.exists(job.path) else 0
        self.meta_text = human(size) + (f"  ·  📁 {job.rel_dir}" if job.rel_dir else "")
        self.meta = QLabel(self.meta_text)
        self.meta.setStyleSheet("font-size: 12px; color: #8e8e93;")
        self.meta.setWordWrap(True)
        self.meta.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        self.bar.hide()
        col.addWidget(self.name)
        col.addWidget(self.meta)
        col.addWidget(self.bar)
        lay.addLayout(col, 1)
        self.status = QLabel("Queued")
        self.status.setStyleSheet("font-size: 12px; color: #8e8e93;")
        self.status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.status, 0, Qt.AlignTop)
        self.x = QToolButton()
        self.x.setObjectName("x")
        self.x.setText("✕")
        self.x.setCursor(Qt.PointingHandCursor)
        self.x.clicked.connect(lambda: self.remove.emit(self))
        lay.addWidget(self.x, 0, Qt.AlignTop)
        if job.status == "skipped":
            self.show_result()

    def set_progress(self, frac, text):
        self.bar.show()
        self.bar.setValue(int(frac * 1000))
        self.status.setText(f"{int(frac * 100)}%")
        self.meta.setText(text)
        self.meta.setStyleSheet("font-size: 12px; color: #3a3a3c;")

    def show_result(self):
        j = self.job
        self.bar.hide()
        self.x.setVisible(j.status != "working")
        if j.status == "done":
            saved = (1 - j.after / j.before) * 100 if j.before else 0
            self.status.setText(f"✓ {human(j.before)} → {human(j.after)}")
            self.status.setStyleSheet("font-size: 12px; color: #1f7a35; font-weight: 600;")
            extra = f"  ·  ⚠ {j.warnings[0]}" if j.warnings else ""
            cat = f"🗂 {j.category}  ·  " if j.category else ""
            self.meta.setText(f"{cat}{j.message}  ·  {os.path.basename(j.out_path)}{extra}")
            self.meta.setStyleSheet("font-size: 12px; color: #6e6e73;")
            self.meta.setToolTip("\n".join(j.warnings))
            _ = saved
        elif j.status in ("failed", "skipped"):
            self.status.setText("✕ Failed" if j.status == "failed" else "Skipped")
            self.status.setStyleSheet("font-size: 12px; color: #c62828; font-weight: 600;")
            self.meta.setText(j.message)
            self.meta.setStyleSheet("font-size: 12px; color: #c62828;")


# ====================================================================== worker
class Worker(QThread):
    progress = Signal(int, float, str)
    finished_job = Signal(int)
    all_done = Signal(str, str)
    log = Signal(str)

    def __init__(self, jobs, opts: Options):
        super().__init__()
        self.jobs, self.opts = jobs, opts
        self.cancelled = False

    def run(self):
        try:
            root = make_output_root(self.opts)
        except Exception as e:
            for k, j in enumerate(self.jobs):
                j.status, j.message = "failed", f"Could not create the output folder: {e}"
                self.finished_job.emit(k)
            self.all_done.emit("", "")
            return
        logf = open(os.path.join(root, "inkforge-log.txt"), "a", encoding="utf-8")

        def log(m):
            logf.write(m + "\n")
            logf.flush()
        for k, j in enumerate(self.jobs):
            if self.cancelled:
                break
            if j.status != "queued":
                continue
            last = [0.0]

            def prog(f, text, k=k):
                now = time.time()
                if now - last[0] > 0.08 or f >= 1:
                    last[0] = now
                    self.progress.emit(k, float(f), text)
            log(f"== {j.path}")
            process_job(j, self.opts, root, prog, log)
            log(f"   {j.status}: {j.message} {j.warnings}")
            self.finished_job.emit(k)
        zpath = ""
        done = [j for j in self.jobs if j.status == "done"]
        if done and self.opts.make_zip and not self.cancelled:
            self.progress.emit(-1, 1.0, "Creating ZIP…")
            try:
                zpath = zip_folder(root)
            except Exception as e:
                log(f"zip failed: {e}")
        logf.close()
        self.all_done.emit(root, zpath)


# ====================================================================== main window
class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — Xteink Book Optimizer")
        self.resize(900, 1000)
        self.setMinimumWidth(640)
        self.setAcceptDrops(True)
        self.settings = QSettings("InkForge", os.environ.get("INKFORGE_PROFILE", "InkForge"))
        try:
            self.o = Options.from_dict(json.loads(self.settings.value("options", "{}")))
        except Exception:
            self.o = Options()
        self.rows: list[JobRow] = []
        self.worker = None

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        body.setObjectName("scrollBody")
        scroll.setWidget(body)
        self.setCentralWidget(scroll)
        outer = QHBoxLayout(body)
        outer.setContentsMargins(24, 24, 24, 32)
        col_w = QWidget()
        col_w.setMaximumWidth(860)
        outer.addWidget(col_w)
        col = QVBoxLayout(col_w)
        col.setSpacing(18)

        # ---------- header
        head = QHBoxLayout()
        tcol = QVBoxLayout()
        tcol.setSpacing(2)
        tcol.addWidget(label(APP_NAME, "h1"))
        tcol.addWidget(label("Optimize EPUB, PDF & MOBI for Xteink e-ink readers — English & ಕನ್ನಡ · works 100% offline", "sub", True))
        head.addLayout(tcol, 1)
        col.addLayout(head)

        # ---------- drop card
        c, lay = card()
        self.drop = DropZone()
        self.drop.dropped.connect(self.add_paths)
        self.drop.btn_files.clicked.connect(self.pick_files)
        self.drop.btn_folder.clicked.connect(self.pick_folder)
        lay.addWidget(self.drop)
        qh = QHBoxLayout()
        self.queue_title = label("No books yet", "h3")
        qh.addWidget(self.queue_title, 1)
        self.btn_clear = QPushButton("Clear list")
        self.btn_clear.setObjectName("link")
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.clicked.connect(self.clear)
        qh.addWidget(self.btn_clear)
        lay.addLayout(qh)
        self.list_box = QVBoxLayout()
        self.list_box.setSpacing(0)
        lay.addLayout(self.list_box)
        col.addWidget(c)

        # ---------- options card
        c, lay = card()
        lay.addWidget(label("Options", "h2"))
        drow = QHBoxLayout()
        dl = label("Device", "h3")
        dl.setFixedWidth(90)
        drow.addWidget(dl)
        self.dev = Segmented([(k, f'<span style="font-size:19px;font-weight:700;color:{{c}}">{d.name}</span><br>'
                                  f'<span style="font-size:13px;color:{{s}}">{d.label}</span>')
                              for k, d in DEVICES.items()], height=74, rich=True)
        self.dev.changed.connect(lambda k: self.opt("device", k))
        drow.addWidget(self.dev, 1)
        lay.addLayout(drow)

        self.mode = Segmented([("quick", '<span style="font-size:22px">⚡</span><br><span style="font-size:17px;font-weight:700;color:{c}">Quick</span>'
                                         '<br><span style="font-size:13px;color:{s}">Images + text</span>'),
                               ("full", '<span style="font-size:22px">✨</span><br><span style="font-size:17px;font-weight:700;color:{c}">Full</span>'
                                        '<br><span style="font-size:13px;color:{s}">Device-optimized</span>'),
                               ("custom", '<span style="font-size:22px">🔧</span><br><span style="font-size:17px;font-weight:700;color:{c}">Custom</span>'
                                          '<br><span style="font-size:13px;color:{s}">Pick &amp; choose</span>')], height=112, rich=True)
        self.mode.changed.connect(self.set_mode)
        lay.addWidget(self.mode)

        grid = QGridLayout()
        grid.setSpacing(14)
        self.toggles = {}
        for k, (key, title, sub) in enumerate(TOGGLES):
            tc = ToggleCard(key, title, sub)
            tc.toggled.connect(self.toggle_changed)
            self.toggles[key] = tc
            grid.addWidget(tc, k // 2, k % 2)
        ln = ToggleCard("light_novel", "Light Novel Mode", "Rotate & split landscape images for vertical reading")
        ln.toggled.connect(self.toggle_changed)
        self.toggles["light_novel"] = ln
        grid.addWidget(ln, (len(TOGGLES) + 1) // 2, 0, 1, 2)
        lay.addLayout(grid)

        lay.addSpacing(4)
        lay.addWidget(label("Kannada books", "h3"))
        self.kn = Segmented([("text", "ಕನ್ನಡ as text  ·  PocketDeck-OS"), ("image", "ಕನ್ನಡ as images  ·  stock firmware")])
        self.kn.changed.connect(lambda k: self.opt("kannada_mode", k))
        lay.addWidget(self.kn)
        self.kn_hint = label("", "muted", True)
        lay.addWidget(self.kn_hint)

        lay.addWidget(label("PDF conversion", "h3"))
        self.pdf = Segmented([("auto", "Auto"), ("reflow", "Text reflow"), ("imgflow", "Image reflow"), ("pages", "Page images")])
        self.pdf.changed.connect(lambda k: self.opt("pdf_mode", k))
        lay.addWidget(self.pdf)
        self.pdf_hint = label("", "muted", True)
        lay.addWidget(self.pdf_hint)

        lay.addWidget(label("JPEG Quality", "h3"))
        self.q = Segmented([(k, k.capitalize()) for k in QUALITY_PRESETS])
        self.q.changed.connect(lambda k: self.set_quality(QUALITY_PRESETS[k]))
        lay.addWidget(self.q)
        srow = QHBoxLayout()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(30, 95)
        self.slider.valueChanged.connect(self.set_quality)
        self.qlabel = QLabel("70%")
        self.qlabel.setStyleSheet("font-size: 17px; color: #6e6e73; font-weight: 500;")
        self.qlabel.setFixedWidth(52)
        self.qlabel.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        srow.addWidget(self.slider, 1)
        srow.addWidget(self.qlabel)
        lay.addLayout(srow)

        lay.addWidget(label("Output filename", "h3"))
        self.naming = Segmented([("original", "Original"), ("title_author", "Title - Author"),
                                 ("author_title", "Author - Title"), ("title", "Title"), ("custom", "Custom")])
        self.naming.changed.connect(self.set_naming)
        lay.addWidget(self.naming)
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText("{author} - {title}   ·   tokens: {title} {author} {device} {original} {lang}")
        self.pattern.textChanged.connect(lambda t: self.opt("custom_pattern", t))
        lay.addWidget(self.pattern)

        lay.addWidget(label("Save to", "h3"))
        orow = QHBoxLayout()
        self.out_label = QLabel()
        self.out_label.setStyleSheet("font-size: 13px; color: #3a3a3c;")
        self.out_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        orow.addWidget(self.out_label, 1)
        b = QPushButton("Change…")
        b.setObjectName("ghost")
        b.clicked.connect(self.pick_out)
        orow.addWidget(b)
        lay.addLayout(orow)
        self.cb_cat = ToggleCard("organize", "Sort into category folders",
                                 "AI & Machine Learning, Communication, Comedy, ಕನ್ನಡ… detected automatically. "
                                 "Off: everything goes straight into today's dated folder.")
        self.cb_cat.toggled.connect(lambda _k, v: self.set_organize(v))
        lay.addWidget(self.cb_cat)
        crow = QHBoxLayout()
        self.cb_zip = QCheckBox("Create a ZIP  (YYYY-MM-DD / Category / book.epub)")
        self.cb_zip.toggled.connect(lambda v: self.opt("make_zip", v))
        self.cb_keep = QCheckBox("Keep my folder structure")
        self.cb_keep.setToolTip("When categories are off, books from a dropped folder keep their sub-folders")
        self.cb_keep.toggled.connect(lambda v: self.set_organize(self.cb_cat.value()))
        self.cb_ask = QCheckBox("Ask where to save")
        self.cb_ask.toggled.connect(lambda v: self.opt("ask_save", v))
        crow.addWidget(self.cb_zip)
        crow.addWidget(self.cb_keep)
        crow.addWidget(self.cb_ask)
        crow.addStretch(1)
        lay.addLayout(crow)

        lay.addSpacing(6)
        self.go = QPushButton("Optimize EPUBs")
        self.go.setObjectName("big")
        self.go.setCursor(Qt.PointingHandCursor)
        self.go.clicked.connect(self.start)
        lay.addWidget(self.go)

        # progress / results
        self.prog_box = QWidget()
        pl = QVBoxLayout(self.prog_box)
        pl.setContentsMargins(0, 4, 0, 0)
        pl.setSpacing(8)
        self.overall = QProgressBar()
        self.overall.setObjectName("overall")
        self.overall.setTextVisible(False)
        self.overall.setRange(0, 1000)
        self.prog_text = label("", "sub", True)
        pr = QHBoxLayout()
        pr.addWidget(self.prog_text, 1)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("ghost")
        self.btn_cancel.clicked.connect(self.cancel)
        pr.addWidget(self.btn_cancel)
        pl.addWidget(self.overall)
        pl.addLayout(pr)
        self.prog_box.hide()
        lay.addWidget(self.prog_box)

        self.result = QFrame()
        self.result.setStyleSheet("QFrame { background: #f1f8f3; border: 1px solid #cfe8d6; border-radius: 14px; }")
        rl = QVBoxLayout(self.result)
        rl.setContentsMargins(18, 16, 18, 16)
        self.result_text = QLabel()
        self.result_text.setWordWrap(True)
        self.result_text.setStyleSheet("font-size: 14px; background: transparent; border: none;")
        rl.addWidget(self.result_text)
        rb = QHBoxLayout()
        self.btn_show = QPushButton("Show in Finder")
        self.btn_zip = QPushButton("Reveal ZIP")
        for bb in (self.btn_show, self.btn_zip):
            bb.setObjectName("ghost")
            bb.setCursor(Qt.PointingHandCursor)
            rb.addWidget(bb)
        rb.addStretch(1)
        rl.addLayout(rb)
        self.result.hide()
        lay.addWidget(self.result)
        col.addWidget(c)

        foot = label(f"{APP_NAME} {__version__} · nothing is uploaded — all processing happens on this Mac", "muted")
        foot.setAlignment(Qt.AlignCenter)
        col.addWidget(foot)
        col.addStretch(1)
        self.load_options()
        self.refresh()

    # ------------------------------------------------------------------ options
    def load_options(self):
        o = self.o
        self.dev.set(o.device)
        self.mode.set(o.mode)
        for k, tc in self.toggles.items():
            tc.set(bool(getattr(o, k)))
        self.kn.set(o.kannada_mode)
        self.pdf.set(o.pdf_mode)
        self.slider.setValue(int(o.jpeg_quality))
        self.set_quality(int(o.jpeg_quality))
        self.naming.set(o.naming)
        self.pattern.setText(o.custom_pattern)
        self.pattern.setVisible(o.naming == "custom")
        self.cb_zip.setChecked(o.make_zip)
        self.cb_cat.set(o.organize == "category")
        self.cb_keep.setChecked(o.organize == "folders")
        self.cb_keep.setEnabled(o.organize != "category")
        self.cb_ask.setChecked(o.ask_save)
        self.update_out_label()
        self.update_hints()

    def save(self):
        self.settings.setValue("options", json.dumps(self.o.to_dict()))

    def opt(self, key, value):
        setattr(self.o, key, value)
        self.save()
        self.update_hints()

    def update_hints(self):
        self.kn_hint.setText(
            "Keeps real Kannada text; EPUBs are tagged language “kn” so PocketDeck-OS shapes them with its Kannada font. "
            "Text-based Kannada PDFs become real-text EPUBs."
            if self.o.kannada_mode == "text" else
            "Kannada paragraphs are drawn into crisp images, so they display correctly even on firmware without a Kannada shaper "
            "(text can't be resized or searched).")
        self.pdf_hint.setText({
            "auto": "Text PDFs (English or Unicode Kannada) become real reflowable text. Scans and broken/legacy-font "
                    "Kannada PDFs are read with the built-in Kannada + English OCR; if a scan is unreadable, Image reflow is used.",
            "reflow": "Always extract the PDF's text layer and re-flow it.",
            "imgflow": "Cut the printed words out and re-wrap them larger — faithful for any script, no OCR needed.",
            "pages": "One screen-sized image per page — for comics, magazines and maths-heavy layouts.",
        }[self.o.pdf_mode])

    def set_mode(self, mode):
        apply_preset(self.o, mode)
        for k, tc in self.toggles.items():
            if k != "light_novel":
                tc.set(bool(getattr(self.o, k)))
        self.save()

    def toggle_changed(self, key, value):
        setattr(self.o, key, value)
        if key != "light_novel" and self.o.mode != "custom":
            self.o.mode = "custom"
            self.mode.set("custom")
        self.save()

    def set_quality(self, v):
        v = int(v)
        self.o.jpeg_quality = v
        if self.slider.value() != v:
            self.slider.blockSignals(True)
            self.slider.setValue(v)
            self.slider.blockSignals(False)
        self.qlabel.setText(f"{v}%")
        match = [k for k, q in QUALITY_PRESETS.items() if q == v]
        if match:
            self.q.set(match[0])
        else:
            self.q.group.setExclusive(False)
            for b in self.q.buttons.values():
                b.setChecked(False)
            self.q.group.setExclusive(True)
        self.save()

    def set_organize(self, categories: bool):
        self.cb_keep.setEnabled(not categories)
        self.opt("organize", "category" if categories else ("folders" if self.cb_keep.isChecked() else "flat"))

    def set_naming(self, k):
        self.pattern.setVisible(k == "custom")
        self.opt("naming", k)

    def update_out_label(self):
        base = self.o.output_dir or os.path.join(os.path.expanduser("~/Downloads"), "Xteink Optimized")
        self.out_label.setText(base.replace(os.path.expanduser("~"), "~"))

    def pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "Save optimized books to", self.o.output_dir or os.path.expanduser("~/Downloads"))
        if d:
            self.opt("output_dir", d)
            self.update_out_label()

    # ------------------------------------------------------------------ queue
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_paths([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def pick_files(self):
        exts = " ".join("*" + x for x in sorted(SUPPORTED))
        files, _ = QFileDialog.getOpenFileNames(self, "Choose books", os.path.expanduser("~"), f"Books ({exts})")
        if files:
            self.add_paths(files)

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose a folder of books", os.path.expanduser("~"))
        if d:
            self.add_paths([d])

    def add_paths(self, paths):
        if self.worker and self.worker.isRunning():
            QMessageBox.information(self, APP_NAME, "Please wait until the current batch finishes.")
            return
        if self.result.isVisible():
            self.clear()
        have = {r.job.path for r in self.rows}
        jobs = [j for j in collect(paths) if j.path not in have]
        if not jobs:
            QMessageBox.information(self, APP_NAME, "No supported books were found there.\n\n"
                                    "Supported: EPUB, PDF, MOBI, AZW/AZW3, TXT (and FB2/DOCX/RTF if Calibre is installed).")
            return
        for j in jobs:
            row = JobRow(j)
            row.remove.connect(self.remove_row)
            self.rows.append(row)
            self.list_box.addWidget(row)
        self.refresh()

    def remove_row(self, row):
        if self.worker and self.worker.isRunning():
            return
        self.rows.remove(row)
        row.setParent(None)
        row.deleteLater()
        self.refresh()

    def clear(self):
        if self.worker and self.worker.isRunning():
            return
        for r in self.rows:
            r.setParent(None)
            r.deleteLater()
        self.rows = []
        self.result.hide()
        self.prog_box.hide()
        self.refresh()

    def refresh(self):
        n = len([r for r in self.rows if r.job.status == "queued"])
        total = len(self.rows)
        kinds = {r.job.ext for r in self.rows if r.job.status == "queued"}
        noun = "EPUBs" if kinds <= {".epub"} else "books"
        self.queue_title.setText(f"{total} book{'s' if total != 1 else ''} in the list" if total else "No books yet")
        self.btn_clear.setVisible(total > 0)
        busy = bool(self.worker and self.worker.isRunning())
        self.go.setEnabled(n > 0 and not busy)
        self.go.setText(f"⇪  Optimize {n} {noun if n != 1 else noun[:-1]}" if n else "⇪  Optimize EPUBs")

    # ------------------------------------------------------------------ run
    def start(self):
        jobs = [r.job for r in self.rows]
        if not any(j.status == "queued" for j in jobs):
            return
        if self.o.ask_save:
            start_dir = self.o.output_dir or os.path.join(os.path.expanduser("~/Downloads"), "Xteink Optimized")
            os.makedirs(start_dir, exist_ok=True) if not self.o.output_dir else None
            d = QFileDialog.getExistingDirectory(self, "Where should the optimized books be saved?", start_dir)
            if not d:
                return
            self.o.output_dir = d
            self.update_out_label()
        self.save()
        self.result.hide()
        self.prog_box.show()
        self.overall.setValue(0)
        self.prog_text.setText("Starting…")
        self.btn_cancel.setEnabled(True)
        for r in self.rows:
            r.x.setVisible(False)
        o = Options.from_dict(self.o.to_dict())
        self.queued_idx = [k for k, j in enumerate(jobs) if j.status == "queued"]
        self.worker = Worker(jobs, o)
        self.worker.progress.connect(self.on_progress)
        self.worker.finished_job.connect(self.on_job)
        self.worker.all_done.connect(self.on_done)
        self.worker.start()
        self.refresh()

    def cancel(self):
        if self.worker:
            self.worker.cancelled = True
            self.btn_cancel.setEnabled(False)
            self.prog_text.setText("Cancelling after the current book…")

    def on_progress(self, k, frac, text):
        if k < 0:
            self.prog_text.setText(text)
            return
        self.rows[k].set_progress(frac, text)
        pos = self.queued_idx.index(k) if k in self.queued_idx else 0
        total = max(1, len(self.queued_idx))
        self.overall.setValue(int((pos + frac) / total * 1000))
        self.prog_text.setText(f"Book {pos + 1} of {total} · {self.rows[k].job.name}")

    def on_job(self, k):
        self.rows[k].show_result()

    def on_done(self, root, zpath):
        jobs = [r.job for r in self.rows]
        done = [j for j in jobs if j.status == "done"]
        failed = [j for j in jobs if j.status == "failed"]
        self.prog_box.hide()
        for r in self.rows:
            r.x.setVisible(r.job.status != "done")
        before = sum(j.before for j in done)
        after = sum(j.after for j in done)
        msg = f"<b>✓ {len(done)} book{'s' if len(done) != 1 else ''} optimized for Xteink {DEVICES[self.o.device].name}</b>"
        if failed:
            msg += f"  ·  <span style='color:#c62828'>{len(failed)} could not be converted (see the list)</span>"
        if done:
            msg += f"<br>{human(before)} → {human(after)}"
            if before:
                msg += f"  ({(1 - after / before) * 100:.0f}% smaller)"
        cats = sorted({j.category for j in done if j.category})
        if cats and self.o.organize == "category":
            msg += "<br>Categories: " + ", ".join(f"{c} ({sum(1 for j in done if j.category == c)})" for c in cats)
        if root:
            msg += f"<br><span style='color:#6e6e73'>{root.replace(os.path.expanduser('~'), '~')}</span>"
        self.result_text.setText(msg)
        self.result.setStyleSheet("QFrame { background: %s; border: 1px solid %s; border-radius: 14px; }" %
                                  (("#f1f8f3", "#cfe8d6") if done else ("#fdf1f0", "#f2cdc9")))
        self._last_root, self._last_zip = root, zpath
        if not getattr(self, "_result_wired", False):
            self.btn_show.clicked.connect(lambda: subprocess.run(["open", self._last_root]) if self._last_root else None)
            self.btn_zip.clicked.connect(lambda: subprocess.run(["open", "-R", self._last_zip]) if self._last_zip else None)
            self._result_wired = True
        self.btn_show.setVisible(bool(root and done))
        self.btn_zip.setVisible(bool(zpath))
        self.result.show()
        self.worker = None
        self.refresh()
        QApplication.alert(self)


class FileOpenFilter(QObject):
    """Files dropped on the Dock icon / 'Open With'."""
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.pending = []

    def eventFilter(self, obj, e):
        if e.type() == QEvent.FileOpen:
            self.win.add_paths([e.file()])
            return True
        return False


def light_palette(app):
    app.setStyle("Fusion")
    p = QPalette()
    for role, c in ((QPalette.Window, "#f2f2f4"), (QPalette.Base, "#ffffff"), (QPalette.Text, "#1c1c1e"),
                    (QPalette.WindowText, "#1c1c1e"), (QPalette.Button, "#ffffff"), (QPalette.ButtonText, "#1c1c1e"),
                    (QPalette.Highlight, "#1c1c1e"), (QPalette.HighlightedText, "#ffffff"),
                    (QPalette.ToolTipBase, "#ffffff"), (QPalette.ToolTipText, "#1c1c1e"),
                    (QPalette.PlaceholderText, "#8e8e93")):
        p.setColor(role, QColor(c))
    app.setPalette(p)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    light_palette(app)
    app.setStyleSheet(QSS)
    icon_path = os.path.join(os.path.dirname(__file__), "assets", "icon.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    w = Main()
    filt = FileOpenFilter(w)
    app.installEventFilter(filt)
    for a in sys.argv[1:]:
        if os.path.exists(a):
            w.add_paths([a])
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
