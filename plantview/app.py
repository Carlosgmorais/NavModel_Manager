import os
os.environ['QT_API'] = 'pyside6'
import sys
import math
import time
from pathlib import Path
import numpy as np
import vtk
from PySide6 import QtCore, QtGui, QtWidgets as W
from pyvistaqt import QtInteractor
import pyvista as pv

from .readers import read_model, parse_dwg_mesh_info
from .project import load_project, save_project
from .gpu import (
    configure_surface,
    setup_gpu_renderer,
    optimize_actor,
    gpu_report,
    add_optimized_parts,
    get_system_memory_info
)
from .navigation import NavisworksStyle
from .colors import LayerManager, resolve_layer_color
from .icons import create_navis_icon
from .ribbon import NavisRibbonBar
from .branding import app_icon, StartupSplash, VERSION
from .navcube import NavigationCube, CoordinateHUD
from .loading_dialog import NavisLoadingDialog
from .widgets import NavisCoordinateOverlay, NavisLayerManagerDialog, setup_nav_cube


class ImportWorker(QtCore.QThread):
    loaded = QtCore.Signal(object, object, object)
    failed = QtCore.Signal(str)
    progress = QtCore.Signal(float, str)

    def __init__(self, record):
        super().__init__()
        self.record = record
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            def on_progress(pct: float, msg: str) -> bool:
                if self._cancelled:
                    raise InterruptedError('Carregamento cancelado pelo usuário.')
                self.progress.emit(pct, msg)
                return True

            parts, warnings = read_model(
                self.record['path'],
                unit_scale=self.record.get('scale', 0.001),
                progress_callback=on_progress,
                force_reload=self.record.get('force_reload', False)
            )
            if self._cancelled:
                return
            self.loaded.emit(self.record, parts, warnings)
        except Exception as e:
            self.failed.emit(str(e))


