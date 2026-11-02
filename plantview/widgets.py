"""Navisworks HUD Overlay, Coordinate Badges, Layer Manager Dialog, NavCube & View Controls."""
from typing import Dict, Tuple
import vtk
from PySide6 import QtCore, QtGui, QtWidgets as W
from .colors import ACI_TABLE


class NavisCoordinateOverlay(W.QFrame):
    """Bottom-left floating coordinate badge identical to Autodesk Navisworks."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("""
            NavisCoordinateOverlay {
                background: transparent;
                border: none;
            }
            QLabel {
                background-color: rgba(225, 235, 245, 0.85);
                border: 1px solid #9cb4cc;
                border-radius: 4px;
                color: #2b4560;
                font-family: 'Segoe UI', Tahoma, monospace;
                font-size: 11px;
                font-weight: 600;
                padding: 3px 8px;
            }
        """)

        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(12, 0, 0, 12)
        layout.setSpacing(4)
        layout.addStretch()

        self.lbl_elev = W.QLabel('EL.: 0,000 m')
        self.lbl_elev.setFixedWidth(200)
        layout.addWidget(self.lbl_elev)

        self.lbl_coords = W.QLabel('X: 0,000 m   Y: 0,000 m   Z: 0,000 m')
        self.lbl_coords.setFixedWidth(290)
        layout.addWidget(self.lbl_coords)

    def update_coordinates(self, eye, focal):
        """Update coordinate readouts from camera eye or focal point."""
        x, y, z = eye
        self.lbl_coords.setText(f'X: {x:,.3f} m   Y: {y:,.3f} m   Z: {z:,.3f} m'.replace(',', 'X').replace('.', ',').replace('X', '.'))
        self.lbl_elev.setText(f'EL.: {z:,.3f} m'.replace(',', 'X').replace('.', ',').replace('X', '.'))


def setup_nav_cube(interactor):
    """Creates a 3D NavCube orientation marker in the top-right corner."""
    cube = vtk.vtkAnnotatedCubeActor()
    cube.SetXPlusFaceText("DIR")
    cube.SetXMinusFaceText("ESQ")
    cube.SetYPlusFaceText("POST")
    cube.SetYMinusFaceText("FRENTE")
    cube.SetZPlusFaceText("TOPO")
    cube.SetZMinusFaceText("BASE")

    cube.SetFaceTextScale(0.17)
    cube.SetTextEdgesVisibility(False)
    cube.GetTextEdgesProperty().SetColor(0.2, 0.3, 0.45)
    cube.GetTextEdgesProperty().SetLineWidth(1.5)
    cube.GetCubeProperty().SetColor(0.90, 0.93, 0.96)
    cube.GetCubeProperty().SetAmbient(1.0)
    cube.GetCubeProperty().SetDiffuse(0.0)
    cube.GetCubeProperty().SetEdgeVisibility(True)
    cube.GetCubeProperty().SetEdgeColor(0.2,0.35,0.5)
    cube.SetZFaceTextRotation(180)

    # Face coloring for clean industrial look
    cube.GetXPlusFaceProperty().SetColor(0.04, 0.18, 0.31)
    cube.GetXMinusFaceProperty().SetColor(0.04, 0.18, 0.31)
    cube.GetYPlusFaceProperty().SetColor(0.04, 0.18, 0.31)
    cube.GetYMinusFaceProperty().SetColor(0.04, 0.18, 0.31)
    cube.GetZPlusFaceProperty().SetColor(0.04, 0.18, 0.31)
    cube.GetZMinusFaceProperty().SetColor(0.04, 0.18, 0.31)

    # Resolve underlying vtkRenderWindowInteractor if a PyVista/Qt widget was passed
    real_interactor = interactor
    if hasattr(interactor, 'render_window') and interactor.render_window:
        real_interactor = interactor.render_window.GetInteractor()
    elif hasattr(interactor, 'GetRenderWindow') and interactor.GetRenderWindow():
        real_interactor = interactor.GetRenderWindow().GetInteractor()

    marker = vtk.vtkOrientationMarkerWidget()
    marker.SetOrientationMarker(cube)
    if real_interactor is not None and isinstance(real_interactor, vtk.vtkRenderWindowInteractor):
        marker.SetInteractor(real_interactor)
        marker.SetViewport(0.82, 0.77, 0.99, 0.99)
        marker.SetEnabled(1)
        marker.InteractiveOff()
    return marker, cube


class NavisLayerManagerDialog(W.QDialog):
    """Layer Manager & Appearance Profiler: customize DWG layer colors and visibility."""
    color_changed = QtCore.Signal(str, tuple)
    visibility_changed = QtCore.Signal(str, bool)

    def __init__(self, layer_manager, parent=None):
        super().__init__(parent)
        self.layer_manager = layer_manager
        self.setWindowTitle('NavModel Manager — Camadas')
        self.resize(520, 420)
        self.setStyleSheet("""
            QDialog {
                background-color: #f5f5f5;
                font-family: 'Segoe UI', Tahoma, sans-serif;
            }
            QTableWidget {
                background-color: white;
                border: 1px solid #d0d0d0;
                gridline-color: #f0f0f0;
            }
            QHeaderView::section {
                background-color: #e5e5e5;
                font-weight: 600;
                padding: 4px;
                border: 1px solid #d0d0d0;
            }
            QPushButton {
                background-color: #e1e1e1;
                border: 1px solid #adadad;
                padding: 4px 12px;
                min-width: 65px;
            }
            QPushButton:hover {
                background-color: #e5f1fb;
                border-color: #0078d4;
            }
        """)

        layout = W.QVBoxLayout(self)

        info = W.QLabel('Personalize as cores originais e visibilidade de cada camada do DWG:')
        layout.addWidget(info)

        self.table = W.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['Camada (Layer)', 'Cor Original', 'Ação'])
        self.table.horizontalHeader().setSectionResizeMode(0, W.QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, W.QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, W.QHeaderView.ResizeToContents)
        layout.addWidget(self.table)

        self.populate()

        # Close button
        btn_layout = W.QHBoxLayout()
        btn_layout.addStretch()
        close_btn = W.QPushButton('Fechar')
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    def populate(self):
        self.table.setRowCount(0)
        for layer_name, info in sorted(self.layer_manager.layers.items()):
            row = self.table.rowCount()
            self.table.insertRow(row)

            # Name item with checkstate
            item = W.QTableWidgetItem(f'{layer_name} ({info["count"]} peças)')
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Checked if info['visible'] else QtCore.Qt.Unchecked)
            self.table.setItem(row, 0, item)

            # Color preview swatch
            color = info['color']
            color_btn = W.QPushButton('   ')
            color_btn.setStyleSheet(f'background-color: rgb({color[0]},{color[1]},{color[2]}); border: 1px solid #333;')
            color_btn.clicked.connect(lambda _, l=layer_name, c=color: self.choose_color(l, c))
            self.table.setCellWidget(row, 1, color_btn)

            # Change button
            edit_btn = W.QPushButton('Alterar Cor')
            edit_btn.clicked.connect(lambda _, l=layer_name, c=color: self.choose_color(l, c))
            self.table.setCellWidget(row, 2, edit_btn)

        self.table.itemChanged.connect(self.on_item_changed)

    def on_item_changed(self, item):
        layer_text = item.text().split(' (')[0].strip()
        visible = (item.checkState() == QtCore.Qt.Checked)
        self.layer_manager.set_visible(layer_text, visible)
        self.visibility_changed.emit(layer_text, visible)

    def choose_color(self, layer_name: str, current_color: Tuple[int, int, int]):
        initial = QtGui.QColor(*current_color)
        chosen = W.QColorDialog.getColor(initial, self, f'Escolher cor para a camada: {layer_name}')
        if chosen.isValid():
            new_rgb = (chosen.red(), chosen.green(), chosen.blue())
            self.layer_manager.set_color(layer_name, new_rgb)
            self.color_changed.emit(layer_name, new_rgb)
            self.populate()
