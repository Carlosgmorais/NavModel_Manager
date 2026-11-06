"""Offline publication of the geometry the application actually renders.

No CAD conversion runs here. Geometry is read from model actors, including the
translation-only glyph instances produced by gpu.py. The portable host is a
self-contained Windows executable with an appended, compressed ZIP package.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
import struct
import tempfile
import zipfile
from dataclasses import asdict, dataclass

import numpy as np
import vtk
from vtk.util.numpy_support import vtk_to_numpy

from .branding import ROOT, VERSION

WEB_ROOT = ROOT / 'resources' / 'web'
FOOTER_MAGIC = b'NVMWEB01'
FOOTER_SIZE = 48


@dataclass
class PublishLimits:
    """Conservative, user-adjustable limits; estimates are not RAM guarantees."""
    max_objects: int = 20_000
    max_triangles: int = 1_500_000
    max_html_mib: int = 80
    max_memory_mib: int = 768


@dataclass
class SceneAnalysis:
    objects: int
    actors: int
    triangles: int
    vertices: int
    source_bytes: int
    geometry_bytes: int
    html_bytes: int
    memory_bytes: int
    bounds: list
    html_allowed: bool
    reasons: list


def _matrix(actor):
    m = actor.GetMatrix()
    return np.array([[m.GetElement(i, j) for j in range(4)] for i in range(4)], dtype=np.float64)


def _mesh(actor):
    mapper = actor.GetMapper()
    if mapper is None:
        return None, None
    mapper.Update()
    if isinstance(mapper, vtk.vtkGlyph3DMapper):
        if mapper.GetScaling() or mapper.GetOrient():
            raise ValueError('A publicação aceita as instâncias transladadas do NavModel; esta instância contém escala/orientação não suportada.')
        data, cloud = mapper.GetSource(0), mapper.GetInput()
        if cloud is None or cloud.GetPoints() is None:
            return None, None
        return data, vtk_to_numpy(cloud.GetPoints().GetData())
    data = mapper.GetInput()
    if data is not None and not isinstance(data, vtk.vtkPolyData):
        surface = vtk.vtkDataSetSurfaceFilter()
        surface.SetInputData(data)
        surface.Update()
        data = surface.GetOutput()
    return data, None


def _triangle_count(data):
    # n-gons -> n-2, and triangle strips -> n-2, without triangulating a copy.
    total = 0
    for cells in (data.GetPolys(), data.GetStrips()):
        if cells is not None and cells.GetNumberOfCells():
            sizes = np.diff(vtk_to_numpy(cells.GetOffsetsArray()))
            total += int(np.maximum(sizes - 2, 0).sum())
    return total


def analyze_scene(models, limits=None):
    limits = limits or PublishLimits()
    objects = actors = triangles = vertices = geometry_bytes = source_bytes = 0
    lo = np.full(3, np.inf); hi = np.full(3, -np.inf)
    for model in models:
        path = Path(model.get('path', ''))
        if path.is_file():
            source_bytes += path.stat().st_size
        for actor in model.get('actors', []):
            data, origins = _mesh(actor)
            if data is None or not data.GetNumberOfPoints():
                continue
            actors += 1
            instances = len(origins) if origins is not None else 1
            objects += max(instances, len(getattr(actor, '_plantview_names', [])), 1)
            nt = _triangle_count(data)
            triangles += nt * instances
            vertices += data.GetNumberOfPoints() * instances
            # Float64 source coordinates, uint32 connectivity; one shared source
            # per glyph group, rather than exploding all identical geometry.
            geometry_bytes += data.GetNumberOfPoints() * 24 + nt * 12 + data.GetNumberOfLines() * 16 + instances * 24
            b = np.array(actor.GetBounds()).reshape(3, 2)
            lo = np.minimum(lo, b[:, 0]); hi = np.maximum(hi, b[:, 1])
    if not actors:
        lo = np.zeros(3); hi = np.ones(3)
    # Include JS, base64 expansion, JSON/metadata, decoded arrays, normals, GPU
    # buffers, draw-call overhead and a browser baseline. Browser/GPU dependent.
    html_bytes = int(1_200_000 + geometry_bytes * 1.45 + objects * 320)
    memory_bytes = int(128 * 2**20 + geometry_bytes * 5 + triangles * 12 + actors * 24_576 + objects * 256)
    reasons = []
    for actual, maximum, title in [
        (objects, limits.max_objects, 'quantidade de objetos'),
        (triangles, limits.max_triangles, 'quantidade de triângulos'),
        (html_bytes, limits.max_html_mib * 2**20, 'tamanho estimado do HTML'),
        (memory_bytes, limits.max_memory_mib * 2**20, 'memória estimada no navegador'),
    ]:
        if actual > maximum:
            reasons.append(title)
    bounds = [float(v) for pair in zip(lo, hi) for v in pair]
    return SceneAnalysis(objects, actors, triangles, vertices, source_bytes, geometry_bytes,
                         html_bytes, memory_bytes, bounds, actors > 0 and not reasons, reasons)


def _safe_json(value):
    # Prevent a CAD name/property from ending an inline script in offline HTML.
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def _properties(data):
    result = {}
    fields = data.GetFieldData()
    for i in range(fields.GetNumberOfArrays()):
        arr = fields.GetAbstractArray(i)
        if arr is None:
            continue
        name = arr.GetName() or str(i)
        # Field data available in the renderer only; avoid huge per-vertex arrays.
        values = [arr.GetVariantValue(j).ToString() for j in range(min(arr.GetNumberOfValues(), 100))]
        result[name] = values if len(values) != 1 else values[0]
    return result


def _binary_parts(data, max_cells=200_000):
    tri = vtk.vtkTriangleFilter(); tri.SetInputData(data); tri.PassLinesOn(); tri.PassVertsOn(); tri.Update()
    poly = tri.GetOutput()
    points = vtk_to_numpy(poly.GetPoints().GetData())
    polys = vtk_to_numpy(poly.GetPolys().GetConnectivityArray()).reshape(-1, 3)
    line_offsets = vtk_to_numpy(poly.GetLines().GetOffsetsArray())
    line_ids = vtk_to_numpy(poly.GetLines().GetConnectivityArray())
    segments = []
    for a, b in zip(line_offsets[:-1], line_offsets[1:]):
        ids = line_ids[a:b]
        if len(ids) > 1:
            segments.append(np.column_stack((ids[:-1], ids[1:])).ravel())
    lines = np.concatenate(segments).reshape(-1, 2) if segments else np.empty((0, 2), dtype=np.int64)
    verts = vtk_to_numpy(poly.GetVerts().GetConnectivityArray()).reshape(-1, 1)
    if not len(polys) and not len(lines) and not len(verts):
        verts = np.arange(len(points)).reshape(-1, 1)
    for cells, kind in ((polys, 'surface'), (lines, 'lines'), (verts, 'points')):
        for start in range(0, len(cells), max_cells):
            indices = cells[start:start + max_cells].ravel()
            if kind == 'points':
                positions = np.asarray(points[indices], dtype=np.float64)
                connectivity = np.empty(0, dtype='<u4')
            else:
                unique, inverse = np.unique(indices, return_inverse=True)
                positions = np.asarray(points[unique], dtype=np.float64)
                connectivity = inverse.astype('<u4', copy=False)
            if not np.isfinite(positions).all():
                raise ValueError('A geometria contém coordenadas inválidas.')
            origin = (positions.min(axis=0) + positions.max(axis=0)) * .5
            positions -= origin
            header = struct.pack('<4sIII', b'NVM1', len(positions),
                                 len(connectivity) if kind == 'surface' else 0,
                                 len(connectivity) if kind == 'lines' else 0)
            yield header + positions.astype('<f8', copy=False).tobytes() + connectivity.tobytes(), origin.tolist()


def _viewer_html(manifest, inline_chunks=None):
    template = (WEB_ROOT / 'viewer.html').read_text(encoding='utf-8')
    script = (WEB_ROOT / 'viewer.js').read_text(encoding='utf-8')
    three = (WEB_ROOT / 'three.min.js').read_text(encoding='utf-8')
    logo = ROOT / 'resources' / 'logo-white.png'
    logo_url = 'data:image/png;base64,' + base64.b64encode(logo.read_bytes()).decode('ascii') if logo.is_file() else ''
    replacements = {'LOGO': logo_url, 'THREE': three.replace('</script', '<\\/script'),
                    'MANIFEST': _safe_json(manifest), 'CHUNKS': _safe_json(inline_chunks),
                    'VIEWER': script}
    return re.sub(r'@@([A-Z]+)@@', lambda match: replacements[match[1]], template)


def export_scene(models, destination, kind='html', limits=None, camera=None,
                 clipping=None, progress=None, cancelled=None, host_path=None):
    """Write atomically. No converter, external service, or runtime CDN required."""
    if kind not in ('html', 'exe'):
        raise ValueError('Formato de publicação desconhecido.')
    limits = limits or PublishLimits()
    analysis = analyze_scene(models, limits)
    if not analysis.actors:
        raise ValueError('Nenhuma geometria renderizada disponível para publicar.')
    if kind == 'html' and not analysis.html_allowed:
        raise ValueError('HTML desabilitado pelos limites: ' + ', '.join(analysis.reasons))
    host = Path(host_path or WEB_ROOT / 'NavModelWebHost.exe')
    if kind == 'exe' and not host.is_file():
        raise FileNotFoundError('O componente portátil NavModelWebHost.exe não está instalado.')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    bounds = np.array(analysis.bounds).reshape(3, 2)
    origin = bounds.mean(axis=1)
    manifest = {'format': 'navmodel-web-1', 'version': VERSION, 'units': 'm',
                'analysis': asdict(analysis), 'origin': origin.tolist(), 'bounds': analysis.bounds,
                'camera': camera, 'clipping': clipping or [], 'models': [], 'actors': [],
                'limits': asdict(limits), 'granularity': 'Mesmo agrupamento e propriedades disponíveis no NavModel Manager.'}
    count = 0
    with tempfile.TemporaryDirectory(prefix='navmodel-publish-', dir=destination.parent) as scratch:
        scratch = Path(scratch)
        package = scratch / 'package.zip'
        chunks = {} if kind == 'html' else None
        with zipfile.ZipFile(package, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for mi, model in enumerate(models):
                source_name = Path(model.get('path', f'Modelo {mi + 1}')).name
                diagnostics = [line for line in model.get('import_warnings', [])
                               if any(key in line for key in ('Status de Cobertura', 'não reconstruídas', 'ignorada(s)', 'AVISO CRÍTICO'))]
                manifest['models'].append({'name': source_name, 'visible': bool(model.get('visible', True)),
                                           'diagnostics': diagnostics[:25]})
                for actor in model.get('actors', []):
                    if cancelled and cancelled():
                        raise InterruptedError('Publicação cancelada.')
                    data, origins = _mesh(actor)
                    if data is None or not data.GetNumberOfPoints():
                        continue
                    parts = []
                    for number, (binary, local_origin) in enumerate(_binary_parts(data)):
                        if cancelled and cancelled():
                            raise InterruptedError('Publicação cancelada.')
                        chunk_name = f'chunks/{count:07d}-{number:04d}.bin'
                        archive.writestr(chunk_name, binary)
                        if chunks is not None:
                            chunks[chunk_name] = base64.b64encode(binary).decode('ascii')
                        parts.append({'chunk': chunk_name, 'localOrigin': local_origin})
                    names = list(getattr(actor, '_plantview_names', []))
                    record = {'id': count, 'model': mi, 'name': getattr(actor, '_plantview_name', names[0] if names else f'Componente {count + 1}'),
                              'names': names, 'layer': str(getattr(actor, '_plantview_layer', '0')),
                              'visible': bool(actor.GetVisibility()), 'color': list(actor.GetProperty().GetColor()),
                              'opacity': float(actor.GetProperty().GetOpacity()), 'matrix': _matrix(actor).tolist(),
                              'parts': parts, 'bounds': list(actor.GetBounds()),
                              'properties': _properties(data), 'instances': None}
                    if origins is not None:
                        # Binary origins avoid multi-megabyte JSON arrays and remain exact doubles.
                        instances_name = f'chunks/{count:07d}-instances.bin'
                        instance_binary = np.ascontiguousarray(origins, dtype='<f8').tobytes()
                        archive.writestr(instances_name, instance_binary)
                        if chunks is not None:
                            chunks[instances_name] = base64.b64encode(instance_binary).decode('ascii')
                        record['instances'] = {'chunk': instances_name, 'count': len(origins)}
                    manifest['actors'].append(record)
                    count += 1
                    if progress:
                        progress(count, analysis.actors)
            html = _viewer_html(manifest, chunks)
            if kind == 'html':
                staged = scratch / 'result.html'
                staged.write_text(html, encoding='utf-8')
                if staged.stat().st_size > limits.max_html_mib * 2**20:
                    raise ValueError('O HTML final excedeu o limite configurado. Publique como EXE portátil.')
            else:
                archive.writestr('index.html', html.encode('utf-8'))
                archive.writestr('manifest.json', _safe_json(manifest).encode('utf-8'))
                archive.writestr('THREE-LICENSE.txt', (WEB_ROOT / 'THREE-LICENSE.txt').read_bytes())
        if kind == 'exe':
            staged = scratch / 'result.exe'
            digest = hashlib.sha256()
            with staged.open('wb') as out, host.open('rb') as stub, package.open('rb') as payload:
                shutil.copyfileobj(stub, out, 2**20)
                while block := payload.read(2**20):
                    digest.update(block); out.write(block)
                out.write(FOOTER_MAGIC + struct.pack('<Q', package.stat().st_size) + digest.digest())
        os.replace(staged, destination)
    return {'path': str(destination), 'bytes': destination.stat().st_size, 'analysis': asdict(analysis), 'kind': kind}


def read_portable_package(path):
    """Validation helper; production host streams the package without extracting."""
    import io
    with Path(path).open('rb') as stream:
        stream.seek(-FOOTER_SIZE, 2)
        footer = stream.read(FOOTER_SIZE)
        if footer[:8] != FOOTER_MAGIC:
            raise ValueError('Pacote NavModel inválido.')
        size = struct.unpack('<Q', footer[8:16])[0]
        stream.seek(-(FOOTER_SIZE + size), 2)
        data = stream.read(size)
        if hashlib.sha256(data).digest() != footer[16:]:
            raise ValueError('Pacote NavModel corrompido.')
    return zipfile.ZipFile(io.BytesIO(data))


def show_publish_dialog(window):
    """Modal scene snapshot + worker export; modelling cannot change underneath it."""
    from PySide6 import QtCore, QtWidgets as W

    class ExportWorker(QtCore.QThread):
        progressed = QtCore.Signal(int, int)
        done = QtCore.Signal(object)
        failed = QtCore.Signal(str)

        def run(self):
            try:
                result = export_scene(window.models, self.path, self.kind, limits=self.limits,
                                      camera=camera, clipping=clip, progress=self.progressed.emit,
                                      cancelled=self.isInterruptionRequested)
                self.done.emit(result)
            except Exception as exc:
                self.failed.emit(str(exc))

    class PublishDialog(W.QDialog):
        def reject(self):
            if getattr(self, 'worker', None) and self.worker.isRunning():
                self.worker.requestInterruption()
                return
            super().reject()

    dialog = PublishDialog(window); dialog.setWindowTitle('Publicar Maquete Web — NavModel Manager')
    dialog.resize(610, 550)
    layout = W.QVBoxLayout(dialog)
    description = W.QLabel('Publique a geometria carregada, com posicionamento, cores, hierarquia e propriedades disponíveis.\nO destinatário abre um único arquivo; o NavModel Manager não é necessário.')
    description.setWordWrap(True); layout.addWidget(description)
    summary = W.QLabel(); summary.setWordWrap(True); layout.addWidget(summary)
    group = W.QGroupBox('Limites para HTML offline (ajustáveis)'); form = W.QFormLayout(group)
    controls = []
    for label, default, maximum in [('Objetos', 20000, 1000000), ('Triângulos', 1500000, 100000000), ('HTML estimado (MiB)', 80, 2048), ('Memória estimada (MiB)', 768, 65536)]:
        control = W.QSpinBox(); control.setRange(1, maximum); control.setValue(default)
        form.addRow(label, control); controls.append(control)
    layout.addWidget(group)
    note = W.QLabel('As estimativas incluem geometria, navegador e GPU; não são garantia de desempenho.\nO EXE reduz o pico de carregamento, mas a maquete completa ainda precisa caber na memória do destinatário.\nO EXE é Windows x64, abre apenas um servidor em 127.0.0.1 e encerra após fechar o visualizador. Por não estar assinado, o Windows/antivírus pode pedir confirmação ou bloquear conforme a política da empresa. Não desative as proteções.')
    note.setWordWrap(True); layout.addWidget(note)
    status = W.QLabel(); status.setWordWrap(True); layout.addWidget(status)
    progress = W.QProgressBar(); progress.hide(); layout.addWidget(progress)
    row = W.QHBoxLayout(); html_button = W.QPushButton('Salvar HTML único'); exe_button = W.QPushButton('Salvar EXE portátil'); close = W.QPushButton('Fechar')
    for button in (html_button, exe_button, close): row.addWidget(button)
    layout.addLayout(row); close.clicked.connect(dialog.reject)

    def limits(): return PublishLimits(*(c.value() for c in controls))

    def refresh():
        try:
            a = analyze_scene(window.models, limits())
            summary.setText(f'{a.objects:,} objetos • {a.actors:,} grupos • {a.triangles:,} triângulos\nArquivos de origem: {a.source_bytes / 2**20:.1f} MiB • Geometria: {a.geometry_bytes / 2**20:.1f} MiB\nHTML estimado: {a.html_bytes / 2**20:.1f} MiB • Memória estimada: {a.memory_bytes / 2**20:.0f} MiB')
            html_button.setEnabled(a.html_allowed); exe_button.setEnabled(a.actors > 0 and (WEB_ROOT / 'NavModelWebHost.exe').is_file())
            status.setText('HTML disponível para esta maquete.' if a.html_allowed else 'HTML desabilitado: ' + (', '.join(a.reasons) or 'nenhuma geometria carregada') + '. Use o EXE portátil.')
        except Exception as exc:
            html_button.setEnabled(False); exe_button.setEnabled(False); status.setText(str(exc))

    for c in controls: c.valueChanged.connect(refresh)
    camera_object = window.plot.camera
    camera = {'position': list(camera_object.GetPosition()), 'target': list(camera_object.GetFocalPoint()), 'up': list(camera_object.GetViewUp()), 'parallel': bool(camera_object.GetParallelProjection()), 'scale': float(camera_object.GetParallelScale())}
    tool = getattr(window, 'section_tool', None)
    clip = [list(map(float, equation)) for equation in tool.clipping_equations()] if tool and hasattr(tool, 'clipping_equations') else []

    def start(kind):
        path, _ = W.QFileDialog.getSaveFileName(dialog, 'Publicar Maquete Web', f'Maquete_NavModel.{kind}', f'Arquivo {kind.upper()} (*.{kind})')
        if not path: return
        if not path.lower().endswith('.' + kind): path += '.' + kind
        for c in controls: c.setEnabled(False)
        html_button.setEnabled(False); exe_button.setEnabled(False); progress.show(); close.setText('Cancelar')
        # The export worker reads VTK datasets. Do not render them concurrently.
        timer = window.plot.render_timer
        was_rendering = timer.isActive()
        timer.stop()
        worker = ExportWorker(dialog); worker.path = path; worker.kind = kind; worker.limits = limits(); dialog.worker = worker
        worker.progressed.connect(lambda value, total: (progress.setMaximum(total), progress.setValue(value)))

        def finished(result=None, error=None):
            if was_rendering:
                timer.start()
            for c in controls: c.setEnabled(True)
            close.setText('Fechar'); refresh()
            status.setText(error if error else f'Publicado: {result["path"]}\nTamanho final: {result["bytes"] / 2**20:.1f} MiB. O arquivo pode ser copiado sozinho para outro computador.')
            if not error: progress.setValue(progress.maximum())
        worker.done.connect(lambda result: finished(result=result)); worker.failed.connect(lambda error: finished(error=error)); worker.start()

    html_button.clicked.connect(lambda: start('html')); exe_button.clicked.connect(lambda: start('exe'))
    refresh(); dialog.exec()
