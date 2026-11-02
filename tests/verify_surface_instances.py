import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np,pyvista as pv
from PySide6.QtWidgets import QApplication
from plantview.app import Window
app=QApplication([]);w=Window();w.show()
p=Path(__file__)
parts=[(str(i),pv.Cube(center=(i*3,0,-5),x_length=2,y_length=2,z_length=2)) for i in range(3)]
w.add_result({'path':str(p),'scale':1,'visible':True},parts,[])
w.plot.view_xy();w.plot.reset_camera();w.plot.render()
r=w.plot.renderer
for i in range(3):
 r.SetWorldPoint(i*3,0,-4,1);r.WorldToDisplay();x,y,_=r.GetDisplayPoint()
 hit=w.measure_tool.surface_picker.pick(x,y)
 np.testing.assert_allclose(hit,[i*3,0,-4],atol=1e-5)
 print('GPU instance',i,'face',hit,flush=True)
w.dirty=False;w.close()
