import unittest
import json
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import pyvista as pv
from scipy.spatial import cKDTree
from plantview.gpu import geometry_fingerprint,add_optimized_parts
from plantview.readers import engine_path, read_dwg


class GeoreferencedDwgRegression(unittest.TestCase):
    def test_same_cad_geometry_near_origin_and_at_survey_coordinates(self):
        with tempfile.TemporaryDirectory() as folder:
            near = Path(folder) / 'near.dwg'
            far = Path(folder) / 'far.dwg'
            engine = str(engine_path())
            subprocess.run([engine, '--fixture', str(near)], check=True)
            subprocess.run([engine, '--fixture-georef', str(far)], check=True)
            with patch.dict(os.environ, {'LOCALAPPDATA': folder}):
                near_parts, _ = read_dwg(near)
                far_parts, _ = read_dwg(far)
        self.assertEqual(len(near_parts), len(far_parts))
        shift = np.array([194_000., 8_745_000., -900.])
        for (_, near_mesh), (_, far_mesh) in zip(near_parts, far_parts):
            self.assertEqual(near_mesh.n_cells, far_mesh.n_cells)
            points = far_mesh.points - shift
            # Equivalent CAD faces can choose a different triangle ordering.
            for a, b in ((near_mesh.points, points), (points, near_mesh.points)):
                self.assertLess(float(cKDTree(a).query(b)[0].max()), 2e-6)
            self.assertAlmostEqual(near_mesh.area, far_mesh.area, places=5)

class CadAppearanceRegression(unittest.TestCase):
 def test_saved_white_and_gray_are_preserved_on_equipment_layers(self):
  colors=[[255,255,255],[190,201,212],[230,230,230],[37,92,141]]
  data={'units':4,'meshes':[{'name':f'{i:X} | EQUIPAMENTOS #1','color':c,'points':[[0,0,0],[1000,0,0],[0,1000,0]],'faces':[3,0,1,2]} for i,c in enumerate(colors)],'coverage':{'status':'complete'}}
  def decode(args,**kwargs):
   Path(args[2]).write_text(json.dumps(data),encoding='utf-8')
   return SimpleNamespace(poll=lambda:0,returncode=0)
  with tempfile.TemporaryDirectory() as d:
   source=Path(d)/'modelo.dwg';source.write_bytes(b'test source')
   with patch.dict(os.environ,{'LOCALAPPDATA':d}),patch('plantview.readers.subprocess.Popen',side_effect=decode):
    parts,_=read_dwg(source)
   self.assertEqual([m.field_data['plantview_rgb'].tolist()[0] for _,m in parts],colors)
class CadIdentityRegression(unittest.TestCase):
 def test_large_parts_differing_between_old_sample_points_are_not_instances(self):
  source=pv.Plane(i_resolution=50,j_resolution=50).triangulate()
  a=source.copy();b=source.copy();b.points[17,2]=.5
  self.assertNotEqual(geometry_fingerprint(a)[0],geometry_fingerprint(b)[0])
  plot=pv.Plotter(off_screen=True)
  try:
   actors,_,stats=add_optimized_parts(plot,[('a',a),('b',b),('c',a.copy())])
   self.assertEqual(stats['instanced_parts'],0)
  finally:plot.close()
 def test_translated_copies_still_share_geometry(self):
  a=pv.Cube();b=a.translate((100,200,-30),inplace=False)
  self.assertEqual(geometry_fingerprint(a)[0],geometry_fingerprint(b)[0])
 def test_connectivity_is_part_of_identity(self):
  a=pv.Plane(i_resolution=50,j_resolution=50).triangulate();b=a.copy()
  faces=b.faces.copy();faces[1],faces[2]=faces[2],faces[1];b.faces=faces
  self.assertNotEqual(geometry_fingerprint(a)[0],geometry_fingerprint(b)[0])
if __name__=='__main__':unittest.main(verbosity=2)
