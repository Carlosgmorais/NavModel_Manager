import json
from pathlib import Path
import struct
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import numpy as np
import pyvista as pv
import vtk

from plantview.web_publish import WEB_ROOT, PublishLimits, _binary_parts, analyze_scene, export_scene


class WebPublishTests(unittest.TestCase):
    def setUp(self):
        self.mesh = pv.Cube(center=(10000, 20000, -30)).triangulate()
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(self.mesh)
        self.actor = vtk.vtkActor()
        self.actor.SetMapper(mapper)
        self.actor._plantview_name = '</script><script>throw 42</script>'
        self.models = [{'path': 'modelo.dwg', 'actors': [self.actor]}]

    def test_progressive_chunks_preserve_world_coordinates(self):
        parts = list(_binary_parts(self.mesh, max_cells=4))
        self.assertEqual(len(parts), 3)
        for binary, origin in parts:
            _, n, indices, _ = struct.unpack_from('<4sIII', binary)
            self.assertEqual(indices, 12)
            points = np.frombuffer(binary, '<f8', n * 3, 16).reshape(-1, 3) + origin
            for point in points:
                self.assertTrue(np.any(np.all(np.asarray(self.mesh.points) == point, axis=1)))

    def test_light_html_and_hard_limit(self):
        self.assertEqual(analyze_scene(self.models).triangles, 12)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'light.html'
            export_scene(self.models, path)
            html = path.read_text(encoding='utf-8')
            self.assertNotIn(self.actor._plantview_name, html)
            self.assertNotIn('<script src=', html)
            with self.assertRaises(ValueError):
                export_scene(self.models, path, limits=PublishLimits(max_triangles=11))
            self.assertEqual(path.read_text(encoding='utf-8'), html)

    def test_publish_dialog_saves_html_and_disables_it_below_limit(self):
        from PySide6 import QtCore, QtWidgets as W
        from plantview.app import Window
        app = W.QApplication.instance() or W.QApplication([])
        window = Window()
        window.add_result({'path': 'modelo.dwg', 'visible': True, 'scale': 1.},
                          [('Cubo', self.mesh)], [])
        failures = []
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'published.html'
            timer = QtCore.QTimer()
            started = time.monotonic()
            clicked = False

            def exercise():
                nonlocal clicked
                dialog = app.activeModalWidget()
                if dialog is None:
                    return
                if time.monotonic() - started > 15:
                    failures.append('A publicação não terminou')
                    dialog.reject()
                    return
                if clicked:
                    if target.exists() and not dialog.worker.isRunning():
                        dialog.accept()
                    return
                try:
                    html = next(button for button in dialog.findChildren(W.QPushButton)
                                if button.text() == 'Salvar HTML único')
                    limit = dialog.findChildren(W.QSpinBox)[1]
                    limit.setValue(1)
                    self.assertFalse(html.isEnabled())
                    limit.setValue(100)
                    self.assertTrue(html.isEnabled())
                    clicked = True
                    html.click()
                except Exception as exc:
                    failures.append(str(exc))
                    dialog.reject()

            timer.timeout.connect(exercise)
            timer.start(25)
            with patch.object(W.QFileDialog, 'getSaveFileName', return_value=(str(target), '')):
                window.publish_web()
            timer.stop()
            self.assertEqual(failures, [])
            self.assertTrue(target.is_file())
        window.dirty = False
        window.close()

    @unittest.skipUnless((WEB_ROOT / 'NavModelWebHost.exe').is_file(), 'Build the portable host first')
    def test_single_file_host_and_route_isolation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'model.exe'
            url_file = Path(folder) / 'url.txt'
            export_scene(self.models, path, kind='exe')
            proc = subprocess.Popen([str(path), '--no-browser', '--url-file', str(url_file)],
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                for _ in range(150):
                    if url_file.exists() and url_file.stat().st_size:
                        break
                    self.assertIsNone(proc.poll())
                    time.sleep(.1)
                url = url_file.read_text(encoding='utf-8')
                self.assertTrue(url.startswith('http://127.0.0.1:'))
                with urllib.request.urlopen(url + 'manifest.json', timeout=5) as response:
                    manifest = json.load(response)
                resource = manifest['actors'][0]['parts'][0]['chunk']
                with urllib.request.urlopen(url + resource, timeout=5) as response:
                    self.assertEqual(response.read(4), b'NVM1')
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                for request in (
                    urllib.request.Request(url + 'manifest.json', headers={'Origin': 'https://example.org'}),
                    urllib.request.Request(url + '../manifest.json'),
                    urllib.request.Request(url, method='POST'),
                ):
                    with self.assertRaises(urllib.error.HTTPError):
                        urllib.request.urlopen(request, timeout=5)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=10)
