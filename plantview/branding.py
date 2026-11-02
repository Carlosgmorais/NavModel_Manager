"""Approved NavModel identity shared by source and frozen Windows builds."""
import sys
from pathlib import Path
from PySide6 import QtCore, QtGui, QtWidgets as W

VERSION = '0.0.7'
ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))

def resource(name):
    return str(ROOT / 'resources' / name)

def app_icon():
    return QtGui.QIcon(resource('navmodel.ico'))

class StartupSplash(W.QWidget):
    """Real progress controls over approved artwork; never blocks the event loop."""
    minimum_ms = 5000

    def __init__(self):
        super().__init__(None, QtCore.Qt.SplashScreen | QtCore.Qt.FramelessWindowHint)
        self.setFixedSize(960, 414)
        self.setWindowTitle('NavModel Manager — Inicializando')
        self.setWindowIcon(app_icon())
        self.setStyleSheet('background: #06244c; color: white;')
        self.clock = QtCore.QElapsedTimer()
        self.art = QtGui.QPixmap(resource('splash-art.png'))
        self.logo = QtGui.QPixmap(resource('logo-white.png'))
        self.caption = W.QLabel('Inicializando o NavModel Manager...', self)
        self.caption.setGeometry(58, 300, 370, 25)
        self.caption.setStyleSheet('font: 13px "Segoe UI"; background: transparent;')
        self.progress = W.QProgressBar(self)
        self.progress.setGeometry(58, 332, 300, 11)
        self.progress.setTextVisible(False)
        self.progress.setStyleSheet('QProgressBar {background:#607d9d;border:0;border-radius:5px;} QProgressBar::chunk {background:#008cff;border-radius:5px;}')
        self.percent = W.QLabel('0%', self)
        self.percent.setGeometry(370, 325, 50, 25)
        self.percent.setStyleSheet('background:transparent; font:13px "Segoe UI";')
        self.version = W.QLabel(f'Versão {VERSION}', self)
        self.version.setGeometry(58, 375, 160, 20)
        self.version.setStyleSheet('color:#c5d5e8;background:transparent;')
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)

    def showEvent(self, event):
        super().showEvent(event)
        if not self.clock.isValid():
            self.clock.start()
            self.timer.start(30)
        self.move(self.screen().availableGeometry().center() - self.rect().center())

    def tick(self):
        value = min(99, int(self.clock.elapsed() * 100 / self.minimum_ms))
        self.progress.setValue(value)
        self.percent.setText(f'{value}%')

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor('#06244c'))
        if not self.art.isNull():
            p.drawPixmap(390, 12, 570, 395, self.art)
        if not self.logo.isNull():
            p.drawPixmap(58, 92, 94, 108, self.logo)
        p.setPen(QtGui.QPen(QtGui.QColor('#20adf3'), 2))
        p.drawLine(168, 100, 168, 191)
        p.setPen(QtCore.Qt.white)
        p.setFont(QtGui.QFont('Segoe UI', 30, QtGui.QFont.Bold))
        p.drawText(183, 145, 'NavModel')
        p.setFont(QtGui.QFont('Segoe UI', 18, QtGui.QFont.DemiBold))
        p.drawText(186, 179, 'M A N A G E R')
