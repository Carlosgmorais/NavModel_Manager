"""Clickable CAD cube rendered in VTK's orientation layer, above the native OpenGL scene."""
import numpy as np
from PySide6 import QtCore

class NavigationCube(QtCore.QObject):
    def __init__(self, window, marker):
        super().__init__(window)
        self.window = window
        self.marker = marker
        self.interactor = window.plot.interactor
        self.interactor.installEventFilter(self)
        self.pressed = False
        renderers = self.interactor.GetRenderWindow().GetRenderers()
        self.renderer = next(renderers.GetItemAsObject(i) for i in range(renderers.GetNumberOfItems())
                             if renderers.GetItemAsObject(i).HasViewProp(marker.GetOrientationMarker()))

    def isVisible(self): return bool(self.marker.GetEnabled())
    def setVisible(self, visible):
        self.marker.SetEnabled(bool(visible)); self.window.plot.render()

    def event_position(self,event):
        size=self.interactor.GetRenderWindow().GetSize()
        return (event.position().x()*size[0]/max(1,self.interactor.width()),
                (self.interactor.height()-event.position().y())*size[1]/max(1,self.interactor.height()))

    def contains(self,x,y):
        rw=self.interactor.GetRenderWindow(); width,height=rw.GetSize()
        a,b,c,d=self.marker.GetViewport()
        return self.isVisible() and a*width<=x<=c*width and b*height<=y<=d*height

    def direction_at(self,x,y):
        # Unproject a display ray in the orientation renderer; intersect the cube.
        renderer=self.renderer
        def unproject(z):
            renderer.SetDisplayPoint(x,y,z); renderer.DisplayToWorld()
            p=np.array(renderer.GetWorldPoint()); return p[:3]/p[3]
        origin=unproject(0); delta=unproject(1)-origin
        near=-np.inf; far=np.inf; axis=0
        for i in range(3):
            if abs(delta[i])<1e-10:
                if abs(origin[i])>.501:return None
                continue
            t1=(-.5-origin[i])/delta[i];t2=(.5-origin[i])/delta[i]
            lo,hi=sorted((t1,t2))
            if lo>near:near=lo;axis=i
            far=min(far,hi)
        if far<max(0,near):return None
        hit=origin+near*delta
        direction=np.where(hit>.28,1,np.where(hit<-.28,-1,0))
        direction[axis]=1 if hit[axis]>0 else -1
        return tuple(int(x) for x in direction)

    def eventFilter(self,obj,event):
        kind=event.type()
        if kind not in (QtCore.QEvent.MouseButtonPress,QtCore.QEvent.MouseButtonRelease,QtCore.QEvent.MouseButtonDblClick,QtCore.QEvent.MouseMove):
            return False
        x,y=self.event_position(event)
        inside=self.contains(x,y)
        if kind==QtCore.QEvent.MouseMove and inside:
            self.interactor.setToolTip('Cubo CAD: clique nas faces, arestas e cantos; duplo clique: isométrica')
            return True
        if kind==QtCore.QEvent.MouseButtonPress and inside and event.button()==QtCore.Qt.LeftButton:
            self.pressed=True; return True
        if kind==QtCore.QEvent.MouseButtonDblClick and inside:
            self.orient((1,-1,1));return True
        if kind==QtCore.QEvent.MouseButtonRelease and self.pressed:
            self.pressed=False
            direction=self.direction_at(x,y) if inside else None
            if direction:self.orient(direction)
            return True
        return False

    def orient(self,direction):
        self.window.save_camera_for_undo()
        camera=self.window.plot.camera
        focus=np.array(camera.GetFocalPoint());distance=max(camera.GetDistance(),.001)
        vector=np.asarray(direction,dtype=float);vector/=np.linalg.norm(vector)
        camera.SetPosition(*(focus+vector*distance))
        camera.SetViewUp((0,1,0) if abs(vector[2])>.999 else (0,0,1))
        camera.OrthogonalizeViewUp()
        self.window.navigation.update_clipping_range(camera,self.window.plot.renderer,full_reset=True)
        self.window.on_camera_changed(camera);self.window.plot.render()

class CoordinateHUD:
    def __init__(self,plot):
        self.actor=plot.add_text('',position='lower_left',font_size=10,color='#0b3a66',name='coordinate-hud',font='arial')
        self.plot=plot
    def update_coordinates(self,eye,focal):
        x,y,z=eye
        self.actor.SetText(0,f'X: {x:.3f} m   Y: {y:.3f} m   Z: {z:.3f} m\nEL.: {z:.3f} m')
    def isVisible(self):return bool(self.actor.GetVisibility())
    def setVisible(self,visible):self.actor.SetVisibility(visible);self.plot.render()
