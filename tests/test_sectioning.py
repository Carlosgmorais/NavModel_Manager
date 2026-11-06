"""Numerical, real mouse interaction, and GPU clipping regressions."""
import itertools
import math
import unittest
from types import SimpleNamespace

import numpy as np
import pyvista as pv
import vtk
from PySide6 import QtCore, QtGui, QtTest, QtWidgets as W

from plantview.gpu import configure_surface, make_instance_actor
from plantview.sectioning import SectionState, SectioningTool, rotation_matrix, rotation_degrees

configure_surface()


class SectionMathTests(unittest.TestCase):
    def test_all_plane_axes_negative_elevation_and_inversion(self):
        state = SectionState(enabled=True, center=np.array((100., -50., -12.)))
        for axis in range(3):
            state.axis = axis
            offset = np.eye(3)[axis]*.25
            for inverted in (False, True):
                state.inverted = inverted
                self.assertEqual(state.contains(state.center+offset), not inverted)
                self.assertEqual(state.contains(state.center-offset), inverted)
                self.assertTrue(state.contains(state.center))
        state.enabled = False
        self.assertTrue(state.contains((1e6, -1e6, -1e6)))

    def test_rotated_box_six_faces_and_nonzero_world_coordinates(self):
        state = SectionState(enabled=True, mode='box', center=np.array((10000., -8000., -200.)),
                             rotation=rotation_matrix((20, 30, 40)), half_sizes=np.array((2., 3., 4.)))
        self.assertEqual(state.equations().shape, (6, 4))
        for axis, sign in itertools.product(range(3), (-1, 1)):
            offset = state.rotation[:, axis]*sign
            self.assertTrue(state.contains(state.center+offset*(state.half_sizes[axis]-.01)))
            self.assertFalse(state.contains(state.center+offset*(state.half_sizes[axis]+.01)))
        np.testing.assert_allclose(rotation_matrix(rotation_degrees(state.rotation)), state.rotation, atol=1e-12)

    def test_every_box_face_moves_only_itself_and_cannot_cross_opposite(self):
        for axis, sign in itertools.product(range(3), (-1, 1)):
            state = SectionState(enabled=True, mode='box', rotation=rotation_matrix((10, 20, 35)))
            direction = state.rotation[:, axis]*sign
            opposite_before = state.center-direction*state.half_sizes[axis]
            face_before = state.center+direction*state.half_sizes[axis]
            state.move_face(axis, sign, .6)
            np.testing.assert_allclose(state.center-direction*state.half_sizes[axis], opposite_before, atol=1e-12)
            np.testing.assert_allclose(state.center+direction*state.half_sizes[axis], face_before+direction*.6, atol=1e-12)
            state.move_face(axis, sign, -100)
            self.assertGreaterEqual(state.half_sizes[axis], 4.9e-7)
            np.testing.assert_allclose(state.center-direction*state.half_sizes[axis], opposite_before, atol=1e-12)

    def test_oriented_fit_contains_every_model_corner(self):
        state = SectionState(enabled=True, mode='box', rotation=rotation_matrix((20, -33, 45)))
        bounds = (-100, 25, -50, 80, -14, 8)
        state.fit(bounds)
        for corner in itertools.product(*np.asarray(bounds).reshape(3, 2)):
            self.assertTrue(state.contains(corner))


class SectionUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = W.QApplication.instance() or W.QApplication([])

    def setUp(self):
        from plantview.app import Window
        self.w = Window(); self.w.show(); QtTest.QTest.qWait(70)
        self.tool = self.w.section_tool
        a = self.w.plot.add_mesh(pv.Cube(center=(-2, 0, -3)), color='silver', reset_camera=False)
        b = self.w.plot.add_mesh(pv.Cube(center=(2, 0, -3)), color='silver', reset_camera=False)
        self.w.models.extend([dict(actors=[a]), dict(actors=[b])])
        self.w.plot.camera_position = [(7, -12, 7), (0, 0, -3), (0, 0, 1)]
        self.w.plot.camera.SetParallelProjection(True); self.w.plot.camera.SetParallelScale(5)
        self.w.plot.render(); self.app.processEvents()

    def tearDown(self):
        self.tool.clear(); self.tool.close()
        self.w.models = []; self.w.dirty = False; self.w.close(); self.w.deleteLater(); self.app.processEvents()

    def test_clipping_attached_to_all_models_append_refresh_and_disable(self):
        tool = self.tool
        tool.set_axis(0)
        for model in self.w.models:
            self.assertEqual(model['actors'][0].GetMapper().GetNumberOfClippingPlanes(), 1)
        tool.set_mode('box')
        for model in self.w.models:
            self.assertEqual(model['actors'][0].GetMapper().GetNumberOfClippingPlanes(), 6)
        new = self.w.plot.add_mesh(pv.Cube(center=(8, 0, -3)), reset_camera=False)
        self.w.models.append(dict(actors=[new])); tool.refresh_models()
        self.assertEqual(new.GetMapper().GetNumberOfClippingPlanes(), 6)
        tool.set_enabled(False)
        self.assertTrue(all(a.GetMapper().GetNumberOfClippingPlanes() == 0 for m in self.w.models for a in m['actors']))
        self.assertFalse(tool.outline.GetVisibility())

    def _qt_position(self, display):
        widget = self.w.plot.interactor; width, height = widget.GetRenderWindow().GetSize()
        return QtCore.QPoint(round(display[0]*widget.width()/width), round(widget.height()-display[1]*widget.height()/height))

    def test_real_mouse_axis_drag_moves_plane_in_axis_direction_without_camera_change(self):
        tool = self.tool; tool.set_axis(0)
        self.app.processEvents(); self.w.plot.render()
        handle = next(h for h in tool._handle_data if h[0] == 'move' and h[1] == 0)
        _, _, _, origin, direction, length = handle
        start = tool._display(origin+direction*length*.72)
        end = tool._display(origin+direction*(length*.72+.45))
        center = tool.state.center.copy(); camera = self.w.plot.camera.GetPosition()
        widget = self.w.plot.interactor
        QtTest.QTest.mousePress(widget, QtCore.Qt.LeftButton, pos=self._qt_position(start))
        self.assertIsNotNone(tool.drag)
        QtTest.QTest.mouseMove(widget, self._qt_position(end), delay=20)
        QtTest.QTest.mouseRelease(widget, QtCore.Qt.LeftButton, pos=self._qt_position(end))
        self.assertIsNone(tool.drag)
        self.assertAlmostEqual(tool.state.center[0]-center[0], .45, delta=.025)
        np.testing.assert_allclose(tool.state.center[1:], center[1:], atol=1e-10)
        np.testing.assert_allclose(self.w.plot.camera.GetPosition(), camera)

    def test_box_drag_rotated_face_and_rotation_ring(self):
        tool = self.tool; tool.set_mode('box'); tool.state.rotation = rotation_matrix((10, 20, 30)); tool._changed()
        handle = next(h for h in tool._handle_data if h[0] == 'face' and h[1] == 0 and h[2] == 1)
        _, axis, sign, origin, direction, length = handle
        opposite = tool.state.center-direction*tool.state.half_sizes[axis]
        start = tool._display(origin+direction*length*.7); end = tool._display(origin+direction*(length*.7+.3))
        self.assertTrue(tool.begin_drag(handle, *start)); tool.drag_to(*end); tool.end_drag()
        np.testing.assert_allclose(tool.state.center-direction*tool.state.half_sizes[axis], opposite, atol=1e-10)
        np.testing.assert_allclose(tool.state.center+direction*tool.state.half_sizes[axis], origin+direction*.3, atol=1e-10)
        tool.set_transform_mode('rotate'); tool.state.rotation = np.eye(3); tool._changed()
        handle = next(h for h in tool._handle_data if h[0] == 'rotate' and h[1] == 2)
        radius = np.linalg.norm(handle[5][0]-tool.state.center)
        start = tool._display(tool.state.center+np.array((radius, 0, 0)))
        end = tool._display(tool.state.center+np.array((0, radius, 0)))
        self.assertTrue(tool.begin_drag(handle, *start)); tool.drag_to(*end); tool.end_drag()
        np.testing.assert_allclose(tool.state.rotation, rotation_matrix((0, 0, 90)), atol=1e-10)

    def test_measurement_has_priority_and_escape_restores_drag(self):
        tool = self.tool; tool.set_axis(0)
        handle = next(h for h in tool._handle_data if h[0] == 'move' and h[1] == 0)
        start = tool._display(handle[3]+handle[4]*handle[5]*.8)
        widget = self.w.plot.interactor; pos = self._qt_position(start)
        self.w.measure_mode = True
        press = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, QtCore.QPointF(pos), QtCore.QPointF(pos), QtCore.Qt.LeftButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
        self.assertFalse(tool.eventFilter(widget, press)); self.assertIsNone(tool.drag)
        self.w.measure_mode = False
        old = tool.state.center.copy()
        tool.begin_drag(handle, *start); tool.drag_to(*(start+np.array((50, 0))))
        QtTest.QTest.keyClick(widget, QtCore.Qt.Key_Escape)
        np.testing.assert_allclose(tool.state.center, old)
        self.assertIsNone(tool.drag)


class GPUClipTests(unittest.TestCase):
    def test_gpu_instances_match_expanded_geometry_with_rotated_box_and_actor_transform(self):
        source = pv.Cube().triangulate()
        origins = np.asarray([(-2, 0, 0), (0, 0, 0), (2, 0, 0)], dtype=float)
        state = SectionState(enabled=True, mode='box', center=np.array((.5, -.3, 0.)),
                             rotation=rotation_matrix((0, 0, 20)), half_sizes=np.array((2., .7, 2.)))
        masks = []
        for instanced in (False, True):
            plot = pv.Plotter(off_screen=True, window_size=(360, 260)); plot.set_background('white')
            if instanced:
                actor = make_instance_actor(plot, source, origins, (.1, .1, .1))
            else:
                meshes = [source.translate(origin, inplace=False) for origin in origins]
                actor = plot.add_mesh(pv.merge(meshes, merge_points=False), color=(.1, .1, .1))
            transform = vtk.vtkTransform()
            transform.Translate(.2, -.1, .1)
            transform.RotateZ(13)
            actor.SetUserTransform(transform)
            actor.GetProperty().LightingOff()
            planes = []
            for equation in state.equations():
                plane = vtk.vtkPlane(); plane.SetNormal(*equation[:3]); plane.SetOrigin(*(-equation[3]*equation[:3]))
                actor.GetMapper().AddClippingPlane(plane); planes.append(plane)
            plot.camera_position = [(5, -12, 7), (0, 0, 0), (0, 0, 1)]
            plot.camera.SetParallelProjection(True); plot.camera.SetParallelScale(2.3)
            image = plot.screenshot(return_img=True)
            masks.append(np.min(image[:, :, :3], axis=2) < 100)
            plot.close()
        self.assertGreater(np.count_nonzero(masks[0]), 1500)
        intersection = np.count_nonzero(masks[0] & masks[1]); union = np.count_nonzero(masks[0] | masks[1])
        self.assertGreater(intersection/union, .97, 'GPU glyph cuts must agree with explicitly transformed geometry')


if __name__ == '__main__':
    unittest.main(verbosity=2)
