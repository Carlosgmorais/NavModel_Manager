"""Coalesced, spatially accelerated measurement with persistent overlays."""
import numpy as np
import vtk
from PySide6 import QtCore, QtWidgets as W
from .surface_picker import SurfacePicker


class MeasurementTool(QtCore.QObject):
    FRAME_MS = 25

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.start = None
        self.hover = None
        self.names = []
        self.last_result = None
        self.surface_picker = SurfacePicker(window)
        self._pending = None
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._update_hover)
        widget = window.plot.interactor
        widget.setMouseTracking(True)
        widget.installEventFilter(self)
        self.panel = W.QTreeWidget()
        self.panel.setHeaderLabels(['Medição', 'm'])
        self._rows = {}
        for title in ('Início', 'Fim', 'Diferença X/Y/Z'):
            parent = W.QTreeWidgetItem(self.panel, [title])
            self._rows[title] = [W.QTreeWidgetItem(parent, [axis, '—']) for axis in 'XYZ']
            parent.setExpanded(True)
        self._distance = W.QTreeWidgetItem(self.panel, ['Distância', '—'])
        self._capture = W.QTreeWidgetItem(self.panel, ['Captura', 'Face'])
        self.snap_mode = W.QComboBox()
        self.snap_mode.addItems(['Face', 'Face e vértices', 'Face, vértices e arestas da malha'])
        self.snap_mode.setCurrentIndex(2)
        self.snap_mode.setToolTip('Arestas incluem as bordas da triangulação disponível. Coordenadas em metros, sem deslocar a origem do modelo.')
        content = W.QWidget()
        layout = W.QVBoxLayout(content)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.snap_mode)
        layout.addWidget(self.panel)
        self.dock = W.QDockWidget('Ferramentas de medição', window)
        self.dock.setWidget(content)
        window.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.dock)
        self.dock.hide()
        self._create_overlays()

    def _create_overlays(self):
        plot = self.window.plot
        self._lines, self._labels, self._actors = [], [], []
        colors = [(1., .67, .05), (.9, .16, .14), (.12, .72, .22), (.1, .35, 1.)]
        for index, color in enumerate(colors):
            line = vtk.vtkLineSource()
            line.SetOutputPointsPrecision(vtk.vtkAlgorithm.DOUBLE_PRECISION)
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(line.GetOutputPort())
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*color)
            actor.GetProperty().SetLineWidth(3 if index == 0 else 2)
            actor.GetProperty().SetLighting(False)
            actor.SetPickable(False)
            actor.SetUseBounds(False)
            actor.SetVisibility(False)
            name = 'dimension-persistent-' + str(index)
            plot.add_actor(actor, name=name, reset_camera=False, render=False, pickable=False)
            self.names.append(name)
            label = vtk.vtkBillboardTextActor3D()
            label.GetTextProperty().SetColor(*color)
            label.GetTextProperty().SetFontSize(15)
            label.GetTextProperty().SetBold(True)
            label.GetTextProperty().SetBackgroundColor(.035, .12, .21)
            label.GetTextProperty().SetBackgroundOpacity(.86)
            label.SetPickable(False)
            label.SetUseBounds(False)
            label.SetVisibility(False)
            lname = 'dimension-persistent-label-' + str(index)
            plot.add_actor(label, name=lname, reset_camera=False, render=False, pickable=False)
            self.names.append(lname)
            self._lines.append(line)
            self._labels.append(label)
            self._actors.append(actor)
        points = vtk.vtkPoints()
        points.SetDataTypeToDouble()
        points.SetNumberOfPoints(4)
        lines = vtk.vtkCellArray()
        lines.InsertNextCell(5)
        for i in (0, 1, 2, 3, 0):
            lines.InsertCellPoint(i)
        square = vtk.vtkPolyData()
        square.SetPoints(points)
        square.SetLines(lines)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(square)
        marker = vtk.vtkActor()
        marker.SetMapper(mapper)
        marker.GetProperty().SetColor(1., .7, .05)
        marker.GetProperty().SetLineWidth(2.5)
        marker.GetProperty().SetLighting(False)
        marker.SetVisibility(False)
        marker.SetPickable(False)
        marker.SetUseBounds(False)
        plot.add_actor(marker, name='measure-snap-square', reset_camera=False, render=False, pickable=False)
        self._square, self._square_points, self._marker = square, points, marker
        endpoint_data = vtk.vtkPolyData()
        endpoint_points = vtk.vtkPoints()
        endpoint_points.SetDataTypeToDouble()
        endpoint_points.SetNumberOfPoints(2)
        endpoint_data.SetPoints(endpoint_points)
        vertices = vtk.vtkCellArray()
        for i in (0, 1):
            vertices.InsertNextCell(1)
            vertices.InsertCellPoint(i)
        endpoint_data.SetVerts(vertices)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(endpoint_data)
        endpoints = vtk.vtkActor()
        endpoints.SetMapper(mapper)
        endpoints.GetProperty().SetColor(1., .72, .05)
        endpoints.GetProperty().SetPointSize(8)
        endpoints.GetProperty().SetRenderPointsAsSpheres(True)
        endpoints.SetPickable(False)
        endpoints.SetUseBounds(False)
        endpoints.SetVisibility(False)
        plot.add_actor(endpoints, name='measure-endpoints', reset_camera=False, render=False, pickable=False)
        self._endpoint_points, self._endpoint_data, self._endpoints = endpoint_points, endpoint_data, endpoints

    def deactivate(self):
        self._timer.stop()
        self._pending = None
        self.start = self.hover = None
        self._marker.SetVisibility(False)
        self.window.plot.interactor.unsetCursor()
        self.window.plot.render()

    def clear(self):
        self._timer.stop()
        self._pending = None
        self.start = self.hover = self.last_result = None
        for actor in self._actors + self._labels + [self._marker, self._endpoints]:
            actor.SetVisibility(False)
        for rows in self._rows.values():
            for row in rows:
                row.setText(1, '—')
        self._distance.setText(1, '—')
        self.window.plot.render()

    def _position(self, event):
        widget = self.window.plot.interactor
        width, height = widget.GetRenderWindow().GetSize()
        return (event.position().x() * width / max(1, widget.width()),
                (widget.height() - event.position().y()) * height / max(1, widget.height()))

    def _hit(self, x, y):
        if self.window.cube.contains(x, y):
            return None
        mode = self.snap_mode.currentIndex()
        hit = self.surface_picker.pick_hit(x, y, snap=mode > 0)
        if hit is not None and mode == 1 and hit.kind == 'Aresta da malha':
            hit = self.surface_picker.pick_hit(x, y, snap=False)
        return hit

    def pick(self, event):
        hit = self._hit(*self._position(event))
        return None if hit is None else hit.point.copy()

    def eventFilter(self, obj, event):
        if not getattr(self.window, 'measure_mode', False):
            return False
        kind = event.type()
        if kind == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_Escape:
            self.window.measure_mode = False
            self.deactivate()
            return True
        if kind == QtCore.QEvent.Leave:
            self._timer.stop()
            self._pending = None
            self._marker.SetVisibility(False)
            self.window.plot.render()
            return False
        if kind in (QtCore.QEvent.MouseMove, QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonRelease):
            x, y = self._position(event)
            if self.window.cube.contains(x, y):
                self._marker.SetVisibility(False)
                return False
        if kind == QtCore.QEvent.MouseButtonPress and event.button() == QtCore.Qt.LeftButton:
            self._timer.stop()
            self._pending = None
            hit = self._hit(x, y)
            if hit is not None:
                if self.start is None:
                    self.clear()
                    self.start = hit.point.copy()
                    self.dock.show()
                    self.draw(hit.point, render=False)
                else:
                    self.draw(hit.point, render=False)
                    self.start = None
                self._show_marker(hit)
                self.window.plot.render()
            return True
        if kind == QtCore.QEvent.MouseButtonRelease and event.button() == QtCore.Qt.LeftButton:
            return True
        if kind == QtCore.QEvent.MouseMove:
            if event.buttons() & (QtCore.Qt.MiddleButton | QtCore.Qt.RightButton):
                self._marker.SetVisibility(False)
                return False
            self._pending = (x, y)
            # Only the most recent move is processed; at most one queued frame.
            if not self._timer.isActive():
                self._timer.start(self.FRAME_MS)
            return True
        return False

    def _show_marker(self, hit):
        point, normal = hit.point, hit.normal
        renderer = self.window.plot.renderer
        renderer.SetWorldPoint(*point, 1.)
        renderer.WorldToDisplay()
        x, y, z = renderer.GetDisplayPoint()
        renderer.SetDisplayPoint(x + 7., y, z)
        renderer.DisplayToWorld()
        p = np.asarray(renderer.GetWorldPoint())
        radius = max(float(np.linalg.norm(p[:3] / p[3] - point)), 1e-9)
        basis = np.eye(3)[np.argmin(abs(normal))]
        u = np.cross(normal, basis)
        u /= np.linalg.norm(u)
        v = np.cross(normal, u)
        camera = np.asarray(renderer.GetActiveCamera().GetPosition()) - point
        camera /= max(np.linalg.norm(camera), 1e-30)
        center = point + camera * radius * .025
        for i, (a, b) in enumerate(((-1, -1), (1, -1), (1, 1), (-1, 1))):
            self._square_points.SetPoint(i, center + radius * (a * u + b * v))
        self._square_points.Modified()
        self._square.Modified()
        self._marker.GetProperty().SetColor(*( (1., .7, .05) if hit.kind == 'Face' else (.1, 1., .9) ))
        self._marker.SetVisibility(True)
        self._capture.setText(1, hit.kind)

    def _update_hover(self):
        if not self.window.measure_mode or self._pending is None:
            return
        position, self._pending = self._pending, None
        hit = self._hit(*position)
        self.hover = None if hit is None else hit.point.copy()
        self.window.plot.interactor.setCursor(QtCore.Qt.CrossCursor if hit is not None else QtCore.Qt.ForbiddenCursor)
        self._marker.SetVisibility(hit is not None)
        if hit is not None:
            self._show_marker(hit)
            if self.start is not None:
                self.draw(hit.point, render=False)
        self.window.plot.render()

    def draw(self, end, render=True):
        if self.start is None:
            return
        a, b = np.asarray(self.start, float), np.asarray(end, float)
        delta = b - a
        distance = float(np.linalg.norm(delta))
        points = [a, np.array([b[0], a[1], a[2]]), np.array([b[0], b[1], a[2]]), b]
        pairs = [(a, b)] + list(zip(points[:-1], points[1:]))
        texts = [f'{distance:.4f} m'] + [f'Δ{axis} {value:+.4f} m' for axis, value in zip('XYZ', delta)]
        for line, actor, label, (p, q), text in zip(self._lines, self._actors, self._labels, pairs, texts):
            line.SetPoint1(p)
            line.SetPoint2(q)
            line.Modified()
            actor.SetVisibility(bool(np.linalg.norm(q - p) > 1e-12))
            label.SetInput(text)
            label.SetPosition((p + q) / 2)
            label.SetVisibility(True)
        self._endpoint_points.SetPoint(0, a)
        self._endpoint_points.SetPoint(1, b)
        self._endpoint_points.Modified()
        self._endpoint_data.Modified()
        self._endpoints.SetVisibility(True)
        for title, values in [('Início', a), ('Fim', b), ('Diferença X/Y/Z', delta)]:
            for row, value in zip(self._rows[title], values):
                row.setText(1, f'{value:.6f}')
        self._distance.setText(1, f'{distance:.6f}')
        self.last_result = dict(start=a.tolist(), end=b.tolist(), delta=delta.tolist(), distance=distance)
        if render:
            self.window.plot.render()
