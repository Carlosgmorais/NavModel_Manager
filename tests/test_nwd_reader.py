import os
import unittest
from pathlib import Path
import pyvista as pv

from plantview.readers import read_model
from plantview.nwd_reader import parse_navisworks_toc, read_nwd


class TestNwdReader(unittest.TestCase):

    def setUp(self):
        self.sample_nwc = Path(r'C:\Users\c.morais\Desktop\ARQUIVOS VARIADOS\PÚBLICO\2655-A-Galpão_CONCRETO(revisado).nwc')
        self.sample_nwd = Path(r'C:\Users\c.morais\Desktop\ARQUIVOS VARIADOS\EMISSÃO\83512_R12.nwd')

    def test_nwc_toc_parsing(self):
        if not self.sample_nwc.exists():
            self.skipTest('Sample NWC file not found')
        with open(self.sample_nwc, 'rb') as f:
            toc = parse_navisworks_toc(f, self.sample_nwc.stat().st_size)
            self.assertIn('cache\\LcOpNwdGeometry', toc)
            self.assertIn('cache\\LcOpNwdFragments', toc)
            self.assertIn('cache\\LcOpNwdLogicalHierarchy', toc)

    def test_nwd_toc_parsing(self):
        if not self.sample_nwd.exists():
            self.skipTest('Sample NWD file not found')
        with open(self.sample_nwd, 'rb') as f:
            toc = parse_navisworks_toc(f, self.sample_nwd.stat().st_size)
            self.assertIn('cache\\LcOpNwdGeometry', toc)
            self.assertIn('cache\\LcOpNwdLogicalHierarchy', toc)

    def test_read_nwc_model(self):
        if not self.sample_nwc.exists():
            self.skipTest('Sample NWC file not found')
        parts, warns = read_model(self.sample_nwc)
        self.assertGreater(len(parts), 0, 'NWC model must produce parts')
        for name, mesh in parts[:5]:
            self.assertIsInstance(mesh, pv.PolyData)
            self.assertGreater(mesh.n_points, 0)
            self.assertGreater(mesh.n_cells, 0)
            self.assertTrue(hasattr(mesh, '_plantview_layer') or 'plantview_layer' in mesh.field_data)

    def test_read_nwd_model(self):
        if not self.sample_nwd.exists():
            self.skipTest('Sample NWD file not found')
        parts, warns = read_model(self.sample_nwd)
        self.assertGreater(len(parts), 0, 'NWD model must produce parts')
        total_cells = sum(m.n_cells for _, m in parts)
        self.assertGreater(total_cells, 1000, 'NWD industrial model must have cells')

    def test_read_galpao_nwc(self):
        galpao = Path(r'W:\Projetos\AFF\P-2655 - Novo Site Indaiatuba\000 BIM\00_Modelos_Publicados\Arquitetura\2655-000-SAG-ARQ-GALPAO.nwc')
        if not galpao.exists():
            self.skipTest('Galpao NWC not found')
        parts, warns = read_model(galpao)
        self.assertGreater(len(parts), 0, 'Galpao NWC must produce parts')
        for name, mesh in parts:
            self.assertGreater(mesh.n_cells, 0)
            # Verify no crazy spikes: maximum bounding box diagonal must be reasonable (< 50,000 m)
            b = mesh.bounds
            dx, dy, dz = b[1] - b[0], b[3] - b[2], b[5] - b[4]
            self.assertLess(max(dx, dy, dz), 50000.0)


if __name__ == '__main__':
    unittest.main()
