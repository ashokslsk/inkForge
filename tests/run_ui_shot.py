import sys, os
os.environ.setdefault("INKFORGE_PROFILE", "InkForge-tests")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from inkforge import ui
app = QApplication(sys.argv); ui.light_palette(app); app.setStyleSheet(ui.QSS)
w = ui.Main(); w.resize(900, 1900)
w.add_paths(sys.argv[1:])
w.show()
def shot():
    w.grab().save(os.environ["SHOT"]); app.quit()
QTimer.singleShot(1200, shot)
app.exec()
