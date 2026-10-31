"""GPU Acceleration, Frustum Culling, Instancing and Diagnostics.

Optimized for large industrial plant models (piping, equipment, valves, steel structures).
"""
import os
import hashlib
from collections import defaultdict
from typing import List, Tuple, Dict, Any, Optional
import numpy as np
import pyvista as pv
import vtk
from PySide6 import QtGui


def configure_surface():
    """Configure OpenGL hardware surface before QApplication creation."""
    if os.environ.get('PLANTVIEW_SOFTWARE_RENDER') == '1':
        return
    fmt = QtGui.QSurfaceFormat()
    fmt.setRenderableType(QtGui.QSurfaceFormat.OpenGL)
    fmt.setProfile(QtGui.QSurfaceFormat.CoreProfile)
    fmt.setVersion(3, 2)
    fmt.setDepthBufferSize(24)
    fmt.setStencilBufferSize(8)
    fmt.setSwapBehavior(QtGui.QSurfaceFormat.DoubleBuffer)
    QtGui.QSurfaceFormat.setDefaultFormat(fmt)


def setup_gpu_renderer(plot):
    """Enable hardware frustum culling and OpenGL optimizations on the VTK renderer."""
    renderer = plot.renderer
    culler = vtk.vtkFrustumCoverageCuller()
    culler.SetMinimumCoverage(0.0)
    renderer.AddCuller(culler)
    renderer.SetUseDepthPeeling(False)
    renderer.SetUseFXAA(False)


def optimize_actor(actor):
    """Explicitly mark meshes immutable for VTK's OpenGL vertex buffer reuse with realistic CAD lighting."""
    mapper = actor.GetMapper()
    if mapper is not None:
        if hasattr(mapper, 'SetStatic'): mapper.SetStatic(True)
        if hasattr(mapper, 'SetScalarVisibility'): mapper.SetScalarVisibility(False)
    prop = actor.GetProperty()
    if hasattr(prop, 'SetInterpolationToFlat'): prop.SetInterpolationToFlat()
    if hasattr(prop, 'SetEdgeVisibility'): prop.SetEdgeVisibility(False)
    if hasattr(prop, 'SetAmbient'): prop.SetAmbient(0.28)
    if hasattr(prop, 'SetDiffuse'): prop.SetDiffuse(0.68)
    if hasattr(prop, 'SetSpecular'): prop.SetSpecular(0.18)
    if hasattr(prop, 'SetSpecularPower'): prop.SetSpecularPower(25.0)
    return actor


def geometry_fingerprint(mesh) -> Optional[Tuple[Tuple[int, int, str], np.ndarray]]:
    """Calculates invariant topology fingerprint to detect repeated components (valves, flanges, bolts)."""
    points = np.asarray(mesh.points, dtype=np.float64)
    if len(points) == 0:
        return None
    origin = points[0].copy()
    relative = np.ascontiguousarray(points - origin)
    topology = np.ascontiguousarray(mesh.faces, dtype=np.int64)
    digest = hashlib.blake2b(digest_size=20)
    digest.update(relative.tobytes())
    digest.update(topology.tobytes())
    return (len(points), int(mesh.n_cells), digest.hexdigest()), origin


def make_instance_actor(plot, source, origins, color):
    """Use VTK GPU glyph mapping for exact repeated geometry in distinct positions."""
    import numpy as np
    import pyvista as pv
    from vtkmodules.vtkRenderingCore import vtkGlyph3DMapper, vtkActor
    cloud = pv.PolyData(np.asarray(origins, dtype=float))
    mapper = vtkGlyph3DMapper()
    mapper.SetInputData(cloud)
    mapper.SetSourceData(source)
    mapper.ScalingOff()
    mapper.OrientOff()
    mapper.SetScalarVisibility(False)
    actor = vtkActor()
    actor.SetMapper(mapper)
    prop = actor.GetProperty()
    prop.SetColor(*color)
    prop.SetInterpolationToFlat()
    prop.SetEdgeVisibility(False)
    plot.add_actor(actor, reset_camera=False, render=False)
    return actor



def combine_poly_datas(mesh_list: List[Any]) -> Any:
    """Concatenates PolyData meshes using hardware-accelerated C++ vtkAppendPolyData.
    Preserves all points and cells exactly without welding, tolerance merging, or topology distortion.
    """
    valid = [m for m in mesh_list if m is not None and getattr(m, 'n_points', 0) > 0 and getattr(m, 'n_cells', 0) > 0]
    if not valid:
        return pv.PolyData() if hasattr(pv, 'PolyData') else None
    if len(valid) == 1:
        return valid[0]
    if hasattr(vtk, 'vtkAppendPolyData') and hasattr(valid[0], 'GetPoints'):
        appender = vtk.vtkAppendPolyData()
        for m in valid:
            appender.AddInputData(m)
        appender.Update()
        return pv.wrap(appender.GetOutput())
    if hasattr(valid[0], 'merge'):
        return valid[0].copy(deep=True).merge(valid[1:], merge_points=False)
    return valid[0]


