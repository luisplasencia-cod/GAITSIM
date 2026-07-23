import sys
from PySide6.QtWidgets import QApplication, QLabel

app = QApplication(sys.argv)

label = QLabel("GAITSIM CONTROL TEST")
label.resize(400,200)
label.show()

sys.exit(app.exec())

