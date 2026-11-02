"""Integration checks; run with Python 3.12 from the source root."""
import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_API'] = 'pyside6'
import json
import tempfile
import unittest
import subprocess
import numpy as np
import pyvista as pv
from plantview.readers import read_model, engine_path
from plantview.project import save_project, load_project

class Checks(unittest.TestCase):
    def test_dwg_cache_and_native_cancellation(self):
        from unittest.mock import patch
        import plantview.readers as readers
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'cached.dwg'
            subprocess.run([str(engine_path()), '--fixture', str(path)], check=True)
            with patch.dict(os.environ, {'LOCALAPPDATA': d}):
                read_model(path)
                with patch.object(readers.subprocess, 'Popen', side_effect=AssertionError('Cache must bypass decoder')):
                    parts, _ = read_model(path)
                    self.assertEqual(len(parts), 2)
                path.touch()
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
                def cancel(pct, message):
                    if 'decorridos' in message:
                        raise InterruptedError('cancelled')
                    return True
                try:
                    with patch.object(readers.subprocess, 'Popen', return_value=child):
                        with self.assertRaises(InterruptedError):
                            read_model(path, progress_callback=cancel)
                    self.assertIsNotNone(child.poll())
                finally:
                    if child.poll() is None:
                        child.kill(); child.wait()

    def test_project_roundtrip_and_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'test.pvproj'
            model = {'path': str((Path(d)/'modelo.step').resolve()), 'visible': False, 'scale': .001}
            save_project(p, [model], [[1,2,3],[0,0,0],[0,0,1]])
            result = load_project(p)
            self.assertEqual(result['models'], [model])
            data = json.loads(p.read_text())
            data['models'][0]['scale'] = -1
            p.write_text(json.dumps(data))
            with self.assertRaises(ValueError): load_project(p)

    def test_dwg_native_units_and_placement(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'solid.dwg'
            subprocess.run([str(engine_path()), '--fixture', str(path)], check=True)
            before = path.read_bytes()
            parts, warnings = read_model(path)
            self.assertEqual(len(parts), 2)
            np.testing.assert_allclose(parts[0][1].bounds, [9.5,10.5,19,21,1.5,4.5], atol=1e-6)
            self.assertEqual(before, path.read_bytes())

    def test_step_units_and_placement(self):
        import cadquery as cq
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'box.step'
            cq.exporters.export(cq.Workplane().box(1000,2000,3000).translate((10000,20000,3000)), str(p))
            parts, _ = read_model(p)
            np.testing.assert_allclose(parts[0][1].bounds, [9.5,10.5,19,21,1.5,4.5], atol=1e-6)

    def test_ifc_world_placement(self):
        import ifcopenshell.api
        api = ifcopenshell.api.run
        with tempfile.TemporaryDirectory() as d:
            f = api('project.create_file')
            api('root.create_entity', f, ifc_class='IfcProject', name='Teste')
            api('unit.assign_unit', f)
            context = api('context.add_context', f, context_type='Model')
            body = api('context.add_context', f, context_type='Model', context_identifier='Body', target_view='MODEL_VIEW', parent=context)
            wall = api('root.create_entity', f, ifc_class='IfcWall', name='Parede')
            representation = api('geometry.add_wall_representation', f, context=body, length=4., height=3., thickness=.2)
            api('geometry.assign_representation', f, product=wall, representation=representation)
            matrix = np.eye(4); matrix[:3,3] = [10,20,0]
            api('geometry.edit_object_placement', f, product=wall, matrix=matrix)
            p = Path(d)/'wall.ifc'; f.write(str(p))
            parts, warnings = read_model(p)
            self.assertEqual(len(parts),1)
            np.testing.assert_allclose(parts[0][1].bounds, [10,14,20,20.2,0,3], atol=1e-6)

    def test_dxf_warning_and_units(self):
        import ezdxf
        with tempfile.TemporaryDirectory() as d:
            doc = ezdxf.new(); doc.header['$INSUNITS'] = 4
            doc.modelspace().add_line((1000,2000,0),(2000,2000,0))
            doc.modelspace().add_text('ignored')
            p = Path(d)/'test.dxf'; doc.saveas(p)
            parts, warnings = read_model(p)
            self.assertEqual(len(parts),1)
            self.assertTrue(any('TEXT' in w for w in warnings))
            np.testing.assert_allclose(parts[0][1].bounds,[1,2,2,2,0,0])


    def test_dwg_coverage_report_and_diagnostics(self):
        """Valida que o leitor reporta cobertura COMPLETA e diagnóstico detalhado de sólidos 3D."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'solids_cov.dwg'
            subprocess.run([str(engine_path()), '--fixture', str(path)], check=True)
            parts, warnings = read_model(path, force_reload=True)
            self.assertEqual(len(parts), 2)
            diag_lines = [w for w in warnings if 'PLANTVIEW STUDIO — DIAGNÓSTICO' in w or 'Status de Cobertura' in w]
            self.assertTrue(len(diag_lines) >= 1)
            self.assertTrue(any('COMPLETO' in w for w in warnings))

    def test_gpu_diagnostic_isolated_mode(self):
        """Valida o modo de diagnóstico isolado (PLANTVIEW_NO_BATCH=1) da Prioridade 5."""
        from unittest.mock import patch
        from plantview.gpu import add_optimized_parts
        plot = pv.Plotter(off_screen=True)
        m1 = pv.Cube(center=(0, 0, 0))
        m2 = pv.Cube(center=(5, 0, 0))
        parts = [('Cube1', m1), ('Cube2', m2)]
        with patch.dict(os.environ, {'PLANTVIEW_NO_BATCH': '1'}):
            actors, names, stats = add_optimized_parts(plot, parts, visible=True)
            self.assertEqual(len(actors), 2)
            self.assertEqual(stats['gpu_actors'], 2)
            self.assertEqual(stats['mode'], 'DIAGNOSTIC_ISOLATED')
        plot.close()

    def test_unsupported_format(self):
        with self.assertRaisesRegex(ValueError, 'SDK'): read_model('model.rvt')

class GuiChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_refresh_preserves_camera_visibility_and_failed_model(self):
        from PySide6.QtCore import QEventLoop, QTimer, Qt
        from plantview.app import Window
        w = Window()
        w.show()
        self.assertIs(w.plot.interactor.GetInteractorStyle(), w.navigation)
        self.assertFalse(w.plot.camera.GetParallelProjection())
        def wait_import():
            loop = QEventLoop()
            timer = QTimer()
            timer.timeout.connect(lambda: loop.quit() if w.worker is None else None)
            timer.start(30)
            QTimer.singleShot(30000,loop.quit)
            loop.exec(); timer.stop()
            self.assertIsNone(w.worker)
        try:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d)/'box.vtp'
                pv.Cube().save(p)
                w.enqueue([{'path':str(p),'visible':False,'scale':1}]); wait_import()
                self.assertEqual(len(w.models),1)
                old = w.models[0]['actors'][0]
                camera = [list(v) for v in w.plot.camera_position]
                pv.Cube(center=(20,0,0)).save(p)
                w.refresh_models(); wait_import()
                self.assertEqual(len(w.models),1)
                self.assertFalse(w.models[0]['visible'])
                self.assertIsNot(old,w.models[0]['actors'][0])
                np.testing.assert_allclose(w.models[0]['actors'][0].bounds, [19.5,20.5,-.5,.5,-.5,.5])
                np.testing.assert_allclose([list(v) for v in w.plot.camera_position],camera)
                good = w.models[0]['actors'][0]
                p.unlink()
                w.refresh_models(); wait_import()
                self.assertIs(good,w.models[0]['actors'][0])
                w.models[0]['item'].setCheckState(0,Qt.Checked)
                self.assertTrue(w.models[0]['actors'][0].GetVisibility())
        finally:
            w.dirty = False; w.close()

if __name__ == '__main__': unittest.main(verbosity=2)
