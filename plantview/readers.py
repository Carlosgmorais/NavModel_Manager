"""Readers return geometry in metres, preserving world placement and original CAD colors."""
import json
import hashlib
import shutil
from functools import lru_cache
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from typing import List, Tuple, Callable, Optional, Set
import numpy as np
import pyvista as pv
from .colors import resolve_layer_color, resolve_ifc_color, ACI_TABLE

UNITS = {1: .0254, 2: .3048, 4: .001, 5: .01, 6: 1., 7: 1000.}

def attach_mesh_metadata(mesh: pv.PolyData, layer_name: str, color_rgb=None):
    """Safely attach layer name and color metadata to a mesh without triggering VTK ASCII errors.
    VTK string arrays only support 7-bit ASCII characters. Storing non-ASCII strings
    (e.g., accented characters in Portuguese layer names or filenames) raises:
    ValueError: String array contains non-ASCII characters that are not supported by VTK.
    We attach the exact original layer string as a Python attribute `_plantview_layer`,
    and store an ASCII-normalized version in `field_data['plantview_layer']` for VTK serialization.
    """
    layer_str = str(layer_name) if layer_name is not None else '0'
    mesh._plantview_layer = layer_str
    if color_rgb is not None:
        rgb_arr = np.array([color_rgb], dtype=np.uint8)
        mesh.field_data['plantview_rgb'] = rgb_arr
        mesh._plantview_rgb = rgb_arr
    ascii_layer = unicodedata.normalize('NFKD', layer_str).encode('ascii', 'ignore').decode('ascii')
    if not ascii_layer:
        ascii_layer = '0'
    mesh.field_data['plantview_layer'] = np.array([ascii_layer])

def engine_path():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    cand = root / 'engine' / 'plantview-dwg.exe'
    if cand.is_file():
        return cand
    cand2 = Path(__file__).resolve().parents[1] / 'engine' / 'plantview-dwg.exe'
    if cand2.is_file():
        return cand2
    return cand

def parse_dwg_mesh_info(name: str) -> Tuple[str, str, str]:
    """Parse prefix, handle, and layer from mesh name.
    Examples:
      '31 | 0 #1' -> ('', '31', '0')
      'VALVE/4B | PIPING #2' -> ('VALVE/', '4B', 'PIPING')
    """
    block_prefix = ''
    layer_name = '0'
    handle = ''
    if ' | ' in name:
        left, right = name.split(' | ', 1)
        layer_name = right.split('#')[0].strip()
        if '/' in left:
            block_prefix, handle = left.rsplit('/', 1)
            block_prefix += '/'
        else:
            handle = left.strip()
    else:
        handle = name
    return block_prefix, handle, layer_name

@lru_cache(maxsize=4)
def _engine_signature(path, size, modified):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def triangulate_polygon_3d(points: np.ndarray, indices: List[int]) -> List[int]:
    """Robust Ear-Clipping triangulation for 3D planar polygons (concave, convex, n-gons).
    Preserves openings, holes, and prevents bridging across open pipe bores or concave steel profiles.
    """
    n = len(indices)
    if n < 3:
        return []
    if n == 3:
        return [3, indices[0], indices[1], indices[2]]
    if n == 4:
        return [3, indices[0], indices[1], indices[2], 3, indices[0], indices[2], indices[3]]

    poly_pts = points[indices]
    normal = np.zeros(3)
    for i in range(n):
        p1 = poly_pts[i]
        p2 = poly_pts[(i + 1) % n]
        normal[0] += (p1[1] - p2[1]) * (p1[2] + p2[2])
        normal[1] += (p1[2] - p2[2]) * (p1[0] + p2[0])
        normal[2] += (p1[0] - p2[0]) * (p1[1] + p2[1])

    norm_len = np.linalg.norm(normal)
    if norm_len > 1e-9:
        normal /= norm_len
        axis = np.argmax(np.abs(normal))
        drop_cols = [c for c in [0, 1, 2] if c != axis]
        coords = poly_pts[:, drop_cols]
    else:
        coords = poly_pts[:, :2]

    verts = list(range(n))
    triangles = []
    count = len(verts)
    attempts = 0

    while count > 3 and attempts < count * 2:
        ear_found = False
        for i in range(count):
            p_idx = verts[(i - 1) % count]
            c_idx = verts[i]
            n_idx = verts[(i + 1) % count]
            a, b, c = coords[p_idx], coords[c_idx], coords[n_idx]
            cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            if cross <= 0:
                continue

            any_inside = False
            for k in range(count):
                idx = verts[k]
                if idx in (p_idx, c_idx, n_idx):
                    continue
                pt = coords[idx]
                d1 = (pt[0] - b[0]) * (a[1] - b[1]) - (pt[1] - b[1]) * (a[0] - b[0])
                d2 = (pt[0] - c[0]) * (b[1] - c[1]) - (pt[1] - c[1]) * (b[0] - c[0])
                d3 = (pt[0] - a[0]) * (c[1] - a[1]) - (pt[1] - a[1]) * (c[0] - a[0])
                if (d1 >= 0 and d2 >= 0 and d3 >= 0) or (d1 <= 0 and d2 <= 0 and d3 <= 0):
                    any_inside = True
                    break

            if not any_inside:
                triangles.extend([3, indices[p_idx], indices[c_idx], indices[n_idx]])
                verts.pop(i)
                count -= 1
                ear_found = True
                break
        attempts += 1
        if not ear_found:
            break

    if count == 3:
        triangles.extend([3, indices[verts[0]], indices[verts[1]], indices[verts[2]]])
    elif count > 3:
        for k in range(1, count - 1):
            triangles.extend([3, indices[verts[0]], indices[verts[k]], indices[verts[k + 1]]])

    return triangles


