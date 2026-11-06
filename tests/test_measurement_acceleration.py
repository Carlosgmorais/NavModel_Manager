"""Accuracy, invalidation and bounded-work regressions for spatial measurement."""
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pyvista as pv
import vtk
from PySide6 import QtWidgets as W
from plantview.surface_picker import SurfacePicker, BoundsTree
from plantview.gpu import make_instance_actor


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.plot = pv.Plotter(off_screen=True, window_size=(800, 600))
        self.window = SimpleNamespace(plot=self.plot, models=[])
        self.picker = SurfacePicker(self.window)

    def tearDown(self):
        self.picker.clear()
        self.plot.close()

    def add(self, mesh, **kwargs):
        actor = self.plot.add_mesh(mesh, render=False, **kwargs)
        self.window.models.append({'actors': [actor]})
        return actor

    def test_negative_world_and_actor_transform(self):
        actor = self.add(pv.Cube(x_length=2, y_length=4, z_length=2))
        actor.SetPosition(12345., -20., -5.)
        actor.RotateZ(90)
        hit = self.picker.ray_pick([12345, -20, 10], [12345, -20, -20])
        np.testing.assert_allclose(hit.point, [12345, -20, -4], atol=1e-8)
        self.assertIs(hit.actor, actor)

    def test_instance_bvh_prunes_ten_thousand_objects_and_shares_locator(self):
        origins = np.column_stack((np.arange(10000) * 3., np.zeros(10000), np.full(10000, -5.)))
        actor = make_instance_actor(self.plot, pv.Cube(x_length=2, y_length=2, z_length=2), origins, (1, 0, 0))
        self.window.models.append({'actors': [actor]})
        start = time.perf_counter()
        self.picker.prepare()
        build_seconds = time.perf_counter() - start
        samples = []
        for i in (0, 128, 2048, 5001, 9999):
            start = time.perf_counter()
            hit = self.picker.ray_pick([i * 3., 0, 10], [i * 3., 0, -10])
            samples.append(time.perf_counter() - start)
            np.testing.assert_allclose(hit.point, [i * 3., 0, -4.], atol=1e-8)
            self.assertEqual(hit.instance_id, i)
            self.assertLessEqual(self.picker.last_stats['candidates'], 1)
            self.assertLess(self.picker.last_stats['nodes'], 30)
        self.assertEqual(self.picker.last_stats['locator_builds'], 1)
        print('BVH 10,000 instances: build %.3fs; cached ray median %.3fms; candidates=%s' %
              (build_seconds, np.median(samples) * 1000, self.picker.last_stats['candidates']), flush=True)

    def test_visibility_transform_and_geometry_invalidate(self):
        mesh = pv.Cube(x_length=2, y_length=2, z_length=2)
        actor = self.add(mesh)
        ray = ([0, 0, 10], [0, 0, -10])
        np.testing.assert_allclose(self.picker.ray_pick(*ray).point, [0, 0, 1])
        actor.SetVisibility(False)
        self.assertIsNone(self.picker.ray_pick(*ray))
        actor.SetVisibility(True)
        actor.SetPosition(0, 0, -4)
        np.testing.assert_allclose(self.picker.ray_pick(*ray).point, [0, 0, -3])
        mesh.translate((0, 0, -2), inplace=True)
        np.testing.assert_allclose(self.picker.ray_pick(*ray).point, [0, 0, -5])
        self.assertEqual(len(self.picker.cache), 1)

    def test_cut_front_face_returns_retained_back_face(self):
        self.add(pv.Cube(x_length=2, y_length=2, z_length=2))
        self.window.section_tool = SimpleNamespace(contains_point=lambda p: p[2] <= 0, revision=1)
        hit = self.picker.ray_pick([0, 0, 10], [0, 0, -10])
        np.testing.assert_allclose(hit.point, [0, 0, -1], atol=1e-8)

    def test_nearest_surface_across_models_and_removal(self):
        a = self.add(pv.Cube(center=(0, 0, -5)))
        b = self.add(pv.Cube(center=(0, 0, -2)))
        self.assertIs(self.picker.ray_pick([0, 0, 10], [0, 0, -10]).actor, b)
        self.window.models.pop()
        self.assertIs(self.picker.ray_pick([0, 0, 10], [0, 0, -10]).actor, a)

    def test_screen_face_vertex_edge_and_same_pointer_cache(self):
        self.add(pv.Cube(x_length=2, y_length=2, z_length=2))
        self.plot.view_xy()
        self.plot.enable_parallel_projection()
        self.plot.reset_camera()
        self.plot.render()
        renderer = self.plot.renderer
        def pick_at(point, snap=True):
            renderer.SetWorldPoint(*point, 1)
            renderer.WorldToDisplay()
            x, y, _ = renderer.GetDisplayPoint()
            return self.picker.pick_hit(x, y, snap=snap), (x, y)
        face, _ = pick_at([.1, .2, 1])
        self.assertEqual(face.kind, 'Face')
        vertex, _ = pick_at([.995, .995, 1])
        self.assertEqual(vertex.kind, 'Vértice')
        np.testing.assert_allclose(vertex.point, [1, 1, 1], atol=1e-8)
        edge, (x, y) = pick_at([.995, .1, 1])
        self.assertEqual(edge.kind, 'Aresta da malha')
        self.assertAlmostEqual(edge.point[0], 1)
        self.assertIs(self.picker.pick_hit(x, y, snap=True), edge)

    def test_parallel_ray_on_bounds_and_large_coordinates(self):
        tree = BoundsTree([[[1e6, -1, -1], [1e6+1, 1, 1]]])
        self.assertEqual(len(list(tree.candidates(np.array([1e6, 0, 10]), np.array([1e6, 0, -10])))), 1)
        self.assertEqual(len(list(tree.candidates(np.array([1e6-1, 0, 10]), np.array([1e6-1, 0, -10])))), 0)

    def test_actual_dwg_and_step_rendered_geometry(self):
        from plantview.readers import read_model
        from plantview.gpu import add_optimized_parts
        root = Path(__file__).resolve().parents[1]
        for name in ('equipamentos.dwg', 'estrutura.step'):
            parts, _ = read_model(root / 'examples' / name)
            actors, _, _ = add_optimized_parts(self.plot, parts)
            self.window.models.append({'actors': actors})
            self.picker.invalidate()
            hit_count = 0
            # Cast normal rays at actual reader-provided face centroids.
            for _, part in parts[:8]:
                surface = part.triangulate()
                for i in range(min(10, surface.n_cells)):
                    cell = surface.get_cell(i)
                    if cell.dimension != 2 or len(cell.points) < 3:
                        continue
                    points = cell.points
                    center = points[:3].mean(axis=0)
                    normal = np.cross(points[1]-points[0], points[2]-points[0])
                    norm = np.linalg.norm(normal)
                    if norm < 1e-10:
                        continue
                    normal /= norm
                    extent = max(surface.length, .01)
                    hit = self.picker.ray_pick(center + normal * extent, center - normal * extent)
                    if hit is not None:
                        hit_count += 1
            self.assertGreater(hit_count, 0, name)
            print(name, 'actual surfaces intersected:', hit_count, flush=True)
            self.window.models.clear()
            self.picker.clear()
            for actor in actors:
                self.plot.remove_actor(actor, render=False)


class MeasurementOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = W.QApplication.instance() or W.QApplication([])

    def test_world_delta_persistent_geometry_and_clear(self):
        from plantview.measurement import MeasurementTool
        plot = pv.Plotter(off_screen=True)
        window = W.QMainWindow()
        window.models = []
        window.measure_mode = False
        widget = W.QWidget()
        window.plot = SimpleNamespace(renderer=plot.renderer, interactor=widget,
                                      add_actor=plot.add_actor, render=lambda: None)
        tool = MeasurementTool(window)
        addresses = [a.GetAddressAsString('') for a in tool._actors]
        tool.start = np.array([100000., -200000., -30.])
        tool.draw([100003., -199996., -18.])
        before = plot.renderer.GetActors().GetNumberOfItems()
        for i in range(20):
            tool.draw([100003., -199996., -18.])
        self.assertEqual(tool.last_result['distance'], 13.)
        self.assertEqual(tool.last_result['delta'], [3., 4., 12.])
        self.assertEqual(plot.renderer.GetActors().GetNumberOfItems(), before)
        self.assertEqual([a.GetAddressAsString('') for a in tool._actors], addresses)
        tool.start = np.array([0., 0., 0.])
        tool.draw([-3., -4., -12.])
        self.assertEqual(tool.last_result['delta'], [-3., -4., -12.])
        tool.clear()
        self.assertTrue(all(not a.GetVisibility() for a in tool._actors + tool._labels))
        plot.close()
        window.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
