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
from .loading_dialog import NavisLoadingDialog
from .widgets import NavisCoordinateOverlay, NavisLayerManagerDialog, setup_nav_cube, NavisViewControlOverlay


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
        self.setWindowTitle('PlantView Studio 2026 — Maquete Federada 3D')
        self.setWindowIcon(create_navis_icon('app_logo', 64))
        self.resize(1380, 880)

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
            QMainWindow, QWidget { background: #f3f6fa; color: #26364a; font-family: 'Segoe UI'; font-size: 12px; }
            QTreeWidget, QPlainTextEdit { background: white; border: 1px solid #e0e7ef; border-radius: 8px; padding: 6px; selection-background-color: #deedff; selection-color: #174f94; }
            QTreeWidget::item { padding: 5px 3px; }
            QTreeWidget::item:selected { background: #deedff; color: #174f94; border-radius: 4px; }
            QPlainTextEdit { font-family: 'Consolas'; font-size: 11px; }
            QHeaderView::section { background: #edf2f8; border: none; padding: 9px; font-weight: 600; }
            QDockWidget { font-weight: 600; }
            QDockWidget::title { background: #e8eef6; padding: 10px 12px; border-radius: 5px; }
            QStatusBar { background: #152b46; color: #e1ecfa; padding: 5px; font-size: 11px; }
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
        vp_layout = W.QStackedLayout(viewport_container)
        vp_layout.setStackingMode(W.QStackedLayout.StackAll)

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

        # Floating View and Projection Controls Overlay (under NavCube)
        self.view_overlay = NavisViewControlOverlay(self, viewport_container)
        vp_layout.addWidget(self.view_overlay)

        # Floating Navisworks Coordinate Overlay in bottom-left
        self.coord_overlay = NavisCoordinateOverlay(viewport_container)
        vp_layout.addWidget(self.coord_overlay)

        # 1. Navisworks Ribbon Bar (Created after plot is ready)
        self.ribbon = NavisRibbonBar(self)
        layout.addWidget(self.ribbon)
        layout.addWidget(viewport_container)
        self.setCentralWidget(central)

        # Surface point picking for measurements
        self.plot.enable_surface_point_picking(
            callback=self.point_picked,
            show_message=False,
            show_point=False,
            left_clicking=False
        )

        # Picking installs another style; restore our navigation after its setup.
        self.plot.interactor.SetInteractorStyle(self.navigation)

        # Welcome text in 3D scene
        self.plot.add_text(
            'PlantView Studio 2026\nAdicione modelos industriais (DWG • STEP • IFC • DXF)',
            name='welcome',
            font_size=13,
            color='#2a4460'
        )

        # Dock 1: Selection Tree (Left Dock)
        self.tree = W.QTreeWidget()
        self.tree.setHeaderLabels(['Hierarquia de Componentes'])
        self.tree.itemChanged.connect(self.visibility_changed)
        self.tree.itemSelectionChanged.connect(self.selection_changed)
        self.tree_dock = W.QDockWidget('Modelos e componentes', self)
        self.tree_dock.setWidget(self.tree)
        self.tree_dock.setMinimumWidth(300)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.tree_dock)

        # Dock 2: Log and Measurement Report (Bottom Dock)
        self.log = W.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log_dock = W.QDockWidget('Atividade e medições', self)
        self.log_dock.setWidget(self.log)
        self.log_dock.setMaximumHeight(160)
        self.addDockWidget(QtCore.Qt.BottomDockWidgetArea, self.log_dock)

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
        self.log.appendPlainText('PlantView Studio 2026 v0.0.6 iniciado com renderização OpenGL acelerada por GPU.')

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
        self.total_triangles += stats.get('total_triangles', sum(getattr(m, 'n_cells', 1) for _, m in parts))

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
            to_reload.append(dict(
                path=m['path'],
                visible=m['visible'],
                scale=m.get('scale', 0.001),
                force_reload=True,
                _replace=m
            ))

        self.log.appendPlainText('\n[F5 REFRESH] Verificando e recarregando modelos da maquete...')
        self.enqueue(to_reload)

    def selection_changed(self):
        """Highlights the selected actor in the 3D scene."""
        selected_items = self.tree.selectedItems()
        if not selected_items:
            if self.selected_actor:
                self.selected_actor.GetProperty().SetEdgeVisibility(False)
                self.selected_actor = None
                self.plot.render()
            return

        item = selected_items[0]
        actor = item.data(0, QtCore.Qt.UserRole)
        if actor and isinstance(actor, vtk.vtkActor):
            if self.selected_actor and self.selected_actor != actor:
                self.selected_actor.GetProperty().SetEdgeVisibility(False)
            self.selected_actor = actor
            actor.GetProperty().SetEdgeVisibility(True)
            actor.GetProperty().SetEdgeColor(1.0, 0.84, 0.0)  # Navisworks Gold highlight
            actor.GetProperty().SetLineWidth(2.5)
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

    def isolate(self):
        """Isolate: Hides everything except selected item."""
        selected_items = self.tree.selectedItems()
        if not selected_items:
            return
        target = selected_items[0]
        for m in self.models:
            is_match = (m['item'] is target)
            m['item'].setCheckState(0, QtCore.Qt.Checked if is_match else QtCore.Qt.Unchecked)

    def toggle_hide_selected(self):
        """Alterna a visibilidade (oculta/exibe) do modelo ou componente selecionado (Ctrl+H)."""
        selected_items = self.tree.selectedItems()
        if selected_items:
            target = selected_items[0]
            # Caso seja o modelo raiz (arquivo federado)
            for m in self.models:
                if m['item'] is target:
                    new_vis = not m['visible']
                    m['visible'] = new_vis
                    target.setCheckState(0, QtCore.Qt.Checked if new_vis else QtCore.Qt.Unchecked)
                    for actor in m['actors']:
                        actor.SetVisibility(new_vis)
                    self.dirty = True
                    self.plot.render()
                    action = 'reexibido' if new_vis else 'ocultado'
                    self.statusBar().showMessage(f'Modelo {action}: {target.text(0)} (Ctrl+H para alternar)')
                    return

            # Caso seja um nó filho (ator individual ou componente)
            actor = target.data(0, QtCore.Qt.UserRole)
            current_state = target.checkState(0)
            new_state = QtCore.Qt.Checked if current_state == QtCore.Qt.Unchecked else QtCore.Qt.Unchecked
            target.setCheckState(0, new_state)
            is_vis = (new_state == QtCore.Qt.Checked)
            if actor and isinstance(actor, vtk.vtkActor):
                actor.SetVisibility(is_vis)
                self.dirty = True
                self.plot.render()
                action = 'reexibido' if is_vis else 'ocultado'
                self.statusBar().showMessage(f'Elemento {action}: {target.text(0)} (Ctrl+H para alternar)')
                return

        # Se houver selected_actor no viewport diretamente
        if self.selected_actor and isinstance(self.selected_actor, vtk.vtkActor):
            is_vis = not self.selected_actor.GetVisibility()
            self.selected_actor.SetVisibility(is_vis)
            self.dirty = True
            self.plot.render()
            action = 'reexibido' if is_vis else 'ocultado'
            self.statusBar().showMessage(f'Elemento {action} (Ctrl+H para alternar)')

    def hide_selected(self):
        """Compatibilidade: chama toggle_hide_selected."""
        self.toggle_hide_selected()

    def show_all(self):
        """Unhide All: Shows all models and components."""
        for m in self.models:
            m['item'].setCheckState(0, QtCore.Qt.Checked)
            for actor in m['actors']:
                actor.SetVisibility(True)
        self.plot.render()
        self.statusBar().showMessage('Todos os modelos e componentes foram reexibidos.')

    def on_shift_pick(self, x, y):
        """Shift + Left Click: Navisworks hierarchy drill-down selection."""
        renderer = self.plot.renderer
        if not renderer:
            return
        picker = vtk.vtkPropPicker()
        if not picker.Pick(x, y, 0, renderer):
            return
        picked_actor = picker.GetActor()
        if not picked_actor:
            return

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
        actor.GetProperty().SetEdgeVisibility(True)
        actor.GetProperty().SetEdgeColor(1.0, 0.84, 0.0)  # Navisworks Gold
        actor.GetProperty().SetLineWidth(2.5)
        self.plot.render()

    def select_same_layer(self):
        """Selects all items belonging to the same layer as the active item."""
        if not self.selected_actor or not hasattr(self.selected_actor, '_plantview_layer'):
            return
        target_layer = self.selected_actor._plantview_layer
        count = 0
        for m in self.models:
            for act in m['actors']:
                if hasattr(act, '_plantview_layer') and act._plantview_layer == target_layer:
                    act.GetProperty().SetEdgeVisibility(True)
                    act.GetProperty().SetEdgeColor(1.0, 0.84, 0.0)
                    count += 1
        self.plot.render()
        self.log.appendPlainText(f'Selecionados {count} itens da camada "{target_layer}".')

    def show_properties(self):
        """Displays properties sheet for selected item."""
        if not self.selected_actor:
            W.QMessageBox.information(self, 'Propriedades', 'Nenhum elemento selecionado na árvore ou viewport.')
            return
        name = getattr(self.selected_actor, '_plantview_name', 'Desconhecido')
        layer = getattr(self.selected_actor, '_plantview_layer', '0')
        bounds = getattr(self.selected_actor, '_plantview_bounds', None)
        pos = self.selected_actor.GetPosition()

        info = [
            f'Nome do Elemento: {name}',
            f'Camada (Layer): {layer}',
            f'Posição X, Y, Z: {pos[0]:.4f} m, {pos[1]:.4f} m, {pos[2]:.4f} m',
        ]
        if bounds:
            dx = bounds[1] - bounds[0]
            dy = bounds[3] - bounds[2]
            dz = bounds[5] - bounds[4]
            info.append(f'Dimensões: Largura={dx:.4f} m, Comprimento={dy:.4f} m, Altura={dz:.4f} m')
        W.QMessageBox.information(self, 'Propriedades do Elemento', '\n'.join(info))

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
        self.measure_points = []
        if self.measure_mode:
            self.statusBar().showMessage('MODO MEDIR ATIVO: Clique com botão direito ou pressione P em dois pontos da maquete.')
            self.log.appendPlainText('MEDIR: Clique ou pressione P sobre duas superfícies para calcular a distância exata em metros.')
        else:
            self.statusBar().showMessage('Medição desativada.')

    def clear_measures(self):
        self.plot.remove_actor('measure')
        self.measure_points.clear()
        self.plot.render()

    def point_picked(self, point):
        if not self.measure_mode:
            return
        self.measure_points.append(np.array(point))
        if len(self.measure_points) == 2:
            a, b = self.measure_points
            dist = np.linalg.norm(a - b)
            self.plot.add_mesh(pv.Line(a, b), color='#ffcc00', line_width=4, name='measure', pickable=False, reset_camera=False)
            self.log.appendPlainText(f'MEDIDA: Distância = {dist:.4f} m ({dist * 1000:.1f} mm) | A={a.round(4)} | B={b.round(4)}')
            self.measure_points = []


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
        W.QMessageBox.information(self, 'PlantView Studio — Diagnóstico GPU', info)

    def save(self):
        path, _ = W.QFileDialog.getSaveFileName(self, 'Salvar projeto federado', self.project_path or 'Maquete.pvproj', 'Projeto PlantView (*.pvproj)')
        if not path:
            return False
        if not path.lower().endswith('.pvproj'):
            path += '.pvproj'
        try:
            save_project(path, self.models, [list(v) for v in self.plot.camera_position])
            self.project_path = path
            self.dirty = False
            self.log.appendPlainText(f'Projeto federado salvo com sucesso em: {path}')
            return True
        except Exception as e:
            W.QMessageBox.warning(self, 'Falha ao salvar projeto', str(e))
            return False

    def open_project(self):
        path, _ = W.QFileDialog.getOpenFileName(self, 'Abrir projeto federado', '', 'Projeto PlantView (*.pvproj)')
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
        for m in self.models:
            for actor in m['actors']:
                self.plot.remove_actor(actor)
        self.plot.remove_actor('measure')
        self.models.clear()
        self.tree.clear()
        self.total_triangles = 0
        self.project_path = None
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
        self.plot.close()
        event.accept()


def main():
    configure_surface()
    app = W.QApplication(sys.argv)
    app.setApplicationName('PlantView Studio')
    app.setWindowIcon(create_navis_icon('app_logo', 64))

    window = Window()
    window.show()

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
            window.plot.view_isometric()
            window.plot.reset_camera()
            QtCore.QTimer.singleShot(1500, finish)

        def finish():
            window.grab().save(str(output / 'PlantViewStudio.png'))
            window.plot.screenshot(str(output / 'viewport.png'))
            (output / 'smoke-test.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
            window.dirty = False
            window.close()
            import os
            os._exit(1 if report['errors'] else 0)

        QtCore.QTimer.singleShot(600, check)

    return app.exec()
