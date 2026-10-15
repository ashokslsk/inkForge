import sys, os
os.environ.setdefault("INKFORGE_PROFILE", "InkForge-tests")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from inkforge import ui
app = QApplication(sys.argv); ui.light_palette(app); app.setStyleSheet(ui.QSS)
w = ui.Main(); w.resize(900, 1100)
w.o.output_dir = os.environ["OUT"]; w.o.ask_save = False; w.o.device = "x3"; w.dev.set("x3"); w.cb_cat.set(os.environ.get("CAT","1")=="1"); w.set_organize(w.cb_cat.value())
w.add_paths(sys.argv[1:]); w.show()
QTimer.singleShot(500, w.start)
def poll():
    if w.worker is None and w.result.isVisible():
        w.centralWidget().verticalScrollBar().setValue(0)
        w.grab().save(os.environ["SHOT"]); app.quit()
    else:
        QTimer.singleShot(500, poll)
QTimer.singleShot(1500, poll)
app.exec()