def read_dwg(
    path,
    unit_scale=.001,
    progress_callback: Optional[Callable[[float, str], bool]] = None,
    visited_files: Optional[Set[Path]] = None,
    force_reload: bool = False
):
    engine = engine_path()
    if not engine.is_file():
        raise ValueError(f'Motor plantview-dwg.exe ausente em {engine}. Reextraia o pacote completo ou verifique a pasta engine.')

    source = Path(path).resolve()
    file_name = source.name

    if visited_files is None:
        visited_files = set()
    visited_files.add(source)

    if progress_callback:
        progress_callback(10.0, f'Loading {file_name} ...')

    t_start = time.monotonic()
    source_stat, engine_stat = source.stat(), engine.stat()
    engine_signature = _engine_signature(str(engine), engine_stat.st_size, engine_stat.st_mtime_ns)
    # Cache version v6 incorporates Plant 3D elbows/fittings reconstruction, beam cross-section extrusion, and parallel ACIS tessellation
    cache_key = hashlib.sha256(
        f'v6|{source}|{source_stat.st_size}|{source_stat.st_mtime_ns}|{engine_signature}'.encode()
    ).hexdigest()
    cache_dir = Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'PlantView Studio' / 'Cache'
    cache_file = cache_dir / (cache_key + '.json')

    if force_reload and cache_file.exists():
        cache_file.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix='PlantView-') as folder:
        root = Path(folder)
        result = root / 'geometry.json'

        used_cache = False
        if not force_reload and cache_file.is_file():
            try:
                cached_data = json.loads(cache_file.read_text(encoding='utf-8-sig'))
                cov = cached_data.get('coverage', {})
                # Discard cache if empty or if invalid
                if cov.get('status') == 'empty' or (cov.get('total_solids_found', 0) > 0 and len(cached_data.get('meshes', [])) == 0):
                    cache_file.unlink(missing_ok=True)
                else:
                    shutil.copyfile(cache_file, result)
                    used_cache = True
                    if progress_callback:
                        progress_callback(40.0, f'Loading {file_name} (Cache)...')
            except Exception:
                cache_file.unlink(missing_ok=True)

        if not used_cache:
            if progress_callback:
                progress_callback(25.0, f'Loading {file_name} ...')

            with (root / 'decoder.log').open('w+b') as log:
                proc = subprocess.Popen(
                    [str(engine), str(source), str(result)],
                    cwd=folder, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                started = time.monotonic()
                try:
                    while proc.poll() is None:
                        elapsed = time.monotonic() - started
                        if progress_callback:
                            if progress_callback(25.0 + min(25.0, elapsed * 2.0), f'Loading {file_name} — {int(elapsed)} s decorridos...') is False:
                                raise InterruptedError('Carregamento cancelado pelo usuário.')
                        if elapsed > 300:
                            raise TimeoutError('O leitor DWG excedeu 5 minutos. A decodificação foi encerrada.')
                        time.sleep(0.15)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                log.seek(0, 2)
                log.seek(max(0, log.tell() - 2500))
                decoder_detail = log.read().decode(errors='replace')
            if proc.returncode != 0 or not result.exists():
                detail = decoder_detail
                raise ValueError('Falha no leitor DWG experimental. O original não foi alterado.\n' + detail)

            if progress_callback:
                progress_callback(55.0, f'Loading {file_name} ...')

        try:
            data = json.loads(result.read_text(encoding='utf-8-sig'))
        except (ValueError, OSError):
            cache_file.unlink(missing_ok=True)
            raise ValueError('Geometria DWG inválida. Tente abrir novamente para reconstruir o cache.')

        cov = data.get('coverage', {})
        # Only cache when models have valid output and are not classified as empty
        if data.get('meshes') and not cache_file.exists() and cov.get('status') != 'empty':
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                temporary_cache = cache_file.with_suffix('.tmp-' + str(os.getpid()))
                shutil.copyfile(result, temporary_cache)
                os.replace(temporary_cache, cache_file)
            except OSError:
                pass

    if not data.get('meshes'):
        skipped_summary = []
        for k, v in data.get('skipped', {}).items():
            skipped_summary.append(f"{v} {k}")
        ent_desc = f"Entidades no arquivo: {', '.join(skipped_summary[:8])}." if skipped_summary else ""
        detail = '\n'.join(data.get('warnings', [])[:12])
        raise ValueError(
            f'O arquivo DWG não produziu sólidos 3D visualizáveis.\n'
            f'{ent_desc}\n'
            f'{detail}'
        )

    scale = UNITS.get(data.get('units', 4), unit_scale)
    parts = []
    meshes = data.get('meshes', [])
    total_meshes = len(meshes)
    total_triangles_count = 0
    proxy_converted_count = 0

    for i, m in enumerate(meshes):
        points = m.get('points')
        if not points:
            continue
        pts = np.asarray(points, dtype=np.float32)
        if scale != 1.0:
            pts *= np.float32(scale)

        raw_faces = m.get('faces', [])
        faces_arr = np.asarray(raw_faces, dtype=np.int64)
        mesh = pv.PolyData(pts, faces_arr)
        try:
            if len(faces_arr) > 0 and faces_arr[0] > 4:
                mesh = mesh.triangulate()
        except Exception:
            pass
        total_triangles_count += int(len(faces_arr) // 4) if len(faces_arr) > 0 and faces_arr[0] == 3 else getattr(mesh, 'n_cells', 1)

        _, _, layer_name = parse_dwg_mesh_info(m['name'])
        raw_color = m.get('color')

        is_default_white_or_gray = raw_color in ([255, 255, 255], [190, 201, 212], [230, 230, 230], None)
        if not is_default_white_or_gray:
            color_rgb = list(raw_color)
        else:
            color_rgb = list(resolve_layer_color(layer_name))

        if any(tag in m['name'] for tag in ('[Plant3D]', '[Plant3D-Mesh]', '[Plant3D-Fitting]', '[Plant3D-Structure]')):
            proxy_converted_count += 1

        attach_mesh_metadata(mesh, layer_name, color_rgb)
        parts.append((m['name'], mesh))

        if progress_callback and (i % max(1, total_meshes // 10) == 0 or i == total_meshes - 1):
            pct = 60.0 + (35.0 * i / max(1, total_meshes))
            progress_callback(pct, f'Loading {file_name} ...')

    xrefs_loaded_count = 0
    explicit_xrefs = data.get('xrefs', [])
    if explicit_xrefs:
        xref_dirs = [source.parent, source.parent / 'xref', source.parent / 'xrefs', source.parent / 'referencias']
        for xref_name in explicit_xrefs:
            for xdir in xref_dirs:
                cand = (xdir / xref_name).resolve()
                if cand.is_file() and cand not in visited_files and cand != source:
                    try:
                        xref_parts, _ = read_dwg(cand, unit_scale=unit_scale, progress_callback=None, visited_files=visited_files)
                        for xname, xmesh in xref_parts:
                            parts.append((f'[XREF: {cand.stem}] {xname}', xmesh))
                        xrefs_loaded_count += 1
                        break
                    except Exception:
                        pass

    elapsed_time = time.monotonic() - t_start

    # Relatório Diagnóstico e Cobertura Completa (Prioridade 2 e 4)
    warnings = list(data.get('warnings', []))
    cov = data.get('coverage', {})
    status_label = {
        'complete': 'COMPLETO (100% dos sólidos reconstruídos com sucesso)',
        'partial': f'PARCIAL ({cov.get("failed_solids", 0)} sólidos com erro/incompletos)',
        'empty': 'VAZIO (Nenhum sólido 3D reconstruído)'
    }.get(cov.get('status', 'complete'), cov.get('status', 'Indefinido'))

    diag_report = [
        "============================================================",
        "PLANTVIEW STUDIO — DIAGNÓSTICO DE IMPORTAÇÃO DWG 3D",
        "============================================================",
        f"Arquivo: {file_name}",
        f"Modo de Leitura: {data.get('read_mode', 'normal_failsafe').upper()}",
        f"Status de Cobertura: {status_label}",
        f"Sólidos 3D Identificados no DWG: {cov.get('total_solids_found', total_meshes)}",
        f"  ├─ Reconstrução ACIS B-Rep Nativa: {cov.get('acis_success', total_meshes)}",
        f"  ├─ Recuperados via Proxy GraphicData Fallback: {cov.get('proxy_fallback', 0)}",
        f"  ├─ Recuperados via Wireframe 3D Fallback: {cov.get('wire_fallback', 0)}",
        f"  └─ Sólidos com Falha na Reconstrução: {cov.get('failed_solids', 0)}",
        f"Malhas 3D Exibidas no Visor: {len(parts)}",
        f"Objetos PROXY (Plant 3D / Estruturas) no Visor: {proxy_converted_count}",
        f"Xrefs Carregadas Automaticamente: {xrefs_loaded_count}",
        f"Total de Triângulos na Maquete: {total_triangles_count:,}",
        f"Unidade do DWG: {data.get('units', 4)} (Escala: {scale} m/unidade)",
        f"Tempo de Importação: {elapsed_time:.2f} s",
        f"Assinatura do Motor Nativo (SHA256): {engine_signature[:16]}...",
        "============================================================"
    ]
    if cov.get('status') == 'partial':
        warnings.append(f"AVISO CRÍTICO: Cobertura parcial ({cov.get('failed_solids', 0)} sólido(s) não puderam ser reconstruídos).")

    for line in reversed(diag_report):
        warnings.insert(0, line)

    if progress_callback:
        progress_callback(100.0, f'Loading {file_name} ...')

    return parts, warnings


def read_model(path, unit_scale=.001, progress_callback: Optional[Callable[[float, str], bool]] = None, force_reload: bool = False):
    path = Path(path)
    ext = path.suffix.lower()
    warnings = []

    if ext == '.dwg':
        parts, warnings = read_dwg(path, unit_scale, progress_callback, force_reload=force_reload)
    elif ext in ('.step', '.stp'):
        if progress_callback:
            progress_callback(15.0, f'Loading {path.name} ...')
        from OCP.STEPControl import STEPControl_Reader
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.BRep import BRep_Tool
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
        from OCP.TopoDS import TopoDS
        from OCP.TopLoc import TopLoc_Location
        reader = STEPControl_Reader()
        if reader.ReadFile(str(path)) != IFSelect_RetDone:
            raise ValueError('Não foi possível ler o STEP.')
        reader.TransferRoots()
        shape = reader.OneShape()
        if progress_callback:
            progress_callback(50.0, f'Loading {path.name} ...')
        BRepMesh_IncrementalMesh(shape, .5, False, .3, True)
        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        points, faces = [], []
        while explorer.More():
            face = TopoDS.Face_s(explorer.Current())
            loc = TopLoc_Location()
            triangulation = BRep_Tool.Triangulation_s(face, loc)
            if triangulation is not None:
                base = len(points)
                for i in range(1, triangulation.NbNodes()+1):
                    p = triangulation.Node(i).Transformed(loc.Transformation())
                    points.append([p.X()*.001, p.Y()*.001, p.Z()*.001])
                for i in range(1, triangulation.NbTriangles()+1):
                    a,b,c = triangulation.Triangle(i).Get()
                    if face.Orientation() == TopAbs_REVERSED: b,c = c,b
                    faces.extend([3,base+a-1,base+b-1,base+c-1])
            else: warnings.append('Face STEP sem triangulação.')
            explorer.Next()

        parts = []
        if points:
            mesh = pv.PolyData(np.asarray(points), faces)
            layer = 'ESTRUTURA' if 'ESTRUTURA' in path.stem.upper() else 'EQUIP'
            color = resolve_layer_color(layer)
            attach_mesh_metadata(mesh, layer, color)
            parts.append((path.stem, mesh))
        if progress_callback:
            progress_callback(100.0, f'Loading {path.name} ...')
    elif ext == '.ifc':
        if progress_callback:
            progress_callback(15.0, f'Loading {path.name} ...')
        import ifcopenshell
        import ifcopenshell.geom
        doc = ifcopenshell.open(str(path))
        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)
        parts = []
        products = list(doc.by_type('IfcProduct'))
        total_p = len(products)
        for idx, entity in enumerate(products):
            if not entity.Representation: continue
            try:
                shape = ifcopenshell.geom.create_shape(settings, entity)
                points = np.array(shape.geometry.verts).reshape(-1, 3)
                triangles = np.array(shape.geometry.faces).reshape(-1, 3)
                faces = np.column_stack((np.full(len(triangles), 3), triangles)).ravel()
                if len(points):
                    mesh = pv.PolyData(points, faces)
                    color = resolve_ifc_color(entity.is_a())
                    attach_mesh_metadata(mesh, entity.is_a(), color)
                    parts.append((f'{entity.Name or entity.is_a()} | {entity.GlobalId}', mesh))
            except Exception as error:
                warnings.append(f'{entity.GlobalId}: {error}')
            if progress_callback and idx % max(1, total_p // 10) == 0:
                pct = 20.0 + (75.0 * idx / max(1, total_p))
                progress_callback(pct, f'Loading {path.name} ...')
        if progress_callback:
            progress_callback(100.0, f'Loading {path.name} ...')
    elif ext == '.dxf':
        if progress_callback:
            progress_callback(15.0, f'Loading {path.name} ...')
        import ezdxf
        from ezdxf.disassemble import recursive_decompose
        from ezdxf.path import make_path
        doc = ezdxf.readfile(path)
        unit = int(doc.header.get('$INSUNITS', 0))
        scale = UNITS.get(unit, unit_scale)
        if unit not in UNITS: warnings.append(f'Unidade DXF não reconhecida; escala {scale} m/unidade.')

        dxf_layer_colors = {}
        for l in doc.layers:
            color_idx = l.color
            if color_idx in ACI_TABLE:
                dxf_layer_colors[l.dxf.name] = ACI_TABLE[color_idx]
            else:
                dxf_layer_colors[l.dxf.name] = resolve_layer_color(l.dxf.name)

        parts = []
        skipped = Counter()
        for entity in recursive_decompose(doc.modelspace()):
            kind = entity.dxftype()
            layer = entity.dxf.layer
            layer_color = dxf_layer_colors.get(layer, resolve_layer_color(layer))
            try:
                if kind == '3DFACE':
                    pts = np.array([tuple(p) for p in entity.wcs_vertices(close=False)])*scale
                    mesh = pv.PolyData(pts, [len(pts), *range(len(pts))])
                elif kind in ('LINE', 'LWPOLYLINE', 'POLYLINE', 'ARC', 'CIRCLE', 'ELLIPSE', 'SPLINE'):
                    points = np.array([tuple(p) for p in make_path(entity).flattening(.5)])*scale
                    if len(points) < 2: continue
                    mesh = pv.lines_from_points(points)
                else:
                    skipped[kind] += 1; continue
                attach_mesh_metadata(mesh, layer, layer_color)
                parts.append((f'{layer} | {kind}', mesh))
            except Exception: skipped[kind] += 1
        warnings.extend(f'{count} entidade(s) {kind} não suportada(s).' for kind, count in skipped.items())
        if progress_callback:
            progress_callback(100.0, f'Loading {path.name} ...')
    elif ext in ('.stl', '.obj', '.ply', '.vtp'):
        if progress_callback:
            progress_callback(20.0, f'Loading {path.name} ...')
        mesh = pv.read(str(path))
        mesh.points *= unit_scale
        color = resolve_layer_color(path.stem)
        attach_mesh_metadata(mesh, path.stem, color)
        parts = [(path.stem, mesh)]
        warnings.append(f'Malha sem unidade: escala aplicada {unit_scale} m/unidade.')
        if progress_callback:
            progress_callback(100.0, f'Loading {path.name} ...')
    else:
        raise ValueError(f'Formato {ext} não suportado. NWC/NWD/RVT exigem SDK específico.')

    if not parts:
        raise ValueError('Nenhuma geometria compatível encontrada.\n' + '\n'.join(warnings[:30]))
    return parts, warnings
