"""Render EPUB chapters roughly as an e-reader would (528x792) to PNGs: preview_pages.py book.epub outdir needle..."""
import os, re, sys, zipfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtGui import QGuiApplication, QTextDocument, QImage, QPainter, QColor, QFont
from PySide6.QtCore import QRectF
app = QGuiApplication([])
z = zipfile.ZipFile(sys.argv[1]); out = sys.argv[2]; os.makedirs(out, exist_ok=True)
css = z.read([n for n in z.namelist() if n.endswith("inkforge.css")][0]).decode()
xs = sorted(n for n in z.namelist() if "/Text/ch" in n)
for k, needle in enumerate(sys.argv[3:]):
    for x in xs:
        h = z.read(x).decode()
        i = h.find(needle)
        if i < 0:
            continue
        body = re.search(r"<body>(.*)</body>", h, re.S).group(1)
        j = body.find(needle)
        start = body.rfind("<p", 0, j) if body.rfind("<p", 0, j) > body.rfind("<h", 0, j) else body.rfind("<h", 0, j)
        frag = re.sub(r'<img[^>]*>', '', body[max(0, start):])
        doc = QTextDocument(); f = QFont(); f.setPixelSize(22); doc.setDefaultFont(f)
        doc.setDefaultStyleSheet(css + 'body{font-family:"Noto Sans Kannada","Kannada Sangam MN",Georgia;} p{margin:0 0 4px 0;line-height:140%} h2{font-size:26px} h3{font-size:23px} p.fn{font-size:16px;color:#333}')
        doc.setHtml(frag); doc.setTextWidth(528 - 36)
        img = QImage(528, 792, QImage.Format_RGB32); img.fill(QColor("white"))
        p = QPainter(img); p.translate(18, 20); doc.drawContents(p, QRectF(0, 0, 528 - 36, 792 - 40)); p.end()
        img.save(os.path.join(out, f"preview_{k + 1}.png")); print("saved", x, needle[:30]); break
