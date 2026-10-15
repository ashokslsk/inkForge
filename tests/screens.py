import sys, os
os.environ.setdefault("INKFORGE_PROFILE", "InkForge-tests")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from inkforge import ui
OUT = os.environ["SHOTS"]; DEST = os.environ["DEST"]
app = QApplication(sys.argv); ui.light_palette(app); app.setStyleSheet(ui.QSS)
w = ui.Main(); w.resize(920, 1080)
w.o.output_dir = DEST; w.o.ask_save = False; w.o.device = "x3"; w.dev.set("x3")
w.cb_cat.set(True); w.set_organize(True); w.show()
sb = lambda: w.centralWidget().verticalScrollBar()
state = {"mid": False}
def s1():
    w.grab().save(f"{OUT}/1-welcome.png")
    w.add_paths(sys.argv[1:])
    QTimer.singleShot(400, s2)
def s2():
    sb().setValue(820); w.grab().save(f"{OUT}/2-options.png")
    sb().setValue(sb().maximum()); w.grab().save(f"{OUT}/3-options-kannada-pdf-output.png")
    w.start(); QTimer.singleShot(2500, s3)
def s3():
    sb().setValue(0); w.grab().save(f"{OUT}/4-processing.png"); QTimer.singleShot(500, poll)
def poll():
    if w.worker is None and w.result.isVisible():
        sb().setValue(0); w.grab().save(f"{OUT}/5-results.png")
        sb().setValue(sb().maximum()); w.grab().save(f"{OUT}/6-results-summary.png"); app.quit()
    else: QTimer.singleShot(500, poll)
QTimer.singleShot(800, s1)
app.exec()
