"""Autodesk Navisworks Manage navigation style for VTK.

Controles Navisworks:
- Botão esquerdo arrastar: Olhar ao redor livremente (Look Around em primeira pessoa, gira no próprio eixo).
- Botão do meio (scroll pressionado) arrastar: Pan suave (deslocamento lateral e vertical).
- Shift + Botão do meio (scroll pressionado) arrastar: Ferramenta Orbital (Orbit clássico do Navisworks em torno do ponto de interesse).
- Scroll (rodinha): Avançar / recuar (zoom suave na direção da visão em perspectiva ou escala em paralela).
- Shift + qualquer movimento: Aumenta a velocidade de navegação.
- Shift + Clique esquerdo: Drill-down seleção de elementos na hierarquia da árvore.
- Ctrl + Z: Desfazer movimento / vista anterior da câmera.
- Sem movimentação WASD.
- F5: Atalho para atualizar modelos.
"""
import math
import time
import numpy as np
import vtk
from PySide6 import QtCore, QtWidgets


class NavisworksStyle(vtk.vtkInteractorStyleUser):
    def __init__(self, on_change=None, on_refresh=None):
        super().__init__()
        self.drag_mode = None
        self._active_button = None
        self.previous = None
        self.on_change = on_change
        self.on_refresh = on_refresh
        self.camera_history = []
        self._pre_movement_state = None
        self.on_shift_pick = None
        self.selection_mode = False
        self._click_start = None
        self._move_count = 0
        self.orbit_pivot = None
        self.last_valid_target = None
        self._zoom_pick_xy = None
        self._zoom_pick_time = 0.0
        self._zoom_target_pt = None
        self._prop_picker = vtk.vtkPropPicker()

        # Event observers
        self.AddObserver('LeftButtonPressEvent', self.left_down)
        self.AddObserver('LeftButtonReleaseEvent', self.mouse_up)
        self.AddObserver('MiddleButtonPressEvent', self.middle_down)
        self.AddObserver('MiddleButtonReleaseEvent', self.mouse_up)
        self.AddObserver('RightButtonPressEvent', self.right_down)
        self.AddObserver('RightButtonReleaseEvent', self.mouse_up)
        self.AddObserver('MouseMoveEvent', self.mouse_move)
        self.AddObserver('MouseWheelForwardEvent', lambda o, e: self.dolly(1))
        self.AddObserver('MouseWheelBackwardEvent', lambda o, e: self.dolly(-1))
        self.AddObserver('KeyPressEvent', self.key_press)

    def camera(self):
        interactor = self.GetInteractor()
        if not interactor:
            return None, None
        rw = interactor.GetRenderWindow()
        if not rw:
            return None, None
        renderers = rw.GetRenderers()
        renderer = self.GetCurrentRenderer() or renderers.GetFirstRenderer()
        if not renderer:
            return None, None
        camera = renderer.GetActiveCamera()
        return camera, renderer

    def push_camera_state(self, state):
        if not self.camera_history or self.camera_history[-1] != state:
            self.camera_history.append(state)
            if len(self.camera_history) > 50:
                self.camera_history.pop(0)

    def save_camera_state(self):
        camera, _ = self.camera()
        if not camera:
            return
        state = (
            camera.GetPosition(),
            camera.GetFocalPoint(),
            camera.GetViewUp(),
            camera.GetParallelProjection(),
            camera.GetParallelScale()
        )
        self.push_camera_state(state)

    def _record_pre_movement(self):
        camera, _ = self.camera()
        if not camera:
            return
        self._pre_movement_state = (
            camera.GetPosition(),
            camera.GetFocalPoint(),
            camera.GetViewUp(),
            camera.GetParallelProjection(),
            camera.GetParallelScale()
        )

    def _commit_pre_movement(self):
        if self._pre_movement_state is None:
            return
        camera, _ = self.camera()
        if camera:
            curr = (
                camera.GetPosition(),
                camera.GetFocalPoint(),
                camera.GetViewUp(),
                camera.GetParallelProjection(),
                camera.GetParallelScale()
            )
            diff = any(
                abs(a - b) > 1e-4
                for a, b in zip(self._pre_movement_state[0] + self._pre_movement_state[1],
                                curr[0] + curr[1])
            ) or self._pre_movement_state[3] != curr[3]
            if diff:
                self.push_camera_state(self._pre_movement_state)
        self._pre_movement_state = None

    def undo_camera(self):
        if not self.camera_history:
            return False
        state = self.camera_history.pop()
        pos, focal, up, proj, scale = state
        camera, renderer = self.camera()
        if not camera or not renderer:
            return False
        camera.SetPosition(*pos)
        camera.SetFocalPoint(*focal)
        camera.SetViewUp(*up)
        if proj:
            camera.ParallelProjectionOn()
            camera.SetParallelScale(scale)
        else:
            camera.ParallelProjectionOff()
        self.update_clipping_range(camera, renderer, full_reset=True)
        interactor = self.GetInteractor()
        if interactor:
            interactor.Render()
        if self.on_change:
            self.on_change(camera)
        return True

    def update_clipping_range(self, camera=None, renderer=None, full_reset=False):
        """Maintain a tight near plane (down to 1 mm) and generous far plane so close-up objects never clip."""
        if camera is None or renderer is None:
            camera, renderer = self.camera()
        if not camera or not renderer:
            return
        if full_reset:
            renderer.ResetCameraClippingRange()
        near, far = camera.GetClippingRange()
        eye = np.array(camera.GetPosition(), dtype=float)
        focal = np.array(camera.GetFocalPoint(), dtype=float)
        dist = max(float(np.linalg.norm(focal - eye)), 0.01)
        safe_near = max(min(dist * 0.01, 0.05), 0.001)
        safe_far = max(float(far), dist * 50.0, 2000.0)
        camera.SetClippingRange(safe_near, safe_far)

    def _is_shift_down(self, interactor=None):
        """Reliably detect Shift state from both VTK interactor and Qt global keyboard modifiers."""
        if interactor is None:
            interactor = self.GetInteractor()
        if interactor and interactor.GetShiftKey():
            return True
        try:
            mods = QtWidgets.QApplication.keyboardModifiers()
            if bool(mods & QtCore.Qt.KeyboardModifier.ShiftModifier):
                return True
        except Exception:
            pass
        return False

    def _pick_single_point(self, px, py, renderer, eye, view_dir):
        """Perform a single fast hardware pick at (px, py) and return (pt, depth) if hit."""
        if px < 0 or py < 0:
            return None
        if self._prop_picker.Pick(int(px), int(py), 0, renderer) and self._prop_picker.GetActor() is not None:
            pt = np.array(self._prop_picker.GetPickPosition(), dtype=float)
            depth = float(np.dot(pt - eye, view_dir))
            if depth > 1e-3:
                self.last_valid_target = pt.copy()
                return pt, depth
        return None

    def _scene_reference_target(self, renderer, eye, view_dir, fallback_focal):
        """Return a reliable 3D target point and distance when cursor picks empty background."""
        if self.last_valid_target is not None:
            d = float(np.dot(self.last_valid_target - eye, view_dir))
            if d > 0.02:
                return self.last_valid_target.copy(), d

        now = time.monotonic()
        if (
            getattr(self, '_cached_bounds_time', 0) > 0
            and (now - self._cached_bounds_time) < 4.0
            and getattr(self, '_cached_scene_bounds', None) is not None
        ):
            bounds = self._cached_scene_bounds
        else:
            bounds = renderer.ComputeVisiblePropBounds()
            self._cached_scene_bounds = bounds
            self._cached_bounds_time = now

        if bounds and len(bounds) == 6 and bounds[1] >= bounds[0]:
            center = np.array([
                0.5 * (bounds[0] + bounds[1]),
                0.5 * (bounds[2] + bounds[3]),
                0.5 * (bounds[4] + bounds[5]),
            ], dtype=float)
            diag = float(math.sqrt(
                (bounds[1] - bounds[0]) ** 2 +
                (bounds[3] - bounds[2]) ** 2 +
                (bounds[5] - bounds[4]) ** 2
            ))
            d_center = float(np.dot(center - eye, view_dir))
            if d_center > 0.05:
                return center, d_center
            ref_d = max(diag * 0.20, 1.0)
            return eye + view_dir * ref_d, ref_d

        d_focal = max(float(np.linalg.norm(fallback_focal - eye)), 1.0)
        return eye + view_dir * d_focal, d_focal

    def _pick_target_point(self, x, y, renderer, camera):
        """Pick the 3D surface point under (x, y) or at screen center to anchor orbit."""
        eye = np.array(camera.GetPosition(), dtype=float)
        focal = np.array(camera.GetFocalPoint(), dtype=float)
        view_vec = focal - eye
        view_len = float(np.linalg.norm(view_vec))
        if view_len < 1e-9:
            return None
        view_dir = view_vec / view_len

        hit = self._pick_single_point(int(x), int(y), renderer, eye, view_dir)
        if hit is not None:
            return hit

        interactor = self.GetInteractor()
        if interactor and interactor.GetRenderWindow():
            rw_w, rw_h = interactor.GetRenderWindow().GetSize()
            if rw_w > 0 and rw_h > 0:
                hit = self._pick_single_point(rw_w // 2, rw_h // 2, renderer, eye, view_dir)
                if hit is not None:
                    return hit
        return None

    def _init_orbit_pivot(self, x, y):
        """Lock orbit pivot onto the 3D surface being inspected without any camera jump."""
        camera, renderer = self.camera()
        if not camera or not renderer:
            self.orbit_pivot = None
            return
        eye = np.array(camera.GetPosition(), dtype=float)
        focal = np.array(camera.GetFocalPoint(), dtype=float)
        view_vec = focal - eye
        view_len = float(np.linalg.norm(view_vec))
        view_dir = view_vec / max(view_len, 1e-9)

        hit = self._pick_target_point(x, y, renderer, camera)
        if hit is not None:
            pt, depth = hit
            self.orbit_pivot = pt
            # Align focal point depth with the picked surface along the exact current line of sight (zero jump)
            camera.SetFocalPoint(*(eye + view_dir * max(depth, 0.02)))
        else:
            target_pt, ref_depth = self._scene_reference_target(renderer, eye, view_dir, focal)
            self.orbit_pivot = target_pt
            camera.SetFocalPoint(*(eye + view_dir * max(ref_depth, 0.05)))

    def left_down(self, obj, event):
        interactor = self.GetInteractor()
        if self._is_shift_down(interactor):
            pos = interactor.GetEventPosition()
            if self.on_shift_pick:
                self.on_shift_pick(pos[0], pos[1])
                return
        self._click_start = interactor.GetEventPosition()
        self._active_button = 'left'
        self.previous = interactor.GetEventPosition()
        self.drag_mode = 'look'
        self._record_pre_movement()
        if interactor and interactor.GetRenderWindow():
            interactor.GetRenderWindow().SetDesiredUpdateRate(30.0)

    def middle_down(self, obj, event):
        interactor = self.GetInteractor()
        self._active_button = 'middle'
        self.previous = interactor.GetEventPosition()
        self._record_pre_movement()
        if interactor and interactor.GetRenderWindow():
            interactor.GetRenderWindow().SetCurrentCursor(9)
            interactor.GetRenderWindow().SetDesiredUpdateRate(30.0)
        if self._is_shift_down(interactor):
            self.drag_mode = 'orbit'
            self._init_orbit_pivot(self.previous[0], self.previous[1])
        else:
            self.drag_mode = 'pan'

    def right_down(self, obj, event):
        interactor = self.GetInteractor()
        self._active_button = 'right'
        self.previous = interactor.GetEventPosition()
        self.drag_mode = 'orbit' if self._is_shift_down(interactor) else 'look'
        self._record_pre_movement()
        if self.drag_mode == 'orbit':
            self._init_orbit_pivot(self.previous[0], self.previous[1])
        if interactor and interactor.GetRenderWindow():
            interactor.GetRenderWindow().SetDesiredUpdateRate(30.0)

    def mouse_up(self, obj, event):
        interactor = self.GetInteractor()
        if self.selection_mode and self._active_button == 'left' and self._click_start:
            x, y = interactor.GetEventPosition()
            if abs(x-self._click_start[0]) + abs(y-self._click_start[1]) <= 4 and self.on_shift_pick:
                self.on_shift_pick(x, y)
        self._click_start = None
        self._active_button = None
        self.drag_mode = None
        self.previous = None
        self.orbit_pivot = None
        self._move_count = 0
        self._commit_pre_movement()
        interactor = self.GetInteractor()
        if interactor and interactor.GetRenderWindow():
            interactor.GetRenderWindow().SetCurrentCursor(0)
            interactor.GetRenderWindow().SetDesiredUpdateRate(0.001)
            camera, renderer = self.camera()
            if camera and renderer:
                self.update_clipping_range(camera, renderer, full_reset=True)
            interactor.Render()

    def key_press(self, obj, event):
        interactor = self.GetInteractor()
        key = interactor.GetKeySym()
        if key == 'F5' and self.on_refresh:
            self.on_refresh()

    def mouse_move(self, obj, event):
        if self.drag_mode is None or self.previous is None:
            return
        interactor = self.GetInteractor()
        pos = interactor.GetEventPosition()

        # Allow seamless live transition between Pan and Orbit when Shift is pressed/released mid-drag
        shift_down = self._is_shift_down(interactor)
        if self._active_button == 'middle':
            want_mode = 'orbit' if shift_down else 'pan'
            if want_mode != self.drag_mode:
                self.drag_mode = want_mode
                if want_mode == 'orbit':
                    self._init_orbit_pivot(pos[0], pos[1])

        dx = pos[0] - self.previous[0]
        dy = pos[1] - self.previous[1]
        self.previous = pos
        if dx == 0 and dy == 0:
            return

        camera, renderer = self.camera()
        if not camera or not renderer:
            return

        self._move_count += 1

        # Sensitivity speed multiplier (Shift accelerates look/pan, but not orbit since Shift+Scroll triggers orbit)
        speed = 2.5 if (shift_down and self.drag_mode != 'orbit') else 1.0

        if self.drag_mode == 'look':
            # Autodesk Navisworks First-Person Look Around:
            # - Eye stays strictly locked at its current 3D position (no orbit, no translation)
            # - Left/Right mouse moves rotate Yaw around global Z-up axis [0, 0, 1]
            # - Up/Down mouse moves rotate Pitch around local camera Right axis
            # - Pitch is clamped to prevent flipping or horizon inversion
            # - Horizon never tilts (ViewUp is maintained upright)
            eye = np.array(camera.GetPosition(), dtype=float)
            focal = np.array(camera.GetFocalPoint(), dtype=float)
            view_vec = focal - eye
            dist = np.linalg.norm(view_vec)
            if dist < 1e-6:
                dist = 1.0
                view_vec = np.array([1.0, 0.0, 0.0])

            horiz_dist = np.hypot(view_vec[0], view_vec[1])
            yaw = math.atan2(view_vec[1], view_vec[0])
            pitch = math.atan2(view_vec[2], max(horiz_dist, 1e-9))

            sens = 0.004 * speed
            yaw -= dx * sens
            pitch = np.clip(pitch + dy * sens, -math.radians(88.5), math.radians(88.5))

            cos_pitch = math.cos(pitch)
            new_dir = np.array([
                cos_pitch * math.cos(yaw),
                cos_pitch * math.sin(yaw),
                math.sin(pitch)
            ], dtype=float)

            camera.SetPosition(*eye)
            camera.SetFocalPoint(*(eye + new_dir * dist))
            camera.SetViewUp(0.0, 0.0, 1.0)

        elif self.drag_mode == 'orbit':
            # Autodesk Navisworks / AutoCAD 3D Orbit around the inspected 3D surface pivot:
            # - Rotates the camera frame around `self.orbit_pivot` preserving Z-up upright horizon
            # - Zero jump at start even when orbiting an off-center object
            eye = np.array(camera.GetPosition(), dtype=float)
            focal = np.array(camera.GetFocalPoint(), dtype=float)
            pivot = self.orbit_pivot if self.orbit_pivot is not None else focal

            view_vec = focal - eye
            dist = float(np.linalg.norm(view_vec))
            if dist < 1e-6:
                dist = 1.0
                view_dir = np.array([1.0, 0.0, 0.0])
            else:
                view_dir = view_vec / dist

            horiz_dist = np.hypot(view_dir[0], view_dir[1])
            yaw = math.atan2(view_dir[1], view_dir[0])
            pitch = math.atan2(view_dir[2], max(horiz_dist, 1e-9))

            cos_p = math.cos(pitch)
            old_dir = np.array([cos_p * math.cos(yaw), cos_p * math.sin(yaw), math.sin(pitch)], dtype=float)
            old_right = np.array([-math.sin(yaw), math.cos(yaw), 0.0], dtype=float)
            old_up = np.cross(old_right, old_dir)

            rel_eye = eye - pivot
            c_dir = float(np.dot(rel_eye, old_dir))
            c_right = float(np.dot(rel_eye, old_right))
            c_up = float(np.dot(rel_eye, old_up))

            sens = 0.005
            new_yaw = yaw - dx * sens
            new_pitch = float(np.clip(pitch + dy * sens, -math.radians(88.5), math.radians(88.5)))

            cos_np = math.cos(new_pitch)
            new_dir = np.array([cos_np * math.cos(new_yaw), cos_np * math.sin(new_yaw), math.sin(new_pitch)], dtype=float)
            new_right = np.array([-math.sin(new_yaw), math.cos(new_yaw), 0.0], dtype=float)
            new_up = np.cross(new_right, new_dir)

            new_eye = pivot + c_dir * new_dir + c_right * new_right + c_up * new_up
            focal_dist = max(abs(c_dir), dist, 0.02)
            new_focal = new_eye + new_dir * focal_dist

            camera.SetPosition(*new_eye)
            camera.SetFocalPoint(*new_focal)
            camera.SetViewUp(0.0, 0.0, 1.0)

        elif self.drag_mode == 'pan':
            # Navisworks Pan: smooth lateral and vertical translation
            eye = np.array(camera.GetPosition(), dtype=float)
            focal = np.array(camera.GetFocalPoint(), dtype=float)
            view_vec = focal - eye
            dist = max(np.linalg.norm(view_vec), 0.01)
            direction = view_vec / dist

            up = np.array(camera.GetViewUp(), dtype=float)
            if np.linalg.norm(up) < 1e-6:
                up = np.array([0.0, 0.0, 1.0])
            else:
                up = up / np.linalg.norm(up)

            right = np.cross(direction, up)
            if np.linalg.norm(right) < 1e-6:
                right = np.array([1.0, 0.0, 0.0])
            else:
                right = right / np.linalg.norm(right)

            rw_size = interactor.GetRenderWindow().GetSize()
            height = max(rw_size[1], 1)
            if camera.GetParallelProjection():
                step = 2.0 * camera.GetParallelScale() / height
            else:
                step = 2.0 * dist * math.tan(math.radians(camera.GetViewAngle()) / 2.0) / height
            offset = (-dx * right - dy * up) * step * speed

            camera.SetPosition(*(eye + offset))
            camera.SetFocalPoint(*(focal + offset))

        # Fast O(1) near/far clipping plane update so objects never clip during close-up rotation
        self.update_clipping_range(camera, renderer, full_reset=False)
        interactor.Render()
        if self.on_change and (self._move_count % 3 == 0):
            self.on_change(camera)

    def dolly(self, direction):
        """Scroll wheel navigation: cursor-aware zoom that adapts focal distance and near clipping."""
        camera, renderer = self.camera()
        if not camera or not renderer:
            return

        self._record_pre_movement()
        interactor = self.GetInteractor()
        speed = 2.0 if self._is_shift_down(interactor) else 1.0

        if camera.GetParallelProjection():
            scale = camera.GetParallelScale()
            factor = 0.85 if direction > 0 else 1.15
            if speed > 1.0:
                factor = 0.70 if direction > 0 else 1.30
            camera.SetParallelScale(max(scale * factor, 1e-4))
        else:
            eye = np.array(camera.GetPosition(), dtype=float)
            focal = np.array(camera.GetFocalPoint(), dtype=float)
            view = focal - eye
            dist = float(np.linalg.norm(view))
            if dist < 1e-9:
                return
            view_dir = view / dist
            zoom_frac = min(0.20 * speed, 0.45)

            hit = None
            now = time.monotonic()
            if interactor:
                mx, my = interactor.GetEventPosition()
                # Reuse cached pick target during rapid scroll bursts if cursor stayed within 6px
                if (
                    self._zoom_pick_xy is not None
                    and abs(mx - self._zoom_pick_xy[0]) <= 6
                    and abs(my - self._zoom_pick_xy[1]) <= 6
                    and (now - self._zoom_pick_time) < 0.55
                ):
                    if self._zoom_target_pt is not None:
                        d_cached = float(np.dot(self._zoom_target_pt - eye, view_dir))
                        if d_cached > 1e-3:
                            hit = (self._zoom_target_pt, d_cached)
                else:
                    self._zoom_pick_xy = (mx, my)
                    hit = self._pick_single_point(int(mx), int(my), renderer, eye, view_dir)
                    self._zoom_target_pt = hit[0].copy() if hit is not None else None
                self._zoom_pick_time = now

            if hit is not None:
                pt, depth = hit
                if direction > 0:
                    to_pt = pt - eye
                    to_pt_len = max(float(np.linalg.norm(to_pt)), 1e-6)
                    # Responsive step: proportional when far, with a solid minimum (0.05m * speed)
                    # so the camera never crawls asymptotically or feels blocked by an invisible barrier
                    step = max(depth * zoom_frac, 0.05 * speed)
                    if depth > 0.06:
                        actual_step = min(step, to_pt_len * 0.85)
                        movement = (to_pt / to_pt_len) * actual_step
                        new_dist = max(depth - actual_step, 0.05)
                    else:
                        # Close to surface (<6cm): push smoothly forward through the surface along view_dir
                        movement = view_dir * step
                        new_dist = max(dist, 0.25)
                    new_eye = eye + movement
                    new_focal = new_eye + view_dir * new_dist
                else:
                    ref_dist = max(depth, 0.20)
                    step = max(ref_dist * zoom_frac, 0.05 * speed)
                    new_eye = eye - view_dir * step
                    new_dist = ref_dist + step
                    new_focal = new_eye + view_dir * new_dist
            else:
                _, ref_depth = self._scene_reference_target(renderer, eye, view_dir, focal)
                eff_dist = max(dist, min(ref_depth, 250.0), 0.25)
                step = max(eff_dist * zoom_frac, 0.05 * speed)
                if direction > 0:
                    new_eye = eye + view_dir * step
                    new_dist = max(eff_dist - step, 0.05)
                else:
                    new_eye = eye - view_dir * step
                    new_dist = eff_dist + step
                new_focal = new_eye + view_dir * new_dist

            camera.SetPosition(*new_eye)
            camera.SetFocalPoint(*new_focal)

        self.update_clipping_range(camera, renderer, full_reset=False)
        if interactor:
            interactor.Render()
        self._commit_pre_movement()
        if self.on_change:
            self.on_change(camera)