class Window(W.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Novo modelo — NavModel Manager')
        self.setWindowIcon(app_icon())
        self.resize(1536, 900)
        self.selection_mode = False

        self.models = []
        self.queue = []
        self.worker = None
        self.loading_dialog = None
        self.dirty = False
        self.project_path = None
        self.restore_camera = None
        self.measure_points = []
        self.measure_mode = False
        self.total_triangles = 0
        self.layer_manager = LayerManager()
        self.selected_actor = None
        self.last_picked_actor = None
        self.drill_down_idx = 0
        self.fps_counter = 0
        self.fps_last_time = time.time()
        self.current_fps = 60.0

        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f3f6fa; color: #26364a; font-family: 'Segoe UI'; font-size: 14px; }
            QTreeWidget, QPlainTextEdit { background: white; alternate-background-color:#f3f6f9; border: 1px solid #e0e7ef; border-radius: 8px; padding: 6px; selection-background-color: #deedff; selection-color: #174f94; }
            QTreeWidget::item { padding: 2px 3px; height:22px; }
            QTreeWidget::item:selected { background: #deedff; color: #174f94; border-radius: 4px; }
            QPlainTextEdit { font-family: 'Consolas'; font-size: 11px; }
            QHeaderView::section { background: #edf2f8; border: none; padding: 9px; font-weight: 600; }
            QDockWidget { font-weight: 600; }
            QDockWidget::title { color:white; background: #0b3a66; padding: 10px 12px; border-radius: 5px; }
            QStatusBar { background: #152b46; color: #e1ecfa; padding: 7px; font-size: 13px; }
            QComboBox { background: white; border: 1px solid #cbd7e6; border-radius: 5px; padding: 3px 7px; }
            QToolTip { background: #152b46; color: white; border: none; padding: 7px; }
            QSplitter::handle { background: #e5ebf3; }
        """)

        # Central Widget & Layout
        central = W.QWidget()
        layout = W.QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 2. Viewport Area (Container for 3D View and Floating Overlays)
        viewport_container = W.QWidget()
        self.viewport_container = viewport_container
        vp_layout = W.QVBoxLayout(viewport_container)
        vp_layout.setContentsMargins(0,0,0,0)

        self.plot = QtInteractor(viewport_container)
        vp_layout.addWidget(self.plot.interactor)

        # Navisworks Viewport Sky Horizon Gradient
        # Top: Rich sky blue #5b8ecb | Bottom: Pale horizon ice blue #d8e8f8
        self.plot.set_background('#d8e8f8', top='#5b8ecb')
        self.plot.add_axes(interactive=False)

        # Setup GPU Renderer & Hardware Frustum Culling
        setup_gpu_renderer(self.plot)
        self.plot.disable_anti_aliasing()

        # Navisworks Free 3D Navigation
        self.navigation = NavisworksStyle(
            on_change=self.on_camera_changed,
            on_refresh=self.refresh_models
        )
        self.navigation.on_shift_pick = self.on_shift_pick
        self.plot.interactor.SetInteractorStyle(self.navigation)

        # 3D NavCube Orientation Marker in top-right corner
        self.nav_marker, self.nav_cube = setup_nav_cube(self.plot.interactor)
        self.cube = NavigationCube(self, self.nav_marker)
        from .measurement import MeasurementTool
        self.measure_tool = MeasurementTool(self)
        from .sectioning import SectioningTool
        self.section_tool = SectioningTool(self)
        self.coord_overlay = CoordinateHUD(self.plot)

        # 1. Navisworks Ribbon Bar (Created after plot is ready)
        self.ribbon = NavisRibbonBar(self)
        self.setMenuWidget(self.ribbon)
        layout.addWidget(viewport_container)
        self.setCentralWidget(central)

        # Dock 1: Selection Tree (Left Dock)
        self.tree = W.QTreeWidget()
        self.tree.setHeaderLabels(['Hierarquia de Componentes'])
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(W.QAbstractItemView.ExtendedSelection)
        self.tree.itemChanged.connect(self.visibility_changed)
        self.tree.itemSelectionChanged.connect(self.selection_changed)
        self.tree.itemSelectionChanged.connect(self.update_properties)
        self.tree_dock = W.QDockWidget('Árvore de Seleção', self)
        self.tree_dock.setWidget(self.tree)
        self.tree_dock.setMinimumWidth(240)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.tree_dock)

        # Dock 2: Log and Measurement Report (Bottom Dock)
        self.log = W.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log_dock = W.QDockWidget('Atividade e medições', self)
        self.log_dock.setWidget(self.log)
        self.log_dock.setMaximumHeight(160)
        self.addDockWidget(QtCore.Qt.BottomDockWidgetArea, self.log_dock)

        self.log_dock.hide()
        self.properties = W.QTreeWidget()
        self.properties.setColumnCount(2)
        self.properties.setHeaderLabels(['Propriedade', 'Valor'])
        self.properties.setHeaderHidden(True)
        self.properties.setColumnWidth(0, 105)
        self.properties.setAlternatingRowColors(True)
        self.properties_dock = W.QDockWidget('Propriedades', self)
        self.properties_dock.setWidget(self.properties)
        self.properties_dock.setMinimumWidth(260)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.properties_dock)
        self.resizeDocks([self.tree_dock, self.properties_dock], [338, 314], QtCore.Qt.Horizontal)
        self.update_properties()
        for dock in (self.tree_dock, self.properties_dock, self.log_dock):
            self.style_dock(dock)

        # Shortcuts
        self.shortcut_undo_cam = QtGui.QShortcut(QtGui.QKeySequence('Ctrl+Z'), self)
        self.shortcut_undo_cam.activated.connect(self.undo_camera)

        self.shortcut_hide = QtGui.QShortcut(QtGui.QKeySequence('Ctrl+H'), self)
        self.shortcut_hide.activated.connect(self.toggle_hide_selected)

        self.shortcut_diag = QtGui.QShortcut(QtGui.QKeySequence('Ctrl+Shift+D'), self)
        self.shortcut_diag.activated.connect(self.toggle_diagnostic_render_mode)

        # FPS & Performance Timer (Updates every 1000ms)
        self.perf_timer = QtCore.QTimer(self)
        self.perf_timer.timeout.connect(self.update_performance_status)
        self.perf_timer.start(1000)

        # Initial Status Bar Message
        self.statusBar().showMessage(
            'Pronto | Olhar: Botão esquerdo • Pan: Botão meio • Orbit: Shift + Botão meio • Zoom: Scroll • F5: Atualizar • Ctrl+Z: Desfazer câmera'
        )
        self.log.appendPlainText(f'NavModel Manager v{VERSION} iniciado com renderização OpenGL acelerada por GPU.')

    def style_dock(self, dock):
        bar = W.QWidget(dock)
        bar.setObjectName('dockHeader')
        bar.setFixedHeight(31)
        bar.setStyleSheet('QWidget#dockHeader {background:#0b3a66;border-top-left-radius:6px;border-top-right-radius:6px;} QLabel {background:transparent;color:white;font:15px "Segoe UI";} QToolButton {color:white;background:transparent;border:0;font-size:18px;} QToolButton:hover {background:#1565a8;}')
        layout = W.QHBoxLayout(bar); layout.setContentsMargins(10,0,5,0); layout.setSpacing(7)
        label = W.QLabel(('◇  ' if dock is self.tree_dock else 'ⓘ  ') + dock.windowTitle())
        layout.addWidget(label); layout.addStretch()
        pin = W.QToolButton(); pin.setText('↗'); pin.setToolTip('Destacar / acoplar painel')
        pin.clicked.connect(lambda: dock.setFloating(not dock.isFloating()))
        close = W.QToolButton(); close.setText('×'); close.setToolTip('Fechar painel'); close.clicked.connect(dock.hide)
        layout.addWidget(pin); layout.addWidget(close); dock.setTitleBarWidget(bar)

    def on_camera_changed(self, camera):
        """Update floating coordinate badges whenever camera moves."""
        eye = camera.GetPosition()
        focal = camera.GetFocalPoint()
        self.coord_overlay.update_coordinates(eye, focal)
        self.fps_counter += 1

    def update_performance_status(self):
        """Update real-time FPS and GPU statistics in the status bar."""
        now = time.time()
        dt = max(now - self.fps_last_time, 0.001)
        self.current_fps = self.fps_counter / dt
        self.fps_counter = 0
        self.fps_last_time = now

        mem = get_system_memory_info()
        actors_count = self.plot.renderer.GetActors().GetNumberOfItems() if self.plot.renderer else 0

        msg = (
            f'FPS: {self.current_fps:.1f} | '
            f'Triângulos: {self.total_triangles:,} | '
            f'Atores GPU: {actors_count} | '
            f'RAM: {mem["used_ram_mb"]:.0f} MB / {mem["total_ram_mb"]:.0f} MB ({mem["load_pct"]}%) | '
            f'Modelos: {len(self.models)} federados'
        )
        self.statusBar().showMessage(msg)

    def undo_camera(self):
        """Ctrl+Z: Revert camera to previous position/angle."""
        if self.navigation.undo_camera():
            self.statusBar().showMessage('Câmera restaurada para a vista anterior (Ctrl+Z).')
            if hasattr(self, 'view_overlay'):
                self.view_overlay.update_button_label()

    def toggle_projection(self):
        """Toggle camera projection between perspective and parallel (orthogonal)."""
        camera, _ = self.navigation.camera()
        if camera:
            self.save_camera_for_undo()
            is_parallel = camera.GetParallelProjection()
            if is_parallel:
                camera.ParallelProjectionOff()
                self.statusBar().showMessage('Projeção: Perspectiva 3D')
            else:
                camera.ParallelProjectionOn()
                self.statusBar().showMessage('Projeção: Paralela (Ortográfica)')
            self.plot.render()
            if hasattr(self, 'view_overlay'):
                self.view_overlay.update_button_label()

    def set_perspective(self):
        camera, _ = self.navigation.camera()
        if camera:
            self.save_camera_for_undo()
            camera.ParallelProjectionOff()
            self.plot.render()
            if hasattr(self, 'view_overlay'):
                self.view_overlay.update_button_label()
            self.statusBar().showMessage('Projeção: Perspectiva 3D')

    def set_orthogonal(self):
        camera, _ = self.navigation.camera()
        if camera:
            self.save_camera_for_undo()
            camera.ParallelProjectionOn()
            self.plot.render()
            if hasattr(self, 'view_overlay'):
                self.view_overlay.update_button_label()
            self.statusBar().showMessage('Projeção: Paralela (Ortográfica)')

    def save_camera_for_undo(self):
        self.navigation.save_camera_state()

    def view_isometric_smart(self):
        self.save_camera_for_undo()
        self.plot.view_isometric()
        self.plot.render()

    def view_top_smart(self):
        self.save_camera_for_undo()
        self.plot.view_xy()
        self.plot.render()

    def view_front_smart(self):
        self.save_camera_for_undo()
        self.plot.view_xz()
        self.plot.render()

    def view_right_smart(self):
        self.save_camera_for_undo()
        self.plot.view_yz()
        self.plot.render()

    def add_files(self):
        paths, _ = W.QFileDialog.getOpenFileNames(
            self,
            'Adicionar modelos na posição original (DWG, STEP, IFC, DXF)',
            '',
            'Modelos CAD (*.dwg *.step *.stp *.ifc *.dxf *.stl *.obj *.ply);;Todos (*)'
        )
        if paths:
            records = [
                {
                    'path': p,
                    'visible': True,
                    'scale': self.units.currentData(),
                    'mtime': os.path.getmtime(p) if os.path.exists(p) else 0
                }
                for p in paths
            ]
            self.enqueue(records)

    def enqueue(self, records):
        self.queue.extend(records)
        if self.worker is None:
            self.next_import()

    def next_import(self):
        if not self.queue:
            if self.loading_dialog:
                try:
                    self.loading_dialog.set_progress(100.0, 'Concluído')
                    self.loading_dialog.accept()
                    self.loading_dialog.close()
                except Exception:
                    pass
                self.loading_dialog = None
            if self.restore_camera:
                self.plot.camera_position = self.restore_camera
                self.restore_camera = None
            self.statusBar().showMessage(f'{len(self.models)} modelo(s) federado(s) | Coordenadas globais reais')
            return

        record = self.queue.pop(0)
        file_name = Path(record['path']).name

        # Launch Navisworks-style Loading Dialog
        if not self.loading_dialog:
            self.loading_dialog = NavisLoadingDialog(self)
            self.loading_dialog.cancelled.connect(self.cancel_import)
            self.loading_dialog.show()

        self.loading_dialog.set_file_name(file_name)
        self.loading_dialog.set_progress(0.0, f'Loading {file_name} ...')
        self.log.appendPlainText(f'\nImportando: {record["path"]}')

        self.worker = ImportWorker(record)
        self.worker.progress.connect(self.on_import_progress)
        self.worker.loaded.connect(self.add_result)
        self.worker.failed.connect(self.on_import_failed)
        self.worker.finished.connect(self.import_finished)
        self.worker.start()

    def on_import_progress(self, pct: float, msg: str):
        if self.loading_dialog:
            self.loading_dialog.set_progress(pct, msg)

    def cancel_import(self):
        if self.worker:
            self.worker.cancel()
            self.log.appendPlainText('Importação cancelada pelo usuário.')
            self.queue.clear()

    def on_import_failed(self, error_msg: str):
        if self.loading_dialog:
            try:
                self.loading_dialog.reject()
                self.loading_dialog.close()
            except Exception:
                pass
            self.loading_dialog = None
        self.log.appendPlainText(f'AVISO: Falha ao carregar/atualizar arquivo: {error_msg}. A geometria anterior foi mantida.')
        self.statusBar().showMessage('Erro de leitura no arquivo; modelo anterior conservado intacto.')

    def import_finished(self):
        old = self.worker
        self.worker = None
        if old:
            old.deleteLater()
        self.next_import()

    def add_result(self, record, parts, warnings):
        try:
            if not parts:
                raise ValueError('Nenhuma geometria visualizável encontrada no arquivo.')
            if self.loading_dialog:
                self.loading_dialog.set_progress(98.0, f'Loading {Path(record["path"]).name} ...')
            self._add_result(record, parts, warnings)
            if self.loading_dialog:
                self.loading_dialog.set_progress(100.0, f'Loading {Path(record["path"]).name} ...')
        except Exception as error:
            self.on_import_failed(str(error))
        finally:
            self.tree.blockSignals(False)

    def reset_camera_navisworks(self):
        """Frame model in realistic 3D perspective with strict Z-Up upright horizon."""
        bounds = self.plot.bounds
        if not bounds or bounds[0] > bounds[1]:
            self.plot.reset_camera()
            return
        cx = (bounds[0] + bounds[1]) / 2.0
        cy = (bounds[2] + bounds[3]) / 2.0
        cz = (bounds[4] + bounds[5]) / 2.0
        dx = bounds[1] - bounds[0]
        dy = bounds[3] - bounds[2]
        dz = bounds[5] - bounds[4]
        diag = max(math.sqrt(dx*dx + dy*dy + dz*dz), 1.0)
        camera, renderer = self.navigation.camera()
        if camera:
            camera.ParallelProjectionOff()
            camera.SetViewAngle(50.0)
            camera.SetViewUp(0.0, 0.0, 1.0)
            camera.SetFocalPoint(cx, cy, cz)
            camera.SetPosition(
                cx - 0.85 * diag,
                cy - 0.85 * diag,
                cz + 0.55 * diag
            )
            if renderer:
                self.navigation.update_clipping_range(camera, renderer, full_reset=True)
        self.plot.render()

    def _add_result(self, record, parts, warnings):
        previous = record.pop('_replace', None)
        index = self.models.index(previous) if previous is not None else len(self.models)

        record = dict(record, actors=[])
        record['import_warnings'] = list(warnings)
        record['signature'] = (os.stat(record['path']).st_mtime_ns, os.stat(record['path']).st_size) if os.path.exists(record['path']) else None
        record['mtime'] = os.path.getmtime(record['path']) if os.path.exists(record['path']) else 0

        # Tree Node
        root = W.QTreeWidgetItem([Path(record['path']).name])
        root.setToolTip(0, record['path'])
        root.setFlags(root.flags() | QtCore.Qt.ItemIsUserCheckable)
        self.tree.blockSignals(True)
        root.setCheckState(0, QtCore.Qt.Checked if record['visible'] else QtCore.Qt.Unchecked)

        # Register layers in LayerManager
        for name, mesh in parts:
            layer = getattr(mesh, '_plantview_layer', None)
            if not layer:
                layer_data = mesh.field_data.get('plantview_layer')
                layer = str(layer_data[0]) if layer_data is not None and len(layer_data) > 0 else '0'
            color_data = mesh.field_data.get('plantview_rgb')
            rgb = tuple(int(x) for x in color_data[0]) if color_data is not None else None
            self.layer_manager.register(layer, rgb)

        # GPU Instancing and buffer sharing
        actors, names, stats = add_optimized_parts(self.plot, parts, visible=record['visible'])
        record['actors'].extend(actors)
        record['triangle_count'] = stats.get('total_triangles', sum(getattr(m, 'n_cells', 1) for _, m in parts))

        # Populate hierarchy tree with checkable items
        for actor, name in zip(actors, names):
            child = W.QTreeWidgetItem([name])
            child.setFlags(child.flags() | QtCore.Qt.ItemIsUserCheckable)
            child.setCheckState(0, QtCore.Qt.Checked if record['visible'] else QtCore.Qt.Unchecked)
            child.setData(0, QtCore.Qt.UserRole, actor)
            root.addChild(child)
            subnames = getattr(actor, '_plantview_names', [])
            if len(subnames) <= 150:
                for component_name in subnames:
                    component = W.QTreeWidgetItem([component_name])
                    component.setFlags(component.flags() | QtCore.Qt.ItemIsUserCheckable)
                    component.setCheckState(0, QtCore.Qt.Checked if record['visible'] else QtCore.Qt.Unchecked)
                    component.setData(0, QtCore.Qt.UserRole, actor)
                    child.addChild(component)
            elif subnames:
                summary_child = W.QTreeWidgetItem([f'{len(subnames)} componentes agrupados'])
                summary_child.setFlags(summary_child.flags() & ~QtCore.Qt.ItemIsSelectable)
                child.addChild(summary_child)

        self.log.appendPlainText(
            f"GPU: {stats['original_parts']} peças processadas -> {stats['gpu_actors']} atores GPU, "
            f"{stats['instanced_parts']} instâncias em {stats['instance_groups']} grupos VRAM compartilhados."
        )

        record['item'] = root
        if previous is not None:
            # Replace previous without flickering
            for actor in previous['actors']:
                self.plot.remove_actor(actor)
            self.tree.takeTopLevelItem(index)
            self.models[index] = record
            self.tree.insertTopLevelItem(index, root)
        else:
            self.models.append(record)
            self.tree.addTopLevelItem(root)

        self.tree.blockSignals(False)
        self.total_triangles = sum(m.get('triangle_count', 0) for m in self.models)
        self.measure_tool.surface_picker.invalidate()
        self.section_tool.refresh_models()
        self.plot.remove_actor('welcome')

        if previous is None and record['visible'] and len(self.models) == 1:
            self.reset_camera_navisworks()

        self.dirty = True
        self.log.appendPlainText(f'Carregado: {len(parts)} peça(s) com cores preservadas.')
        for w in warnings[:50]:
            self.log.appendPlainText('AVISO: ' + w)

        self.plot.render()

    def refresh_models(self):
        """F5: Smart refresh. Checks mtime, reloads modified models, preserves camera."""
        if not self.models:
            return
        self.restore_camera = [list(v) for v in self.plot.camera_position]
        self.measure_points = []
        self.plot.remove_actor('measure')

        # Check for files modified on disk
        to_reload = []
        for m in self.models:
            path = m['path']
            try:
                stat = os.stat(path)
                signature = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                self.log.appendPlainText(f'Arquivo indisponível, modelo preservado: {path}')
                continue
            if signature == m.get('signature'):
                continue
            to_reload.append(dict(
                path=m['path'],
                visible=m['visible'],
                scale=m.get('scale', 0.001),
                force_reload=True,
                _replace=m
            ))

        self.log.appendPlainText('\n[F5 REFRESH] Verificando e recarregando modelos da maquete...')
        if to_reload:
            self.enqueue(to_reload)
        else:
            self.restore_camera = None
            self.statusBar().showMessage('Nenhum arquivo alterado.')

    def selected_actors(self):
        actors = {}
        for item in self.tree.selectedItems():
            actor = item.data(0, QtCore.Qt.UserRole)
            if isinstance(actor, vtk.vtkActor):
                actors[id(actor)] = actor
            else:
                model = next((m for m in self.models if m['item'] is item), None)
                if model:
                    actors.update({id(a):a for a in model['actors']})
        return list(actors.values())

    def selection_changed(self):
        for actor in getattr(self, '_highlighted', []):
            actor.GetProperty().SetEdgeVisibility(False)
        self._highlighted = self.selected_actors()
        self.selected_actor = self._highlighted[0] if self._highlighted else None
        for actor in self._highlighted:
            actor.GetProperty().SetEdgeVisibility(True)
            actor.GetProperty().SetEdgeColor(1.0, .84, 0.0)
            actor.GetProperty().SetLineWidth(2.5)
        self.update_properties()
        self.plot.render()

    def visibility_changed(self, item, column):
        """Toggles model or group visibility from the selection tree."""
        # Top-level model
        for model in self.models:
            if model['item'] is item:
                model['visible'] = (item.checkState(0) == QtCore.Qt.Checked)
                for actor in model['actors']:
                    actor.SetVisibility(model['visible'])
                self.dirty = True
                self.plot.render()
                return

        # Child node (actor or component)
        actor = item.data(0, QtCore.Qt.UserRole)
        if actor and isinstance(actor, vtk.vtkActor):
            visible = (item.checkState(0) == QtCore.Qt.Checked)
            actor.SetVisibility(visible)
            self.dirty = True
            self.plot.render()

    def sync_visibility_tree(self):
        self.tree.blockSignals(True)
        for model in self.models:
            visible = any(a.GetVisibility() for a in model['actors'])
            model['visible'] = visible
            model['item'].setCheckState(0, QtCore.Qt.Checked if visible else QtCore.Qt.Unchecked)
        iterator = W.QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value(); actor = item.data(0, QtCore.Qt.UserRole)
            if isinstance(actor, vtk.vtkActor):
                item.setCheckState(0, QtCore.Qt.Checked if actor.GetVisibility() else QtCore.Qt.Unchecked)
            iterator += 1
        self.tree.blockSignals(False)
        self.dirty = True
        self.update_properties()
        self.plot.render()

    def isolate(self):
        chosen = {id(a) for a in self.selected_actors()}
        if not chosen:
            return
        for model in self.models:
            for actor in model['actors']:
                actor.SetVisibility(id(actor) in chosen)
        self.sync_visibility_tree()

    def toggle_hide_selected(self):
        actors = self.selected_actors()
        if not actors and self.selected_actor:
            actors = [self.selected_actor]
        visible = not any(a.GetVisibility() for a in actors)
        for actor in actors:
            actor.SetVisibility(visible)
        self.sync_visibility_tree()

    def hide_selected(self):
        """Compatibilidade: chama toggle_hide_selected."""
        self.toggle_hide_selected()

    def show_all(self):
        for model in self.models:
            for actor in model['actors']:
                actor.SetVisibility(True)
        self.sync_visibility_tree()

    def on_shift_pick(self, x, y):
        """Shift + Left Click: Navisworks hierarchy drill-down selection."""
        renderer = self.plot.renderer
        if not renderer:
            return
        hit = self.measure_tool.surface_picker.pick_hit(x, y, snap=False)
        if hit is None:
            # Line and point primitives have no surface for the measurement picker.
            picker = vtk.vtkPropPicker()
            if not picker.Pick(x, y, 0, renderer):
                return
            picked_actor = picker.GetActor()
            if picked_actor is None or not self.section_tool.contains_point(picker.GetPickPosition()):
                return
        else:
            picked_actor = hit.actor

        matching_items = []
        for i in range(self.tree.topLevelItemCount()):
            root_item = self.tree.topLevelItem(i)
            model_record = next((m for m in self.models if m['item'] is root_item), None)
            if not model_record or picked_actor not in model_record['actors']:
                continue

            matching_items.append(root_item)
            for c in range(root_item.childCount()):
                child = root_item.child(c)
                if child.data(0, QtCore.Qt.UserRole) == picked_actor:
                    matching_items.append(child)
                    for gc in range(child.childCount()):
                        gchild = child.child(gc)
                        matching_items.append(gchild)

        if not matching_items:
            self._highlight_actor(picked_actor)
            return

        # Drill-down cíclico estilo Navisworks
        if self.last_picked_actor == picked_actor:
            self.drill_down_idx = (self.drill_down_idx + 1) % len(matching_items)
        else:
            self.last_picked_actor = picked_actor
            self.drill_down_idx = 1 if len(matching_items) > 1 else 0

        target_item = matching_items[self.drill_down_idx]

        # Expande ancestrais e seleciona
        p = target_item.parent()
        while p:
            p.setExpanded(True)
            p = p.parent()

        self.tree.setCurrentItem(target_item)
        self.tree.scrollToItem(target_item)
        target_item.setSelected(True)

        self._highlight_actor(picked_actor)
        self.statusBar().showMessage(
            f'Elemento selecionado: {target_item.text(0)} | Use Ctrl+H para alternar ocultar/exibir'
        )

    def _highlight_actor(self, actor):
        if self.selected_actor and self.selected_actor != actor:
            self.selected_actor.GetProperty().SetEdgeVisibility(False)
        self.selected_actor = actor
        self.update_properties()
        actor.GetProperty().SetEdgeVisibility(True)
        actor.GetProperty().SetEdgeColor(1.0, 0.84, 0.0)  # Navisworks Gold
        actor.GetProperty().SetLineWidth(2.5)
        self.plot.render()

    def select_same_layer(self):
        if not self.selected_actor:
            return
        layer = getattr(self.selected_actor, '_plantview_layer', '0')
        self.tree.blockSignals(True)
        self.tree.clearSelection()
        iterator = W.QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value(); actor = item.data(0, QtCore.Qt.UserRole)
            if isinstance(actor, vtk.vtkActor) and getattr(actor, '_plantview_layer', '0') == layer:
                item.setSelected(True)
            iterator += 1
        self.tree.blockSignals(False)
        self.selection_changed()

    def update_properties(self):
        if not hasattr(self, 'properties'):
            return
        self.properties.clear()
        actor = self.selected_actor
        general = W.QTreeWidgetItem(self.properties, ['Geral'])
        if actor is None:
            W.QTreeWidgetItem(general, ['Seleção', 'Nenhum elemento'])
        else:
            model = next((m for m in self.models if actor in m['actors']), None)
            fields = [('Nome', getattr(actor, '_plantview_name', 'Componente')),
                      ('Camada', getattr(actor, '_plantview_layer', '0')),
                      ('Arquivo', Path(model['path']).name if model else ''),
                      ('Unidades', 'm'), ('Visível', 'Sim' if actor.GetVisibility() else 'Não')]
            for key, value in fields:
                W.QTreeWidgetItem(general, [key, str(value)])
            bounds = getattr(actor, '_plantview_bounds', None)
            if bounds is None:
                bounds = actor.GetBounds()
            if bounds:
                pos = W.QTreeWidgetItem(self.properties, ['Posição (global)'])
                dim = W.QTreeWidgetItem(self.properties, ['Dimensões'])
                for i, axis in enumerate('XYZ'):
                    W.QTreeWidgetItem(pos, [axis, f'{(bounds[2*i]+bounds[2*i+1])/2:.4f} m'])
                    W.QTreeWidgetItem(dim, [axis, f'{bounds[2*i+1]-bounds[2*i]:.4f} m'])
            material = W.QTreeWidgetItem(self.properties, ['Material'])
            W.QTreeWidgetItem(material, ['Cor RGB', str(tuple(round(c*255) for c in actor.GetProperty().GetColor()))])
        for i in range(self.properties.topLevelItemCount()):
            section = self.properties.topLevelItem(i)
            section.setFirstColumnSpanned(True)
            section.setBackground(0, QtGui.QColor('#d7dfeb'))
            section.setBackground(1, QtGui.QColor('#d7dfeb'))
            font = section.font(0); font.setBold(True); section.setFont(0,font)
        general.setExpanded(True)

    def show_properties(self):
        self.update_properties()
        self.properties_dock.show()
        self.properties_dock.raise_()

    def save_as(self):
        previous = self.project_path
        self.project_path = None
        self.save()
        if self.project_path is None:
            self.project_path = previous

    def enable_selection(self):
        self.selection_mode = self.selection_button.isChecked()
        self.navigation.selection_mode = self.selection_mode

    def select_all_items(self):
        self.tree.selectAll()

    def invert_selection(self):
        iterator = W.QTreeWidgetItemIterator(self.tree)
        self.tree.blockSignals(True)
        while iterator.value():
            item = iterator.value()
            item.setSelected(not item.isSelected())
            iterator += 1
        self.tree.blockSignals(False)
        self.selection_changed()
        self.update_properties()

    def toggle_cube(self):
        self.cube.setVisible(not self.cube.isVisible())

    def toggle_hud(self):
        self.coord_overlay.setVisible(not self.coord_overlay.isVisible())

    def toggle_activity(self):
        self.log_dock.setVisible(not self.log_dock.isVisible())

    def export_view(self):
        path, _ = W.QFileDialog.getSaveFileName(self, 'Salvar captura 3D', 'NavModel.png', 'Imagem PNG (*.png)')
        if path:
            self.plot.screenshot(path)

    def publish_web(self):
        if self.worker is not None or self.queue:
            W.QMessageBox.information(self, 'Publicar Maquete Web', 'Aguarde o carregamento dos modelos antes de publicar.')
            return
        from .web_publish import show_publish_dialog
        show_publish_dialog(self)

    def show_layer_manager(self):
        """Opens the Appearance Profiler & Layer Manager dialog."""
        dlg = NavisLayerManagerDialog(self.layer_manager, self)
        dlg.color_changed.connect(self.on_layer_color_changed)
        dlg.visibility_changed.connect(self.on_layer_visibility_changed)
        dlg.exec()

    def on_layer_color_changed(self, layer_name: str, rgb: tuple):
        """Applies real-time color update to all actors in the modified layer."""
        float_color = (rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
        for m in self.models:
            for act in m['actors']:
                if getattr(act, '_plantview_layer', '') == layer_name:
                    act.GetProperty().SetColor(*float_color)
        self.plot.render()
        self.dirty = True

    def on_layer_visibility_changed(self, layer_name: str, visible: bool):
        """Applies visibility toggle to all actors in the layer."""
        for m in self.models:
            for act in m['actors']:
                if getattr(act, '_plantview_layer', '') == layer_name:
                    act.SetVisibility(visible and m['visible'])
        self.plot.render()

    def toggle_selection_tree(self):
        self.tree_dock.setVisible(not self.tree_dock.isVisible())

    def toggle_measure(self):
        self.measure_mode = not self.measure_mode
        self.measure_tool.start = None
        if not self.measure_mode:
            self.measure_tool.deactivate()
            self.plot.interactor.unsetCursor()
        self.statusBar().showMessage('Medir: clique no primeiro ponto, mova o cursor e clique no segundo. Esc cancela.' if self.measure_mode else 'Medição desativada.')

    def clear_measures(self):
        self.measure_tool.clear()

    def toggle_diagnostic_render_mode(self):
        """Alterna entre Renderização Otimizada GPU (60 FPS) e Diagnóstico Isolado (1:1 peças)."""
        current = getattr(self.plot, '_no_batch_mode', False)
        new_mode = not current
        self.plot._no_batch_mode = new_mode
        mode_str = 'DIAGNÓSTICO ISOLADO (1:1 sem batching)' if new_mode else 'OTIMIZADO GPU (60 FPS)'
        self.log.appendPlainText(f'\n[GPU DIAGNÓSTICO] Modo alternado para: {mode_str}.')
        self.statusBar().showMessage(f'Modo GPU: {mode_str}')
        self.refresh_models()

    def show_gpu_info(self):
        self.plot.render()
        info = gpu_report(self.plot)
        self.log.appendPlainText('\n' + info)
        W.QMessageBox.information(self, 'NavModel Manager — Diagnóstico GPU', info)

    def update_project_title(self):
        text = (Path(self.project_path).name if self.project_path else 'Novo modelo') + ' — NavModel Manager'
        self.setWindowTitle(text)
        if hasattr(self.ribbon, 'project_title'):
            self.ribbon.project_title.setText(text)

    def save(self):
        path, _ = W.QFileDialog.getSaveFileName(self, 'Salvar projeto federado', self.project_path or 'Maquete.pvproj', 'Projeto NavModel (*.pvproj)')
        if not path:
            return False
        if not path.lower().endswith('.pvproj'):
            path += '.pvproj'
        try:
            save_project(path, self.models, [list(v) for v in self.plot.camera_position])
            self.project_path = path
            self.update_project_title()
            self.dirty = False
            self.log.appendPlainText(f'Projeto federado salvo com sucesso em: {path}')
            return True
        except Exception as e:
            W.QMessageBox.warning(self, 'Falha ao salvar projeto', str(e))
            return False

    def open_project(self):
        path, _ = W.QFileDialog.getOpenFileName(self, 'Abrir projeto federado', '', 'Projeto NavModel (*.pvproj)')
        if not path:
            return
        try:
            data = load_project(path)
        except Exception as e:
            W.QMessageBox.warning(self, 'Projeto inválido', str(e))
            return
        if not self.confirm_discard():
            return
        self.clear_models()
        self.project_path = path
        self.update_project_title()
        self.restore_camera = data.get('camera')
        self.enqueue(data['models'])

    def confirm_discard(self):
        if not self.dirty:
            return True
        ans = W.QMessageBox.question(self, 'Projeto modificado', 'Salvar o projeto antes de continuar?', W.QMessageBox.Save | W.QMessageBox.Discard | W.QMessageBox.Cancel)
        if ans == W.QMessageBox.Save:
            return self.save()
        return ans == W.QMessageBox.Discard

    def clear_models(self):
        self.measure_mode = False
        self.measure_tool.deactivate()
        self.measure_tool.clear()
        self.section_tool.clear()
        for m in self.models:
            for actor in m['actors']:
                self.plot.remove_actor(actor)
        self.plot.remove_actor('measure')
        self.selected_actor = None
        self.last_picked_actor = None
        self.models.clear()
        self.measure_tool.surface_picker.clear()
        self.tree.clear()
        self.update_properties()
        self.total_triangles = 0
        self.project_path = None
        self.update_project_title()
        self.dirty = False

    def new_project(self):
        if self.confirm_discard():
            self.clear_models()

    def closeEvent(self, event):
        if self.worker:
            W.QMessageBox.information(self, 'Importação em andamento', 'Aguarde o término da importação para fechar.')
            event.ignore()
            return
        if not self.confirm_discard():
            event.ignore()
            return
        self.measure_tool.deactivate()
        self.measure_tool.surface_picker.clear()
        self.section_tool.close()
        self.plot.close()
        event.accept()


def main():
    configure_surface()
    app = W.QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setFont(QtGui.QFont('Segoe UI',10))
    palette = QtGui.QPalette()
    for role, color in [(QtGui.QPalette.Window,'#f3f6f9'),(QtGui.QPalette.WindowText,'#0b2f50'),
                        (QtGui.QPalette.Base,'white'),(QtGui.QPalette.AlternateBase,'#f3f6f9'),
                        (QtGui.QPalette.Text,'#0b2f50'),(QtGui.QPalette.Button,'#f3f6f9'),
                        (QtGui.QPalette.ButtonText,'#0b2f50'),(QtGui.QPalette.Highlight,'#0788ff'),
                        (QtGui.QPalette.HighlightedText,'white')]:
        palette.setColor(role,QtGui.QColor(color))
    app.setPalette(palette)
    app.setApplicationName('NavModel Manager')
    app.setWindowIcon(app_icon())

    app.setApplicationVersion(VERSION)
    app.setOrganizationName('Carlos Morais')
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('CarlosMorais.NavModelManager')
    splash = StartupSplash()
    splash.show()
    app.processEvents()
    window = Window()
    def reveal():
        # Precise timer + elapsed guard ensure a MINIMUM of five visible seconds.
        if splash.clock.elapsed() < splash.minimum_ms:
            QtCore.QTimer.singleShot(splash.minimum_ms - splash.clock.elapsed() + 5, reveal)
            return
        window.startup_visible_ms = splash.clock.elapsed()
        splash.timer.stop()
        splash.close()
        window.show()
        if '--smoke-test' in sys.argv:
            QtCore.QTimer.singleShot(600, check)
        elif len(sys.argv) > 1 and Path(sys.argv[1]).suffix.lower() == '.pvproj':
            data = load_project(sys.argv[1])
            window.project_path = sys.argv[1]
            window.restore_camera = data.get('camera')
            window.enqueue(data['models'])
    QtCore.QTimer.singleShot(max(0, splash.minimum_ms-splash.clock.elapsed())+5, reveal)

    if '--smoke-test' in sys.argv:
        import json
        output = Path(sys.argv[sys.argv.index('--smoke-test') + 1]).resolve()
        output.mkdir(parents=True, exist_ok=True)
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
        report = {'models': [], 'errors': []}

        def check():
            for name in ('equipamentos.dwg', 'estrutura.step', 'parede.ifc'):
                try:
                    path = root / 'examples' / name
                    if path.is_file():
                        parts, warnings = read_model(path)
                        window.add_result({'path': str(path), 'visible': True, 'scale': 0.001}, parts, warnings)
                        report['models'].append({
                            'file': name,
                            'parts': len(parts),
                            'bounds': [list(m.bounds) for _, m in parts],
                            'warnings': warnings
                        })
                except Exception as e:
                    report['errors'].append(f'{name}: {e}')
            window.tree.expandToDepth(0)
            if window.models and window.models[0]['item'].childCount():
                window.models[0]['item'].child(0).setSelected(True)
            window.plot.view_isometric()
            window.plot.reset_camera()
            if len(window.models) == 3:
                try:
                    from .web_publish import export_scene
                    window.section_tool.set_mode('box')
                    assert len(window.section_tool.clipping_equations()) == 6
                    window.section_tool.set_enabled(False)
                    report['web'] = {}
                    for kind in ('html', 'exe'):
                        result = export_scene(window.models, output / f'maquete.{kind}', kind=kind)
                        report['web'][kind] = {'bytes': result['bytes'], 'triangles': result['analysis']['triangles']}
                except Exception as e:
                    report['errors'].append(f'Publicação Web: {e}')
            QtCore.QTimer.singleShot(1500, finish)

        def finish():
            window.screen().grabWindow(int(window.winId())).save(str(output / 'NavModelManager.png'))
            report['splash_visible_ms'] = window.startup_visible_ms
            report['gpu'] = gpu_report(window.plot)
            report['version'] = VERSION
            window.plot.screenshot(str(output / 'viewport.png'))
            (output / 'smoke-test.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
            window.dirty = False
            window.close()
            import os
            os._exit(1 if report['errors'] else 0)

        QtCore.QTimer.singleShot(150, lambda: splash.grab().save(str(output / "Splash.png")))

    return app.exec()
