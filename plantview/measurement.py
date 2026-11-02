"""Live, surface-snapped point-to-point dimensions in world coordinates."""
import time
import numpy as np
import pyvista as pv
import vtk
from PySide6 import QtCore, QtWidgets as W

class MeasurementTool(QtCore.QObject):
    def __init__(self,window):
        super().__init__(window)
        self.window=window;self.start=None;self.names=[];self.last=0
        from .surface_picker import SurfacePicker
        self.surface_picker=SurfacePicker(window)
        self.hover=None
        window.plot.interactor.installEventFilter(self)
        self.panel=W.QTreeWidget();self.panel.setHeaderLabels(['Medição','m'])
        self.dock=W.QDockWidget('Ferramentas de medição',window);self.dock.setWidget(self.panel)
        window.addDockWidget(QtCore.Qt.LeftDockWidgetArea,self.dock);self.dock.hide()
    def clear(self):
        for name in self.names:self.window.plot.remove_actor(name,render=False)
        self.names=[];self.start=None;self.panel.clear();self.window.plot.remove_actor('measure-snap',render=False);self.window.plot.render()
    def pick(self,event):
        widget=self.window.plot.interactor;rw=widget.GetRenderWindow();width,height=rw.GetSize()
        x=event.position().x()*width/widget.width();y=(widget.height()-event.position().y())*height/widget.height()
        if self.window.cube.contains(x,y):return None
        return self.surface_picker.pick(x,y)
    def eventFilter(self,obj,event):
        if not self.window.measure_mode:return False
        kind=event.type()
        if kind==QtCore.QEvent.KeyPress and event.key()==QtCore.Qt.Key_Escape:
            self.start=None;self.window.measure_mode=False;self.window.plot.remove_actor('measure-snap');self.window.plot.interactor.unsetCursor();return True
        if kind==QtCore.QEvent.MouseButtonPress and event.button()==QtCore.Qt.LeftButton:
            point=self.pick(event)
            if point is not None:
                if self.start is None:self.clear();self.start=point;self.dock.show()
                else:self.draw(point);self.start=None
            return True
        if kind==QtCore.QEvent.MouseButtonRelease and event.button()==QtCore.Qt.LeftButton:return True
        if kind==QtCore.QEvent.MouseMove:
            if time.monotonic()-self.last>.025:
                point=self.pick(event)
                self.hover=point
                self.window.plot.interactor.setCursor(QtCore.Qt.CrossCursor if point is not None else QtCore.Qt.ForbiddenCursor)
                self.window.plot.remove_actor('measure-snap',render=False)
                if point is not None:
                    self.window.plot.add_points([point],name='measure-snap',color='#ffb000',point_size=12,render_points_as_spheres=True,pickable=False,reset_camera=False,render=False)
                    if self.start is not None:self.draw(point)
                    else:self.window.plot.render()
                self.last=time.monotonic()
            return True
        return False
    def draw(self,end):
        if self.start is None:return
        a=self.start;b=np.array(end);delta=b-a;distance=float(np.linalg.norm(delta));plot=self.window.plot
        for name in self.names:plot.remove_actor(name,render=False)
        self.names=[]
        def line(name,p,q,color,width):
            if np.linalg.norm(q-p)>1e-10:
                plot.add_mesh(pv.Line(p,q),name=name,color=color,line_width=width,pickable=False,reset_camera=False,render=False);self.names.append(name)
        def label(name,p,text,color):
            plot.add_point_labels([p],[text],name=name,text_color='white',shape_color=color,shape_opacity=.9,font_size=13,point_size=0,always_visible=True,reset_camera=False,render=False,pickable=False)
            self.names.extend([name+'-labels',name+'-points'])
        line('dimension-total',a,b,'#efa500',3);label('dimension-total-text',(a+b)/2,f'{distance:.3f} m','#d88a00')
        points=[a,np.array([b[0],a[1],a[2]]),np.array([b[0],b[1],a[2]]),b]
        for i,(axis,color) in enumerate(zip('XYZ',['#d72b28','#26a52b','#155bea'])):
            line('dimension-'+axis,points[i],points[i+1],color,2)
            label('dimension-label-'+axis,(points[i]+points[i+1])/2,f'{axis} {abs(delta[i]):.3f} m',color)
        self.panel.clear()
        for title,values in [('Início',a),('Fim',b),('Diferença X/Y/Z',delta)]:
            parent=W.QTreeWidgetItem(self.panel,[title])
            for axis,value in zip('XYZ',values):W.QTreeWidgetItem(parent,[axis,f'{value:.4f}'])
            parent.setExpanded(True)
        W.QTreeWidgetItem(self.panel,['Distância',f'{distance:.4f}'])
        self.last_result={'start':a.tolist(),'end':b.tolist(),'delta':delta.tolist(),'distance':distance}
        plot.render()
