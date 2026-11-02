"""Behavioral UI regressions on actual Qt/VTK widgets, including mouse hit-testing."""
import itertools
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import pyvista as pv
from PySide6 import QtCore, QtWidgets as W, QtTest
from plantview.gpu import configure_surface
configure_surface()
from plantview.app import Window
from plantview.branding import resource

class NavModelChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = W.QApplication.instance() or W.QApplication([])

    def setUp(self):
        self.w = Window(); self.w.show(); QtTest.QTest.qWait(100)

    def tearDown(self):
        self.w.dirty=False; self.w.close(); self.w.deleteLater(); self.app.processEvents()

    def test_cube_26_directions_preserve_target_distance_and_projection(self):
        w=self.w; cube=w.cube
        w.plot.camera_position=[(30,-40,50),(10,20,3),(0,0,1)]
        for parallel in (False,True):
            w.plot.camera.SetParallelProjection(parallel)
            w.plot.camera.SetParallelScale(17)
            focus=w.plot.camera.GetFocalPoint(); distance=w.plot.camera.GetDistance()
            for direction in itertools.product((-1,0,1),repeat=3):
                if direction==(0,0,0):continue
                cube.orient(direction)
                np.testing.assert_allclose(w.plot.camera.GetFocalPoint(),focus)
                self.assertAlmostEqual(w.plot.camera.GetDistance(),distance)
                self.assertEqual(bool(w.plot.camera.GetParallelProjection()),parallel)
                self.assertEqual(w.plot.camera.GetParallelScale(),17)
                d=np.asarray(w.plot.camera.GetPosition())-focus
                np.testing.assert_allclose(d/np.linalg.norm(d),np.asarray(direction)/np.linalg.norm(direction),atol=1e-12)
        cube.orient((1,-1,1)); self.app.processEvents()
        renderer=cube.renderer
        for point, expected in [((.4,-.4,.5),(1,-1,1)),((0,-.4,.5),(0,-1,1)),((0,0,.5),(0,0,1))]:
            renderer.SetWorldPoint(*point,1);renderer.WorldToDisplay()
            px,py,_=renderer.GetDisplayPoint()
            self.assertEqual(cube.direction_at(px,py),expected)
        renderer.SetWorldPoint(0,0,.5,1);renderer.WorldToDisplay()
        x,y,_=renderer.GetDisplayPoint()
        self.assertEqual(cube.direction_at(x,y),(0,0,1))
        rw=w.plot.interactor.GetRenderWindow(); width,height=rw.GetSize()
        point=QtCore.QPoint(round(x*w.plot.interactor.width()/width),round(w.plot.interactor.height()-y*w.plot.interactor.height()/height))
        QtTest.QTest.mouseClick(w.plot.interactor,QtCore.Qt.LeftButton,pos=point)
        np.testing.assert_allclose(w.plot.camera.GetDirectionOfProjection(),(0,0,-1),atol=1e-12)
        w.undo_camera()
        np.testing.assert_allclose(np.asarray(w.plot.camera.GetPosition())-focus,np.array([1,-1,1])*distance/np.sqrt(3))

    def test_append_real_file_and_refresh_command(self):
        w=self.w
        w.units.setCurrentIndex(1)
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'model.vtp'; pv.Cube(center=(10,20,30)).save(file)
            with patch.object(W.QFileDialog,'getOpenFileNames',return_value=([str(file)],'')):
                QtTest.QTest.mouseClick(w.ribbon.append_button,QtCore.Qt.LeftButton)
            for _ in range(1000):
                if w.worker is None and not w.queue:break
                QtTest.QTest.qWait(20)
            self.assertEqual(len(w.models),1)
            camera=np.array([list(v) for v in w.plot.camera_position])
            pv.Cube(center=(20,20,30)).save(file)
            QtTest.QTest.mouseClick(w.ribbon.refresh_button,QtCore.Qt.LeftButton)
            for _ in range(1000):
                if w.worker is None and not w.queue:break
                QtTest.QTest.qWait(20)
            self.assertEqual(len(w.models),1)
            np.testing.assert_allclose(w.models[0]['actors'][0].GetBounds(),(19.5,20.5,19.5,20.5,29.5,30.5))
            np.testing.assert_allclose([list(v) for v in w.plot.camera_position],camera)

    def test_component_isolation_properties_and_restore(self):
        w=self.w
        w.units.setCurrentIndex(1)
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'model.vtp'; pv.Cube().save(file)
            w.add_result({'path':str(file),'scale':1,'visible':True}, [('A',pv.Cube()),('B',pv.Cube(center=(3,0,0)))],[])
            other=Path(directory)/'other.vtp'; pv.Cube(center=(8,0,0)).save(other)
            w.add_result({'path':str(other),'scale':1,'visible':True}, [('Other',pv.Cube(center=(8,0,0)))],[])
            root=w.models[0]['item']; child=root.child(0)
            child.setSelected(True); self.app.processEvents()
            self.assertIsNotNone(w.selected_actor)
            self.assertGreater(w.properties.topLevelItemCount(),1)
            w.isolate(); self.assertTrue(w.selected_actor.GetVisibility())
            self.assertTrue(all(not a.GetVisibility() for a in w.models[1]["actors"]))
            w.toggle_hide_selected(); self.assertFalse(w.selected_actor.GetVisibility())
            w.show_all(); self.assertTrue(all(a.GetVisibility() for a in w.models[0]['actors']))
            w.select_all_items(); self.assertTrue(w.tree.selectedItems())
            w.clear_models(); self.assertIsNone(w.selected_actor)

    def test_layout_and_approved_icon_sizes(self):
        from PIL import Image
        w=self.w; w.resize(1536,800); self.app.processEvents()
        self.assertGreaterEqual(w.tree_dock.width(),330)
        self.assertGreaterEqual(w.properties_dock.width(),300)
        cube=w.cube
        self.assertTrue(cube.isVisible())
        self.assertGreater(cube.marker.GetViewport()[0],.5)
        brand=w.ribbon.brand_button
        tab=w.ribbon.tabs.tabBar()
        self.assertGreater(tab.mapTo(w.ribbon,QtCore.QPoint(0,0)).x(),brand.x())
        with Image.open(resource('navmodel.ico')) as icon:
            self.assertEqual(icon.ico.sizes(),{(s,s) for s in (16,24,32,48,64,128,256)})

if __name__=='__main__':unittest.main(verbosity=2)