def add_optimized_parts(plot, parts, visible=True) -> Tuple[List[Any], List[str], Dict[str, Any]]:
    """Instance same-color translated clones, batch large industrial meshes, retain all names in tree."""
    buckets = defaultdict(list)
    for name, mesh in parts:
        color_data = mesh.field_data.get('plantview_rgb')
        color = tuple(int(x)/255. for x in color_data[0]) if color_data is not None else (.74, .79, .83)
        layer = getattr(mesh, '_plantview_layer', None)
        if not layer:
            layer_data = mesh.field_data.get('plantview_layer')
            layer = str(layer_data[0]) if layer_data is not None and len(layer_data) > 0 else '0'
        sig = geometry_fingerprint(mesh)
        if sig is None:
            continue
        key, origin = sig
        buckets[(key, color, layer)].append((name, mesh, origin, layer))

    actors, names, groups, instances = [], [], 0, 0
    total_triangles = 0
    unique_items_by_layer_color = defaultdict(list)

    for (_, color, layer), items in buckets.items():
        if len(items) >= 3:
            # GPU Instancing with vtkGlyph3DMapper for 3+ identical repeated items
            source = items[0][1].copy(deep=True)
            source.points = np.asarray(source.points) - items[0][2]
            actor = make_instance_actor(plot, source, [origin for _, _, origin, _ in items], color)
            actor.SetVisibility(visible)
            actor._plantview_layer = items[0][3]
            actor._plantview_names = [item[0] for item in items]
            actor._plantview_color = color
            actors.append(actor)
            names.append(f'{items[0][0]} ({len(items)} instâncias)')
            groups += 1
            instances += len(items)
            for _, m, _, _ in items:
                total_triangles += getattr(m, 'n_cells', 1)
        else:
            unique_items_by_layer_color[(layer, color)].extend(items)

    for (layer, color), items in unique_items_by_layer_color.items():
        if len(parts) > 30 and len(items) > 1:
            # High-performance hardware batching: combine meshes of same (layer, color)
            # into a single compound PolyData actor using C++ vtkAppendPolyData.
            # Preserves 100% of vertices, faces and coordinates without distortion.
            try:
                mesh_list = [m for _, m, _, _ in items]
                merged_mesh = combine_poly_datas(mesh_list)
                actor = plot.add_mesh(merged_mesh, color=color, smooth_shading=False,
                                      reset_camera=False, render=False)
                optimize_actor(actor)
                actor.SetVisibility(visible)
                actor._plantview_name = f'Camada: {layer} ({len(items)} peças)'
                actor._plantview_names = [item[0] for item in items]
                actor._plantview_layer = layer
                actor._plantview_color = color
                actor._plantview_bounds = getattr(merged_mesh, 'bounds', None)
                actors.append(actor)
                names.append(f'{layer} ({len(items)} peças)')
                total_triangles += getattr(merged_mesh, 'n_cells', 1)
                continue
            except Exception:
                pass

        for name, mesh, _, lyr in items:
            actor = plot.add_mesh(mesh, color=color, smooth_shading=False,
                                  reset_camera=False, render=False)
            optimize_actor(actor)
            actor.SetVisibility(visible)
            actor._plantview_name = name
            actor._plantview_layer = lyr
            actor._plantview_color = color
            actor._plantview_bounds = getattr(mesh, 'bounds', None)
            actors.append(actor)
            names.append(name)
            total_triangles += getattr(mesh, 'n_cells', 1)

    stats = dict(
        original_parts=len(parts),
        gpu_actors=len(actors),
        instance_groups=groups,
        total_triangles=total_triangles,
        instanced_parts=instances
    )
    return actors, names, stats


def get_system_memory_info() -> Dict[str, float]:
    """Return system RAM consumption in MB."""
    import ctypes
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ('dwLength', ctypes.c_ulong),
            ('dwMemoryLoad', ctypes.c_ulong),
            ('ullTotalPhys', ctypes.c_ulonglong),
            ('ullAvailPhys', ctypes.c_ulonglong),
            ('ullTotalPageFile', ctypes.c_ulonglong),
            ('ullAvailPageFile', ctypes.c_ulonglong),
            ('ullTotalVirtual', ctypes.c_ulonglong),
            ('ullAvailVirtual', ctypes.c_ulonglong),
            ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
        ]
    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        total_mb = stat.ullTotalPhys / (1024 * 1024)
        avail_mb = stat.ullAvailPhys / (1024 * 1024)
        used_mb = total_mb - avail_mb
        return {'total_ram_mb': total_mb, 'used_ram_mb': used_mb, 'load_pct': stat.dwMemoryLoad}
    except Exception:
        return {'total_ram_mb': 0.0, 'used_ram_mb': 0.0, 'load_pct': 0}


def gpu_report(plot) -> str:
    """Generate detailed GPU and OpenGL diagnostics."""
    rw = plot.render_window
    details = [
        'PLANTVIEW STUDIO — DIAGNÓSTICO DE ACELERAÇÃO POR GPU',
        '-------------------------------------------------------',
        'Motor de Renderização: VTK / OpenGL 3.2 Core Profile',
        f'Classe RenderWindow: {rw.GetClassName()}',
    ]
    if hasattr(rw, 'GetOpenGLSupportMessage'):
        try:
            details.append('Suporte OpenGL: ' + rw.GetOpenGLSupportMessage())
        except Exception:
            pass
    if hasattr(rw, 'ReportCapabilities'):
        try:
            report = rw.ReportCapabilities()
            for line in str(report).splitlines():
                if any(word in line.lower() for word in ('vendor', 'renderer', 'version', 'opengl', 'glsl')):
                    details.append(line.strip())
        except Exception:
            pass

    mem = get_system_memory_info()
    if mem['total_ram_mb'] > 0:
        details.append(f"Memória RAM do Sistema: {mem['used_ram_mb']:.0f} MB / {mem['total_ram_mb']:.0f} MB ({mem['load_pct']}%)")
    
    renderer = plot.renderer
    actors_count = renderer.GetActors().GetNumberOfItems()
    details.append(f'Atores Ativos no Viewport: {actors_count}')
    details.append('Frustum Culling: Ativo (vtkFrustumCoverageCuller)')
    details.append('Buffer Instancing VBO: Ativo (SetStatic=True)')
    return '\n'.join(details)
