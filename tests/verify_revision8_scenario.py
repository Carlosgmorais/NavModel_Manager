import sys, tempfile, os, json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from unittest.mock import patch
import numpy as np
import pyvista as pv
from PySide6 import QtWidgets as W, QtCore, QtTest
from plantview.app import Window
app=W.QApplication([]);w=Window();w.show()
result={}
with tempfile.TemporaryDirectory() as d:
    for i in range(10):
        p=Path(d)/f'model{i}.vtp';pv.Cube(center=(i*2,0,0)).save(p)
        w.add_result({'path':str(p),'scale':1,'visible':True},[(str(i),pv.Cube(center=(i*2,0,0)))],[])
    with patch.object(w,'enqueue') as enqueue:
        w.refresh_models();assert not enqueue.called
        p=Path(d)/'model3.vtp';pv.Cube(center=(42,0,0)).save(p)
        os.utime(p,ns=(p.stat().st_atime_ns,p.stat().st_mtime_ns+1000000000))
        w.refresh_models();items=enqueue.call_args.args[0];assert len(items)==1 and items[0]['path']==str(p)
        result['refresh']='10 arquivos: nenhum inalterado recarregado; somente model3 alterado enfileirado'
    assert w.windowTitle()=='Novo modelo — NavModel Manager'
    p=Path(d)/'Minha planta.pvproj'
    with patch.object(W.QFileDialog,'getSaveFileName',return_value=(str(p),'')):assert w.save()
    assert w.windowTitle()=='Minha planta.pvproj — NavModel Manager'
    assert w.ribbon.project_title.text()==w.windowTitle()
    result['title']='Novo modelo e nome salvo verificados nas duas barras'
    w.measure_tool.start=np.array([0.,0.,0.]);w.measure_tool.draw([3.,4.,12.])
    assert w.measure_tool.last_result['distance']==13.
    result['measure']=w.measure_tool.last_result
    w.plot.view_isometric();w.plot.reset_camera();app.processEvents();QtTest.QTest.qWait(1000)
    out=Path(__file__).resolve().parents[3]/'outputs'/'v0.0.8';out.mkdir(parents=True,exist_ok=True)
    w.screen().grabWindow(int(w.winId())).save(str(out/'interface-medicao.png'))
    (out/'validacao.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
w.dirty=False;w.close();print(json.dumps(result,ensure_ascii=False))
