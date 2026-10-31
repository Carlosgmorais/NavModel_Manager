"""Autodesk Navisworks-style Loading Progress Dialog.

Reproduces the exact UI layout from Navisworks Manage:
- Dialog Title: "Loading ..."
- Window Icon: Classic Navisworks 'N' logo
- Percentage readout: "0,00%"
- Primary progress bar (Windows native / classic Navisworks green)
- Filename and current phase: "Loading ESTUDO - NOME.dwg ..."
- Secondary sub-progress bar
- Bottom right "Cancel" button
"""
from pathlib import Path
from PySide6 import QtCore, QtGui, QtWidgets as W
from .icons import create_navis_icon


class NavisLoadingDialog(W.QDialog):
    cancelled = QtCore.Signal()

    def __init__(self, parent=None, title='Loading ...'):
        super().__init__(
            parent,
            QtCore.Qt.Dialog |
            QtCore.Qt.WindowTitleHint |
            QtCore.Qt.WindowCloseButtonHint |
            QtCore.Qt.CustomizeWindowHint
        )
        self.setWindowTitle(title)
        self.setWindowIcon(create_navis_icon('app_logo', 32))
        self.setFixedSize(510, 165)
        self.setModal(False)

        # Exact Navisworks / Windows Classic styling
        self.setStyleSheet("""
            QDialog {
                background-color: #f0f0f0;
                color: #000000;
                font-family: 'Segoe UI', Tahoma, Arial, sans-serif;
            }
            QLabel {
                color: #000000;
            }
            QLabel#pctLabel {
                font-size: 12px;
                font-weight: 500;
                color: #000000;
                padding-bottom: 2px;
            }
            QLabel#fileLabel {
                font-size: 12px;
                color: #000000;
                padding-top: 4px;
                padding-bottom: 2px;
            }
            QProgressBar {
                border: 1px solid #8e8f8f;
                border-radius: 0px;
                background-color: #e6e6e6;
                height: 16px;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #06b025;
            }
            QPushButton {
                background-color: #e1e1e1;
                border: 1px solid #adadad;
                border-radius: 0px;
                padding: 3px 18px;
                font-size: 12px;
                color: #000000;
                min-width: 75px;
                min-height: 23px;
            }
            QPushButton:hover {
                background-color: #e5f1fb;
                border: 1px solid #0078d4;
            }
            QPushButton:pressed {
                background-color: #cce4f7;
                border: 1px solid #005499;
            }
            QPushButton:focus {
                border: 1px solid #0078d4;
            }
        """)

        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)

        # Percentage label at top centered
        self.pct_label = W.QLabel('0,00%')
        self.pct_label.setObjectName('pctLabel')
        self.pct_label.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(self.pct_label)

        # Primary Progress Bar
        self.bar_main = W.QProgressBar()
        self.bar_main.setRange(0, 10000)
        self.bar_main.setValue(0)
        self.bar_main.setTextVisible(False)
        layout.addWidget(self.bar_main)

        # File & action label
        self.file_label = W.QLabel('Loading ...')
        self.file_label.setObjectName('fileLabel')
        self.file_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        layout.addWidget(self.file_label)

        # Sub Progress Bar
        self.bar_sub = W.QProgressBar()
        self.bar_sub.setRange(0, 10000)
        self.bar_sub.setValue(0)
        self.bar_sub.setTextVisible(False)
        layout.addWidget(self.bar_sub)

        layout.addSpacing(6)

        # Bottom row with Cancel button on right
        btn_layout = W.QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.addStretch()
        self.cancel_btn = W.QPushButton('Cancel')
        self.cancel_btn.clicked.connect(self.on_cancel)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

        self._is_cancelled = False
        self._current_file = ''

    def set_file_name(self, filename: str):
        self._current_file = Path(filename).name if filename else ''
        self.file_label.setText(f'Loading {self._current_file} ...' if self._current_file else 'Loading ...')
        QtCore.QCoreApplication.processEvents()

    def set_progress(self, pct: float, message: str = ''):
        """Update percentage (0.0 to 100.0) and optional detail."""
        pct_clamped = max(0.0, min(100.0, float(pct)))
        self.bar_main.setValue(int(pct_clamped * 100))
        self.pct_label.setText(f'{pct_clamped:.2f}%'.replace('.', ','))

        if message:
            # If message starts with Loading ESTUDO..., keep clean Navisworks style
            if message.startswith('Loading '):
                clean_msg = message.split(' — ')[0]
                self.file_label.setText(clean_msg if clean_msg.endswith('...') else clean_msg + ' ...')
            elif self._current_file:
                self.file_label.setText(f'Loading {self._current_file} ...')
            else:
                clean_msg = message.split(' — ')[0]
                self.file_label.setText(clean_msg if clean_msg.endswith('...') else clean_msg + ' ...')

        # Sub progress bar simulation for responsive Navisworks feel
        if pct_clamped >= 100.0:
            self.bar_sub.setValue(10000)
        else:
            sub_pct = ((pct_clamped * 3.7) % 100.0)
            self.bar_sub.setValue(int(sub_pct * 100))

        QtCore.QCoreApplication.processEvents()

    def on_cancel(self):
        self._is_cancelled = True
        self.cancelled.emit()
        self.reject()

    def is_cancelled(self) -> bool:
        return self._is_cancelled
