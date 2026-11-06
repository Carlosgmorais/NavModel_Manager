"""World-coordinate section planes and oriented section boxes.

Clipping is performed by VTK on the GPU, including instanced model actors.  The
same half-space equations are exposed to picking and publishing so invisible
geometry cannot become a measurement endpoint.  Handles are small, persistent
actors; dragging changes plane coefficients, never rebuilds model geometry.
"""
from dataclasses import dataclass, field
import itertools
import math

import numpy as np
import vtk
from PySide6 import QtCore, QtWidgets as W


COLORS = ((.91, .16, .13), (.12, .70, .23), (.12, .36, .95))


def rotation_matrix(degrees):
    x, y, z = np.radians(degrees)
    cx, cy, cz = np.cos((x, y, z)); sx, sy, sz = np.sin((x, y, z))
    return np.array(((cz*cy, cz*sy*sx-sz*cx, cz*sy*cx+sz*sx),
                     (sz*cy, sz*sy*sx+cz*cx, sz*sy*cx-cz*sx),
                     (-sy, cy*sx, cy*cx)), dtype=float)


def rotation_degrees(matrix):
    y = math.asin(float(np.clip(-matrix[2, 0], -1, 1)))
    if abs(math.cos(y)) > 1e-8:
        x, z = math.atan2(matrix[2, 1], matrix[2, 2]), math.atan2(matrix[1, 0], matrix[0, 0])
    else:
        x, z = math.atan2(-matrix[1, 2], matrix[1, 1]), 0
    return np.degrees((x, y, z))


def axis_rotation(axis, angle):
    u = np.asarray(axis, dtype=float); u = u / np.linalg.norm(u)
    cross = np.array(((0, -u[2], u[1]), (u[2], 0, -u[0]), (-u[1], u[0], 0)))
    return np.eye(3)*math.cos(angle)+(1-math.cos(angle))*np.outer(u, u)+math.sin(angle)*cross


@dataclass
class SectionState:
    enabled: bool = False
    mode: str = 'plane'
    axis: int = 2
    inverted: bool = False
    center: np.ndarray = field(default_factory=lambda: np.zeros(3, dtype=float))
    rotation: np.ndarray = field(default_factory=lambda: np.eye(3))
    half_sizes: np.ndarray = field(default_factory=lambda: np.ones(3))

    def equations(self):
        if not self.enabled:
            return np.empty((0, 4), dtype=float)
        if self.mode == 'plane':
            normal = self.rotation[:, self.axis] * (-1 if self.inverted else 1)
            return np.array([np.r_[normal, -np.dot(normal, self.center)]])
        equations = []
        for axis in range(3):
            for sign in (-1, 1):
                outward = self.rotation[:, axis]*sign
                origin = self.center+outward*self.half_sizes[axis]
                equations.append(np.r_[-outward, np.dot(outward, origin)])
        return np.asarray(equations)

    def contains(self, point, tolerance=1e-7):
        eq = self.equations()
        return bool(np.all(eq[:, :3] @ np.asarray(point)+eq[:, 3] >= -tolerance))

    def move_face(self, axis, sign, distance):
        """Move one face, keep its opposite fixed, and prevent inversion."""
        distance = max(float(distance), -2*self.half_sizes[axis]+1e-6)
        self.center = self.center + self.rotation[:, axis]*sign*distance*.5
        self.half_sizes[axis] += distance*.5

    def fit(self, bounds):
        b = np.asarray(bounds, dtype=float).reshape(3, 2)
        if not np.all(np.isfinite(b)) or np.any(b[:, 1] < b[:, 0]):
            return
        corners = np.array(list(itertools.product(*b)))
        local = corners @ self.rotation
        lo, hi = local.min(axis=0), local.max(axis=0)
        self.center = ((lo+hi)*.5) @ self.rotation.T
        self.half_sizes = np.maximum((hi-lo)*.51, 1e-4)


