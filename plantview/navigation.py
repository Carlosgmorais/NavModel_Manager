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
import numpy as np
import vtk


class NavisworksStyle(vtk.vtkInteractorStyleUser):
    def __init__(self, on_change=None, on_refresh=None):
        super().__init__()
        self.drag_mode = None
        self.previous = None
        self.on_change = on_change
        self.on_refresh = on_refresh
        self.camera_history = []
        self._pre_movement_state = None
        self.on_shift_pick = None
        self._move_count = 0
        
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
        renderer.ResetCameraClippingRange()
        interactor = self.GetInteractor()
        if interactor:
            interactor.Render()
        if self.on_change:
            self.on_change(camera)
        return True

    def left_down(self, obj, event):
        interactor = self.GetInteractor()
        if interactor and interactor.GetShiftKey():
            pos = interactor.GetEventPosition()
            if self.on_shift_pick:
                self.on_shift_pick(pos[0], pos[1])
                return
        self.previous = interactor.GetEventPosition()
        self.drag_mode = 'look'
        self._record_pre_movement()
        if interactor and interactor.GetRenderWindow():
            interactor.GetRenderWindow().SetDesiredUpdateRate(30.0)

    def middle_down(self, obj, event):
        interactor = self.GetInteractor()
        self.previous = interactor.GetEventPosition()
        self._record_pre_movement()
        if interactor and interactor.GetRenderWindow():
            interactor.GetRenderWindow().SetCurrentCursor(9)
            interactor.GetRenderWindow().SetDesiredUpdateRate(30.0)
        if interactor.GetShiftKey():
            self.drag_mode = 'orbit'
        else:
            self.drag_mode = 'pan'

    def right_down(self, obj, event):
        interactor = self.GetInteractor()
        self.previous = interactor.GetEventPosition()
        self.drag_mode = 'orbit' if interactor.GetShiftKey() else 'look'
        self._record_pre_movement()
        if interactor and interactor.GetRenderWindow():
            interactor.GetRenderWindow().SetDesiredUpdateRate(30.0)

    def mouse_up(self, obj, event):
        self.drag_mode = None
        self.previous = None
        self._move_count = 0
        self._commit_pre_movement()
        interactor = self.GetInteractor()
        if interactor and interactor.GetRenderWindow():
            interactor.GetRenderWindow().SetCurrentCursor(0)
            interactor.GetRenderWindow().SetDesiredUpdateRate(0.001)
            _, renderer = self.camera()
            if renderer:
                renderer.ResetCameraClippingRange()
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
        dx = pos[0] - self.previous[0]
        dy = pos[1] - self.previous[1]
        self.previous = pos
        if dx == 0 and dy == 0:
            return

        camera, renderer = self.camera()
        if not camera or not renderer:
            return

        self._move_count += 1

        # Sensitivity speed multiplier (Shift accelerates navigation)
        speed = 2.5 if interactor.GetShiftKey() else 1.0

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
            # Autodesk Navisworks Orbit Tool:
            # - Rotates the camera around the focal point preserving Z-up upright horizon
            eye = np.array(camera.GetPosition(), dtype=float)
            focal = np.array(camera.GetFocalPoint(), dtype=float)
            view_vec = eye - focal
            dist = np.linalg.norm(view_vec)
            if dist < 1e-6:
                dist = 1.0
                view_vec = np.array([1.0, 0.0, 0.0])

            horiz_dist = np.hypot(view_vec[0], view_vec[1])
            yaw = math.atan2(view_vec[1], view_vec[0])
            pitch = math.atan2(view_vec[2], max(horiz_dist, 1e-9))

            sens = 0.005 * speed
            yaw -= dx * sens
            pitch = np.clip(pitch - dy * sens, -math.radians(88.5), math.radians(88.5))

            cos_pitch = math.cos(pitch)
            new_offset = np.array([
                cos_pitch * math.cos(yaw),
                cos_pitch * math.sin(yaw),
                math.sin(pitch)
            ], dtype=float) * dist

            camera.SetPosition(*(focal + new_offset))
            camera.SetFocalPoint(*focal)
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

        # Do NOT call ResetCameraClippingRange on every single mouse pixel move!
        # This was causing severe CPU lag/freezing on large models.
        # It is now called once upon mouse_up.
        interactor.Render()
        if self.on_change and (self._move_count % 3 == 0):
            self.on_change(camera)

    def dolly(self, direction):
        """Scroll wheel navigation: smooth movement in camera viewing direction or scale in parallel."""
        camera, renderer = self.camera()
        if not camera or not renderer:
            return

        self._record_pre_movement()
        interactor = self.GetInteractor()
        speed = 2.5 if interactor and interactor.GetShiftKey() else 1.0

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
            dist = np.linalg.norm(view)
            if dist < 1e-9:
                return
            step = max(dist * 0.15 * speed, 0.05) * direction
            movement = (view / dist) * step
            camera.SetPosition(*(eye + movement))
            camera.SetFocalPoint(*(focal + movement))

        renderer.ResetCameraClippingRange()
        interactor.Render()
        self._commit_pre_movement()
        if self.on_change:
            self.on_change(camera)
