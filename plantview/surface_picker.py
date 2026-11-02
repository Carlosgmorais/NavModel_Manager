"""Exact world-space face intersections, including GPU glyph instances."""
import numpy as np
import vtk

class SurfacePicker:
    def __init__(self,window):
        self.window=window;self.cache={}
        self.cell=vtk.vtkCellPicker();self.cell.SetTolerance(.00001);self.cell.PickFromListOn()
    def pick(self,x,y):
        renderer=self.window.plot.renderer
        def world(z):
            renderer.SetDisplayPoint(x,y,z);renderer.DisplayToWorld()
            p=np.array(renderer.GetWorldPoint());return p[:3]/p[3]
        start,end=world(0),world(1);ray=end-start;length=np.dot(ray,ray)
        candidates=[];glyphs=[];self.cell.InitializePickList()
        for model in self.window.models:
            for actor in model['actors']:
                if not actor.GetVisibility() or not actor.GetPickable():continue
                mapper=actor.GetMapper()
                if isinstance(mapper,vtk.vtkGlyph3DMapper):glyphs.append(actor)
                else:self.cell.AddPickList(actor)
        if self.cell.Pick(x,y,0,renderer) and self.cell.GetCellId()>=0:
            p=np.array(self.cell.GetPickPosition());candidates.append((np.dot(p-start,ray)/length,p))
        for actor in glyphs:
            mapper=actor.GetMapper();source=mapper.GetSource(0);cloud=mapper.GetInput()
            if source is None or cloud is None:continue
            key=(source.GetAddressAsString(''),source.GetMTime())
            if key not in self.cache:
                tree=vtk.vtkOBBTree();tree.SetDataSet(source);tree.BuildLocator();self.cache[key]=(tree,source)
            tree,_=self.cache[key]
            inverse=vtk.vtkMatrix4x4();vtk.vtkMatrix4x4.Invert(actor.GetMatrix(),inverse)
            def local(p):
                q=inverse.MultiplyPoint((*p,1));return np.array(q[:3])/q[3]
            a,b=local(start),local(end);direction=b-a
            bounds=np.array(source.GetBounds()).reshape(3,2)
            from vtk.util.numpy_support import vtk_to_numpy
            origins=vtk_to_numpy(cloud.GetPoints().GetData())
            # Vectorized ray/AABB rejection avoids testing every instance's triangles.
            lo=origins+bounds[:,0];hi=origins+bounds[:,1]
            safe=np.where(abs(direction)>1e-15,direction,1e-15)
            t0=(lo-a)/safe;t1=(hi-a)/safe
            valid=np.maximum(np.min([t0,t1],axis=0).max(axis=1),0)<=np.minimum(np.max([t0,t1],axis=0).min(axis=1),1)
            for origin in origins[valid]:
                points=vtk.vtkPoints();ids=vtk.vtkIdList()
                if tree.IntersectWithLine(a-origin,b-origin,points,ids):
                    for i in range(points.GetNumberOfPoints()):
                        q=np.array(points.GetPoint(i))+origin
                        p=np.array(actor.GetMatrix().MultiplyPoint((*q,1)))[:3]
                        t=np.dot(p-start,ray)/length
                        if 0<=t<=1:candidates.append((t,p))
        return min(candidates,key=lambda item:item[0])[1] if candidates else None