class SectioningTool(QtCore.QObject):
    """Section UI, clipping lifecycle, and screen-aware transform handles."""
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.state = SectionState()
        self.planes = [vtk.vtkPlane() for _ in range(6)]
        self.revision = 0
        self.transform_mode = 'move'
        self.show_handles = True
        self.drag = None
        self._attached = []
        self._initialized = False
        self._syncing = False
        self._closed = False
        self._handle_data = []
        self._ribbon_enable = None
        self._ribbon_modes = {}
        self._ribbon_transforms = {}
        self._make_actors()
        self._make_panel()
        window.plot.interactor.installEventFilter(self)
        self._render_observer = window.plot.renderer.AddObserver('StartEvent', self._before_render)

    @property
    def enabled(self):
        return self.state.enabled

    def clipping_equations(self):
        return self.state.equations()

    def contains_point(self, point, tolerance=1e-7):
        return self.state.contains(point, tolerance)

    def _make_actors(self):
        plot = self.window.plot
        self.outline_data = vtk.vtkPolyData()
        mapper = vtk.vtkPolyDataMapper(); mapper.SetInputData(self.outline_data)
        self.outline = vtk.vtkActor(); self.outline.SetMapper(mapper)
        self.outline.GetProperty().SetColor(.04, .62, .92)
        self.outline.GetProperty().SetLineWidth(2)
        self.outline.GetProperty().SetLighting(False)
        self.outline.PickableOff(); self.outline.UseBoundsOff(); self.outline.VisibilityOff()
        plot.renderer.AddActor(self.outline)
        self.face_data = vtk.vtkPolyData()
        mapper = vtk.vtkPolyDataMapper(); mapper.SetInputData(self.face_data)
        self.face_actor = vtk.vtkActor(); self.face_actor.SetMapper(mapper)
        self.face_actor.GetProperty().SetColor(.1, .63, .94)
        self.face_actor.GetProperty().SetOpacity(.13)
        self.face_actor.GetProperty().SetLighting(False)
        self.face_actor.PickableOff(); self.face_actor.UseBoundsOff(); self.face_actor.VisibilityOff()
        plot.renderer.AddActor(self.face_actor)
        # A dedicated transparent layer keeps handles readable through surfaces.
        # The existing CAD cube remains above this layer and receives its clicks.
        rw = plot.interactor.GetRenderWindow()
        rw.SetNumberOfLayers(max(3, rw.GetNumberOfLayers()))
        cube = getattr(self.window, 'cube', None)
        if cube is not None:
            cube.renderer.SetLayer(2)
        self.overlay = vtk.vtkRenderer(); self.overlay.SetLayer(1)
        self.overlay.SetInteractive(False)
        self.overlay.SetActiveCamera(plot.camera)
        rw.AddRenderer(self.overlay)
        source = vtk.vtkArrowSource(); source.SetTipResolution(16); source.SetShaftResolution(12)
        source.SetTipLength(.25); source.SetTipRadius(.065); source.SetShaftRadius(.023)
        source.Update()
        mapper = vtk.vtkPolyDataMapper(); mapper.SetInputConnection(source.GetOutputPort())
        self._arrow_source = source
        self.arrows = []
        for i in range(9):
            actor = vtk.vtkActor(); actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*COLORS[i if i < 3 else (i-3)//2])
            actor.GetProperty().SetLighting(False)
            actor.PickableOff(); actor.UseBoundsOff(); actor.VisibilityOff()
            self.overlay.AddActor(actor); self.arrows.append(actor)
        self.rings = []; self.ring_data = []
        for color in COLORS:
            data = vtk.vtkPolyData(); mapper = vtk.vtkPolyDataMapper(); mapper.SetInputData(data)
            actor = vtk.vtkActor(); actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*color); actor.GetProperty().SetLineWidth(3)
            actor.GetProperty().SetLighting(False)
            actor.PickableOff(); actor.UseBoundsOff(); actor.VisibilityOff()
            self.overlay.AddActor(actor); self.rings.append(actor); self.ring_data.append(data)

    @staticmethod
    def _spin(minimum=-1e12, maximum=1e12, step=.1, decimals=5):
        spin = W.QDoubleSpinBox(); spin.setRange(minimum, maximum)
        spin.setDecimals(decimals); spin.setSingleStep(step)
        spin.setKeyboardTracking(False)
        return spin

    def _make_panel(self):
        panel = W.QWidget(); layout = W.QVBoxLayout(panel)
        self.enable_check = W.QCheckBox('Ativar cortes (Enable Sectioning)')
        self.enable_check.toggled.connect(self.set_enabled); layout.addWidget(self.enable_check)
        form = W.QFormLayout(); layout.addLayout(form)
        self.mode_combo = W.QComboBox(); self.mode_combo.addItems(['Plano (Plane)', 'Caixa (Box)'])
        self.mode_combo.currentIndexChanged.connect(lambda i: self.set_mode('plane' if i == 0 else 'box'))
        form.addRow('Modo', self.mode_combo)
        self.axis_combo = W.QComboBox(); self.axis_combo.addItems(['X — lateral', 'Y — frontal', 'Z — topo'])
        self.axis_combo.setCurrentIndex(2); self.axis_combo.currentIndexChanged.connect(self.set_axis)
        form.addRow('Alinhar plano', self.axis_combo)
        self.invert_check = W.QCheckBox('Inverter lado conservado')
        self.invert_check.toggled.connect(self.set_inverted); form.addRow(self.invert_check)
        self.handles_check = W.QCheckBox('Mostrar plano, caixa e manipuladores'); self.handles_check.setChecked(True)
        self.handles_check.toggled.connect(self.set_show_handles); form.addRow(self.handles_check)
        position = W.QGroupBox('Posição no modelo (m)'); f = W.QFormLayout(position); self.position_spins = []
        for i, axis in enumerate('XYZ'):
            spin = self._spin(); spin.valueChanged.connect(lambda value, a=i: self._set_position(a, value))
            f.addRow(axis, spin); self.position_spins.append(spin)
        layout.addWidget(position)
        rotation = W.QGroupBox('Rotação (graus · X, depois Y, depois Z)'); f = W.QFormLayout(rotation); self.rotation_spins = []
        for i, axis in enumerate('XYZ'):
            spin = self._spin(-180, 180, 5, 3); spin.setWrapping(True)
            spin.valueChanged.connect(self._set_rotation)
            f.addRow(axis, spin); self.rotation_spins.append(spin)
        layout.addWidget(rotation)
        self.box_group = W.QGroupBox('Dimensões da caixa (m)'); f = W.QFormLayout(self.box_group); self.size_spins = []
        for i, axis in enumerate('XYZ'):
            spin = self._spin(.000001, 2e12, .1, 6)
            spin.valueChanged.connect(lambda value, a=i: self._set_size(a, value))
            f.addRow(axis, spin); self.size_spins.append(spin)
        self.face_combo = W.QComboBox(); self.face_combo.addItems(['−X', '+X', '−Y', '+Y', '−Z', '+Z'])
        f.addRow('Face', self.face_combo)
        face_row = W.QWidget(); h = W.QHBoxLayout(face_row); h.setContentsMargins(0, 0, 0, 0)
        self.face_distance = self._spin(); self.face_distance.setToolTip('Positivo expande; negativo contrai. A face oposta permanece fixa.')
        h.addWidget(self.face_distance); button = W.QPushButton('Mover face'); button.clicked.connect(self._move_selected_face); h.addWidget(button)
        f.addRow('Deslocamento (m)', face_row); layout.addWidget(self.box_group)
        help_text = W.QLabel('Mover: arraste as setas X/Y/Z. Na caixa, as seis setas menores ajustam cada face.\nGirar: arraste os anéis. Esc cancela o arrasto. A medição usa somente a geometria visível.')
        help_text.setWordWrap(True); layout.addWidget(help_text)
        layout.addStretch()
        scroll = W.QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(panel)
        self.dock = W.QDockWidget('Sectioning Tools — Cortes', self.window)
        self.dock.setWidget(scroll); self.dock.setMinimumWidth(300)
        self.window.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.dock)
        self.dock.hide(); self._sync_controls()

    def add_ribbon(self, ribbon):
        page = ribbon.page('Sectioning Tools')
        group = ribbon.group(page, 'Cortes')
        self._ribbon_enable = group.add_large_button('Ativar\ncortes', 'select', self.toggle,
                                                    'Enable Sectioning — ativar/desativar cortes')
        self._ribbon_enable.setCheckable(True)
        for mode, text in [('plane', 'Plane'), ('box', 'Box')]:
            button = group.add_large_button(text, 'isolate', lambda checked=False, m=mode: self.set_mode(m),
                                            'Corte por plano' if mode == 'plane' else 'Caixa orientada com seis faces ajustáveis')
            button.setCheckable(True); self._ribbon_modes[mode] = button
        group = ribbon.group(page, 'Alinhamento')
        for axis in range(3):
            group.add_large_button('Plano '+ 'XYZ'[axis], 'layers', lambda checked=False, a=axis: self.set_axis(a))
        group.add_large_button('Alinhar\nvista', 'look', self.align_view, 'Alinhar à direção da câmera')
        group.add_large_button('Ajustar\nseleção', 'select', self.fit_selection, 'Ajustar o corte aos limites da seleção')
        self.invert_button = group.add_large_button('Inverter', 'reset', self.invert, 'Inverter o lado conservado do plano')
        group = ribbon.group(page, 'Transformação')
        for mode, text in [('move', 'Mover'), ('rotate', 'Girar')]:
            button = group.add_large_button(text, 'pan' if mode == 'move' else 'orbit',
                                           lambda checked=False, m=mode: self.set_transform_mode(m))
            button.setCheckable(True); self._ribbon_transforms[mode] = button
        group.add_large_button('Redefinir', 'reset', self.reset, 'Enquadrar o corte em todos os modelos')
        group.add_large_button('Valores', 'properties', self.show_panel, 'Posição, rotação e seis faces em coordenadas do modelo')
        self._sync_controls()
        return page

    def show_panel(self):
        self.dock.show(); self.dock.raise_()

    def toggle(self, checked=False):
        self.set_enabled(not self.state.enabled)

    def set_enabled(self, enabled):
        if self._syncing:
            return
        self.state.enabled = bool(enabled)
        if enabled and not self._initialized:
            self._fit_models(); self._initialized = True
        if enabled:
            self.show_panel()
        self.drag = None
        self.refresh_models()

    def set_mode(self, mode):
        if self._syncing:
            return
        if mode not in ('plane', 'box'):
            raise ValueError('Section mode must be plane or box')
        self.state.mode = mode
        self.set_enabled(True)

    def set_axis(self, axis):
        if self._syncing:
            return
        self.state.axis = int(axis); self.state.rotation = np.eye(3)
        self.state.mode = 'plane'; self.state.inverted = False
        self.set_enabled(True)

    def set_inverted(self, inverted):
        if not self._syncing:
            self.state.inverted = bool(inverted); self._changed()

    def invert(self):
        if self.state.mode == 'plane':
            self.set_inverted(not self.state.inverted)

    def set_transform_mode(self, mode):
        self.transform_mode = mode
        self.set_enabled(True)

    def set_show_handles(self, value):
        self.show_handles = bool(value); self._changed()

    def _model_bounds(self, actors=None):
        actors = actors if actors is not None else [actor for model in self.window.models for actor in model['actors']]
        bounds = []
        for actor in actors:
            if actor.GetVisibility():
                b = np.asarray(actor.GetBounds()).reshape(3, 2)
                if np.all(np.isfinite(b)) and np.all(b[:, 1] >= b[:, 0]):
                    bounds.append(b)
        if not bounds:
            return (-1, 1, -1, 1, -1, 1)
        bounds = np.stack(bounds)
        return np.stack((bounds[:, :, 0].min(axis=0), bounds[:, :, 1].max(axis=0)), axis=1).ravel()

    def _fit_models(self):
        self.state.fit(self._model_bounds())

    def reset(self):
        self.state.rotation = np.eye(3); self.state.inverted = False
        self._fit_models(); self._initialized = True; self.set_enabled(True)

    def fit_selection(self):
        actors = self.window.selected_actors()
        if not actors:
            self.window.statusBar().showMessage('Selecione um componente na árvore para ajustar o corte.', 5000)
            return
        self.state.fit(self._model_bounds(actors)); self._initialized = True; self.set_enabled(True)

    def align_view(self):
        normal = np.asarray(self.window.plot.camera.GetDirectionOfProjection(), dtype=float)
        normal /= max(np.linalg.norm(normal), 1e-12)
        up = np.asarray(self.window.plot.camera.GetViewUp(), dtype=float)
        right = np.cross(up, normal); right /= max(np.linalg.norm(right), 1e-12)
        up = np.cross(normal, right)
        frame = np.column_stack((right, up, normal))
        if self.state.mode == 'plane':
            self.state.axis = 2
        self.state.rotation = frame; self.state.inverted = False
        self.set_enabled(True)

    def _set_position(self, axis, value):
        if not self._syncing:
            self.state.center[axis] = value; self._changed()

    def _set_rotation(self, value=None):
        if not self._syncing:
            self.state.rotation = rotation_matrix([spin.value() for spin in self.rotation_spins]); self._changed()

    def _set_size(self, axis, value):
        if not self._syncing:
            self.state.half_sizes[axis] = value*.5; self._changed()

    def _move_selected_face(self):
        face = self.face_combo.currentIndex()
        self.state.move_face(face//2, -1 if face % 2 == 0 else 1, self.face_distance.value())
        self._changed()

    def refresh_models(self):
        """Call after append/refresh; attach only to actual model mappers."""
        current = [actor.GetMapper() for model in self.window.models for actor in model['actors'] if actor.GetMapper() is not None]
        for mapper in self._attached:
            if mapper not in current:
                self._remove_planes(mapper)
        self._attached = current
        for mapper in current:
            self._remove_planes(mapper)
            if self.state.enabled:
                for plane in self.planes[:1 if self.state.mode == 'plane' else 6]:
                    mapper.AddClippingPlane(plane)
        self._changed()

    def _remove_planes(self, mapper):
        collection = mapper.GetClippingPlanes()
        if collection is not None:
            for plane in self.planes:
                if collection.IsItemPresent(plane):
                    mapper.RemoveClippingPlane(plane)

    def clear(self):
        self.state = SectionState(); self._initialized = False; self.drag = None
        for mapper in self._attached:
            self._remove_planes(mapper)
        self._attached = []
        self.dock.hide(); self._changed()

    def _sync_controls(self):
        self._syncing = True
        try:
            state = self.state
            self.enable_check.setChecked(state.enabled)
            self.mode_combo.setCurrentIndex(0 if state.mode == 'plane' else 1)
            self.axis_combo.setCurrentIndex(state.axis)
            self.axis_combo.setEnabled(state.mode == 'plane')
            self.invert_check.setChecked(state.inverted); self.invert_check.setEnabled(state.mode == 'plane')
            self.box_group.setVisible(state.mode == 'box')
            for spin, value in zip(self.position_spins, state.center):
                spin.setValue(float(value))
            for spin, value in zip(self.rotation_spins, rotation_degrees(state.rotation)):
                spin.setValue(float(value))
            for spin, value in zip(self.size_spins, state.half_sizes*2):
                spin.setValue(float(value))
            if self._ribbon_enable:
                self._ribbon_enable.setChecked(state.enabled)
                self.invert_button.setEnabled(state.mode == 'plane')
            for mode, button in self._ribbon_modes.items():
                button.setChecked(state.mode == mode)
            for mode, button in self._ribbon_transforms.items():
                button.setChecked(self.transform_mode == mode)
        finally:
            self._syncing = False

    def _changed(self, render=True, sync=True):
        self.revision += 1
        for plane, equation in zip(self.planes, self.clipping_equations()):
            plane.SetNormal(*equation[:3]); plane.SetOrigin(*(-equation[3]*equation[:3]))
        self._update_outline(); self._update_handles()
        if sync:
            self._sync_controls()
        if render:
            self.window.plot.render()

    @staticmethod
    def _set_polydata(data, points, lines=(), polygon=False):
        vtk_points = vtk.vtkPoints(); vtk_points.SetDataTypeToDouble()
        for point in points:
            vtk_points.InsertNextPoint(*point)
        data.SetPoints(vtk_points)
        cells = vtk.vtkCellArray()
        for indices in lines:
            cells.InsertNextCell(len(indices))
            for index in indices:
                cells.InsertCellPoint(int(index))
        if polygon:
            data.SetPolys(cells)
        else:
            data.SetLines(cells)
        data.Modified()

    def _update_outline(self):
        visible = self.state.enabled and self.show_handles
        self.outline.SetVisibility(visible); self.face_actor.SetVisibility(visible and self.state.mode == 'plane')
        if not visible:
            return
        state = self.state
        if state.mode == 'plane':
            axes = [i for i in range(3) if i != state.axis]
            size = max(float(np.linalg.norm(state.half_sizes)), .001)
            a, b = state.rotation[:, axes[0]]*size, state.rotation[:, axes[1]]*size
            points = np.array([state.center-a-b, state.center+a-b, state.center+a+b, state.center-a+b])
            self._set_polydata(self.outline_data, points, [(0, 1, 2, 3, 0)])
            self._set_polydata(self.face_data, points, [(0, 1, 2, 3)], polygon=True)
        else:
            signs = np.array(list(itertools.product((-1, 1), repeat=3)))
            points = state.center + (signs*state.half_sizes) @ state.rotation.T
            lines = [(i, j) for i in range(8) for j in range(i+1, 8) if np.count_nonzero(signs[i] != signs[j]) == 1]
            self._set_polydata(self.outline_data, points, lines)

    def _pixels_to_world(self, point):
        camera = self.window.plot.camera
        height = max(1, self.window.plot.interactor.GetRenderWindow().GetSize()[1])
        if camera.GetParallelProjection():
            return 2*camera.GetParallelScale()/height
        depth = np.dot(np.asarray(point)-camera.GetPosition(), camera.GetDirectionOfProjection())
        return 2*max(abs(float(depth)), .001)*math.tan(math.radians(camera.GetViewAngle())*.5)/height

    def _before_render(self, obj=None, event=None):
        if not self._closed and self.state.enabled:
            self.overlay.SetActiveCamera(self.window.plot.camera)
            self._update_handles()

    def _set_arrow(self, actor, origin, direction, length):
        u = np.asarray(direction, dtype=float)
        ref = np.array((0, 0, 1) if abs(u[2]) < .9 else (0, 1, 0))
        v = np.cross(ref, u); v /= np.linalg.norm(v); w = np.cross(u, v)
        matrix = vtk.vtkMatrix4x4()
        for row in range(3):
            for col, vector in enumerate((u, v, w)):
                matrix.SetElement(row, col, vector[row]*length)
            matrix.SetElement(row, 3, origin[row])
        actor.SetUserMatrix(matrix); actor.VisibilityOn()

    def _update_handles(self):
        for actor in self.arrows+self.rings:
            actor.VisibilityOff()
        self._handle_data = []
        if not self.state.enabled or not self.show_handles:
            return
        state = self.state; center = state.center
        length = max(self._pixels_to_world(center)*82, 1e-6)
        if self.transform_mode == 'move':
            for axis in range(3):
                direction = np.eye(3)[axis]
                self._set_arrow(self.arrows[axis], center, direction, length)
                self._handle_data.append(('move', axis, 1, center.copy(), direction, length))
            if state.mode == 'box':
                for axis in range(3):
                    for side, sign in enumerate((-1, 1)):
                        direction = state.rotation[:, axis]*sign
                        origin = center+direction*state.half_sizes[axis]
                        size = max(self._pixels_to_world(origin)*42, 1e-6)
                        self._set_arrow(self.arrows[3+axis*2+side], origin, direction, size)
                        self._handle_data.append(('face', axis, sign, origin, direction, size))
        else:
            angles = np.linspace(0, 2*math.pi, 97)
            for axis in range(3):
                others = [i for i in range(3) if i != axis]
                points = np.repeat(center[None, :], len(angles), axis=0)
                points[:, others[0]] += np.cos(angles)*length*.85
                points[:, others[1]] += np.sin(angles)*length*.85
                self._set_polydata(self.ring_data[axis], points, [tuple(range(len(points)))])
                self.rings[axis].VisibilityOn()
                self._handle_data.append(('rotate', axis, 1, center.copy(), np.eye(3)[axis], points))

    def _display(self, point):
        renderer = self.window.plot.renderer
        renderer.SetWorldPoint(*point, 1); renderer.WorldToDisplay()
        return np.asarray(renderer.GetDisplayPoint())[:2]

    def _ray(self, x, y):
        renderer = self.window.plot.renderer
        points = []
        for z in (0, 1):
            renderer.SetDisplayPoint(x, y, z); renderer.DisplayToWorld()
            p = np.asarray(renderer.GetWorldPoint()); points.append(p[:3]/p[3])
        direction = points[1]-points[0]
        return points[0], direction/max(np.linalg.norm(direction), 1e-15)

    def _event_position(self, event):
        widget = self.window.plot.interactor
        width, height = widget.GetRenderWindow().GetSize()
        return np.array((event.position().x()*width/max(widget.width(), 1),
                         (widget.height()-event.position().y())*height/max(widget.height(), 1)))

    @staticmethod
    def _segment_distance(point, a, b):
        ab = b-a; length = float(np.dot(ab, ab))
        t = np.clip(np.dot(point-a, ab)/max(length, 1e-15), 0, 1)
        return float(np.linalg.norm(point-a-t*ab))

    def handle_at(self, x, y):
        """Screen-space hit testing never traverses model triangles."""
        point = np.array((x, y)); hits = []
        widget = self.window.plot.interactor
        tolerance = 9*widget.GetRenderWindow().GetSize()[0]/max(widget.width(), 1)
        for handle in self._handle_data:
            mode, axis, sign, origin, direction, geometry = handle
            if mode == 'rotate':
                # An edge-on ring has no stable ray/plane angle; numeric rotation
                # remains available and other visible rings can be dragged.
                if abs(np.dot(direction, self.window.plot.camera.GetDirectionOfProjection())) < .04:
                    continue
                screen = [self._display(p) for p in geometry]
                distance = min(self._segment_distance(point, a, b) for a, b in zip(screen, screen[1:]))
            else:
                a, b = self._display(origin+direction*geometry*.20), self._display(origin+direction*geometry)
                if np.linalg.norm(b-a) < 8:
                    continue
                distance = self._segment_distance(point, a, b)
            if distance <= tolerance:
                hits.append((distance, handle))
        return min(hits, key=lambda item: item[0])[1] if hits else None

    def _axis_parameter(self, x, y, origin, direction):
        ray_origin, ray_direction = self._ray(x, y)
        dot = float(np.dot(direction, ray_direction)); denominator = 1-dot*dot
        if denominator < 1e-6:
            return None
        w = origin-ray_origin
        return (dot*np.dot(ray_direction, w)-np.dot(direction, w))/denominator

    def _rotation_vector(self, x, y, origin, normal):
        ray_origin, ray_direction = self._ray(x, y)
        denominator = float(np.dot(ray_direction, normal))
        if abs(denominator) < 1e-8:
            return None
        t = np.dot(origin-ray_origin, normal)/denominator
        point = ray_origin+t*ray_direction-origin
        length = np.linalg.norm(point)
        return point/length if length > 1e-9 else None

    def begin_drag(self, handle, x, y):
        mode, axis, sign, origin, direction, geometry = handle
        value = (self._rotation_vector(x, y, origin, direction) if mode == 'rotate'
                 else self._axis_parameter(x, y, origin, direction))
        if value is None:
            return False
        self.drag = dict(handle=handle, value=value, center=self.state.center.copy(),
                         rotation=self.state.rotation.copy(), sizes=self.state.half_sizes.copy())
        self.window.plot.interactor.setCursor(QtCore.Qt.ClosedHandCursor)
        return True

    def drag_to(self, x, y):
        if not self.drag:
            return
        drag = self.drag; mode, axis, sign, origin, direction, geometry = drag['handle']
        self.state.center = drag['center'].copy(); self.state.half_sizes = drag['sizes'].copy()
        if mode == 'rotate':
            vector = self._rotation_vector(x, y, origin, direction)
            if vector is None:
                return
            angle = math.atan2(np.dot(direction, np.cross(drag['value'], vector)), np.dot(drag['value'], vector))
            self.state.rotation = axis_rotation(direction, angle) @ drag['rotation']
        else:
            value = self._axis_parameter(x, y, origin, direction)
            if value is None:
                return
            delta = float(value-drag['value'])
            if mode == 'move':
                self.state.center += direction*delta
            else:
                self.state.move_face(axis, sign, delta)
        self._changed(sync=False)

    def end_drag(self, cancel=False):
        if self.drag and cancel:
            self.state.center = self.drag['center']; self.state.rotation = self.drag['rotation']; self.state.half_sizes = self.drag['sizes']
        self.drag = None; self.window.plot.interactor.unsetCursor(); self._changed()

    def eventFilter(self, obj, event):
        if not self.state.enabled or not self.show_handles:
            return False
        kind = event.type()
        if kind == QtCore.QEvent.KeyPress and event.key() == QtCore.Qt.Key_Escape and self.drag:
            self.end_drag(cancel=True); return True
        # The last installed Qt filter sees the event first.  Explicitly defer
        # to measurement and the existing CAD cube before grabbing a handle.
        if getattr(self.window, 'measure_mode', False):
            return False
        if kind not in (QtCore.QEvent.MouseButtonPress, QtCore.QEvent.MouseButtonRelease, QtCore.QEvent.MouseMove):
            return False
        x, y = self._event_position(event)
        if self.drag:
            if kind == QtCore.QEvent.MouseMove:
                self.drag_to(x, y); return True
            if kind == QtCore.QEvent.MouseButtonRelease and event.button() == QtCore.Qt.LeftButton:
                self.end_drag(); return True
        if self.window.cube.contains(x, y):
            return False
        if kind == QtCore.QEvent.MouseButtonPress and event.button() == QtCore.Qt.LeftButton:
            handle = self.handle_at(x, y)
            if handle is not None:
                return self.begin_drag(handle, x, y)
        return False

    def close(self):
        """Release VTK observers before the render window is destroyed."""
        if self._closed:
            return
        self._closed = True
        self.window.plot.renderer.RemoveObserver(self._render_observer)
        self.window.plot.interactor.removeEventFilter(self)
        self.window.plot.interactor.GetRenderWindow().RemoveRenderer(self.overlay)
