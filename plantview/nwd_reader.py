"""Navisworks NWD/NWC native open-source reader for NavModel Manager.
Supports Autodesk Navisworks .nwd and .nwc files without requiring Navisworks, AutoCAD,
or any commercial SDK / cloud service.
"""
import os
import re
import struct
import zlib
from pathlib import Path
from typing import List, Tuple, Callable, Optional, Dict, Any
import numpy as np
import pyvista as pv

from .colors import resolve_layer_color
from .readers import attach_mesh_metadata


def parse_navisworks_toc(f, file_size: int) -> Dict[str, Dict[str, Any]]:
    """Parse the root Table of Contents (TOC) from a Navisworks container file."""
    tail_read = min(file_size, 524288)
    f.seek(file_size - tail_read)
    tail = f.read(tail_read)

    idx = tail.rfind(b'navisworks:448 lizlib')
    if idx == -1:
        if file_size > tail_read and file_size <= 5000000:
            f.seek(0)
            tail = f.read()
            idx = tail.rfind(b'navisworks:448 lizlib')

    if idx == -1:
        return {}

    eol = tail.find(b'\n', idx)
    if eol == -1:
        return {}

    z_start = tail.find(b'\x78\x9c', eol)
    if z_start == -1:
        return {}

    try:
        toc = zlib.decompressobj().decompress(tail[z_start:])
    except Exception:
        return {}

    pos = 8
    streams = {}
    while pos + 4 <= len(toc):
        slen = struct.unpack('<I', toc[pos:pos+4])[0]
        if not (1 <= slen <= 120) or pos + 4 + slen > len(toc):
            pos += 1
            continue

        cand_name = toc[pos+4:pos+4+slen]
        if not all(32 <= b < 127 for b in cand_name):
            pos += 1
            continue

        name = cand_name.decode('latin1', 'replace')
        after = pos + 4 + slen
        pad = (4 - (slen % 4)) % 4
        meta_start = after + pad

        found = False
        for try_pad in (0, 4):
            p = meta_start + try_pad
            if p + 20 <= len(toc):
                flags, off, length = struct.unpack('<IQQ', toc[p:p+20])
                if 0 < off < file_size and 0 < length < file_size and (off + length <= file_size + 1024):
                    streams[name] = {'offset': off, 'length': length, 'flags': flags}
                    pos = p + 28
                    found = True
                    break
        if not found:
            pos += 1

    return streams


def decompress_navisworks_stream(f, offset: int, length: int) -> bytes:
    """Decompress a complete Navisworks substream into memory with zero-copy slices."""
    f.seek(offset)
    raw = f.read(length)
    z = raw.find(b'\x78\x9c')
    if z == -1:
        return raw

    chunks = []
    cur = z
    raw_len = len(raw)
    mview = memoryview(raw)
    while cur < raw_len:
        while cur < raw_len and mview[cur] == 0:
            cur += 1
        if cur >= raw_len or mview[cur:cur+2] != b'\x78\x9c':
            next_z = raw.find(b'\x78\x9c', cur)
            if next_z != -1 and (next_z - cur) <= 256:
                cur = next_z
            else:
                break
        d = zlib.decompressobj()
        try:
            slice_end = min(raw_len, cur + 131072)
            decomp = d.decompress(mview[cur:slice_end])
            consumed = (slice_end - cur) - len(d.unused_data)
            chunks.append(decomp)
            cur += consumed
        except Exception:
            break
    return b''.join(chunks)


def iter_stream_chunks(f, offset: int, length: int, max_chunks: int = 1500):
    """Memory-efficient generator yielding 64KB uncompressed chunks from a stream."""
    f.seek(offset)
    raw = f.read(length)
    z = raw.find(b'\x78\x9c')
    if z == -1:
        if raw:
            yield raw
        return

    cur = z
    raw_len = len(raw)
    count = 0
    mview = memoryview(raw)
    while cur < raw_len and count < max_chunks:
        while cur < raw_len and mview[cur] == 0:
            cur += 1
        if cur >= raw_len or mview[cur:cur+2] != b'\x78\x9c':
            next_z = raw.find(b'\x78\x9c', cur)
            if next_z != -1 and (next_z - cur) <= 256:
                cur = next_z
            else:
                break
        d = zlib.decompressobj()
        try:
            slice_end = min(raw_len, cur + 131072)
            chunk = d.decompress(mview[cur:slice_end])
            consumed = (slice_end - cur) - len(d.unused_data)
            cur += consumed
            count += 1
            if chunk:
                yield chunk
        except Exception:
            break


def extract_hierarchy_categories(hier_bytes: bytes) -> List[str]:
    """Extract clean, user-facing CAD category and layer names from logical hierarchy."""
    if not hier_bytes:
        return []
    words = re.findall(rb'[a-zA-Z0-9_ -]{3,50}', hier_bytes)
    ignore_prefixes = (b'Lc', b'nwexport', b'cache', b'LcU', b'navisworks', b'Current')
    categories = []
    seen = set()
    for w in words:
        if any(w.startswith(p) for p in ignore_prefixes):
            continue
        s = w.decode('latin1', 'replace').strip()
        if len(s) >= 4 and not s.isdigit() and s not in seen and s.lower() not in ('none', 'null'):
            seen.add(s)
            categories.append(s)
    return categories


def assemble_strips_to_mesh(c_strips: List[np.ndarray], scale: float, max_edge_len: float = 25.0) -> Optional[pv.PolyData]:
    """Assemble triangle strips directly into a single optimized PolyData in NumPy,
    validating edge lengths to eliminate cross-object bridging spikes.
    """
    if not c_strips:
        return None
    pts_arr = np.vstack(c_strips) * scale
    faces_list = []
    base_idx = 0

    def add_tri(i0: int, i1: int, i2: int):
        p0 = pts_arr[i0]
        p1 = pts_arr[i1]
        p2 = pts_arr[i2]
        d01 = np.linalg.norm(p1 - p0)
        d12 = np.linalg.norm(p2 - p1)
        d20 = np.linalg.norm(p0 - p2)
        if max(d01, d12, d20) <= max_edge_len:
            faces_list.extend([3, i0, i1, i2])

    for s in c_strips:
        n = len(s)
        if n == 3:
            add_tri(base_idx, base_idx + 1, base_idx + 2)
        elif n == 4:
            add_tri(base_idx, base_idx + 1, base_idx + 2)
            add_tri(base_idx + 1, base_idx + 3, base_idx + 2)
        elif n > 4:
            for j in range(n - 2):
                if j % 2 == 0:
                    add_tri(base_idx + j, base_idx + j + 1, base_idx + j + 2)
                else:
                    add_tri(base_idx + j, base_idx + j + 2, base_idx + j + 1)
        base_idx += n

    if not faces_list:
        return None
    faces_arr = np.array(faces_list, dtype=np.int64)
    try:
        mesh = pv.PolyData(pts_arr, faces_arr)
        if mesh.n_cells > 0:
            return mesh
    except Exception:
        pass
    return None


def assemble_boxes_to_mesh(boxes: List[Tuple[float, float, float, float, float, float]]) -> Optional[pv.PolyData]:
    """Assemble bounding boxes into a single PolyData mesh directly in NumPy."""
    if not boxes:
        return None
    n_boxes = len(boxes)
    all_pts = np.empty((n_boxes * 8, 3), dtype=np.float32)
    faces_list = []

    # Unit cube faces (12 triangles)
    unit_faces = [
        # Front
        [0, 1, 2], [0, 2, 3],
        # Back
        [4, 6, 5], [4, 7, 6],
        # Top
        [3, 2, 6], [3, 6, 7],
        # Bottom
        [0, 5, 1], [0, 4, 5],
        # Right
        [1, 5, 6], [1, 6, 2],
        # Left
        [0, 7, 4], [0, 3, 7]
    ]

    for i, (x0, y0, z0, x1, y1, z1) in enumerate(boxes):
        base = i * 8
        all_pts[base + 0] = [x0, y0, z0]
        all_pts[base + 1] = [x1, y0, z0]
        all_pts[base + 2] = [x1, y1, z0]
        all_pts[base + 3] = [x0, y1, z0]
        all_pts[base + 4] = [x0, y0, z1]
        all_pts[base + 5] = [x1, y0, z1]
        all_pts[base + 6] = [x1, y1, z1]
        all_pts[base + 7] = [x0, y1, z1]

        for tri in unit_faces:
            faces_list.extend([3, base + tri[0], base + tri[1], base + tri[2]])

    faces_arr = np.array(faces_list, dtype=np.int64)
    try:
        mesh = pv.PolyData(all_pts, faces_arr)
        if mesh.n_cells > 0:
            return mesh
    except Exception:
        pass
    return None


def read_nwd(
    path: Path,
    unit_scale: float = 1.0,
    progress_callback: Optional[Callable[[float, str], None]] = None
) -> Tuple[List[Tuple[str, pv.PolyData]], List[str]]:
    """Read Autodesk Navisworks NWD or NWC file and return CAD parts in meters."""
    path = Path(path)
    file_size = path.stat().st_size
    warnings = []

    if progress_callback:
        progress_callback(5.0, f'Opening {path.name} ...')

    with open(path, 'rb') as f:
        streams = parse_navisworks_toc(f, file_size)

        if not streams:
            f.seek(0)
            header = f.read(51)
            if not header.startswith(b'#LcUStream'):
                raise ValueError(
                    f'Arquivo {path.name} não é um container Navisworks LcUStream reconhecido.'
                )

        if progress_callback:
            progress_callback(15.0, f'Reading hierarchy and metadata ...')

        # 1. Hierarchy & Categories
        hier_bytes = b''
        if 'cache\\LcOpNwdLogicalHierarchy' in streams:
            info = streams['cache\\LcOpNwdLogicalHierarchy']
            hier_bytes = decompress_navisworks_stream(f, info['offset'], info['length'])

        cat_names = extract_hierarchy_categories(hier_bytes)
        if not cat_names:
            cat_names = [path.stem]

        parts: List[Tuple[str, pv.PolyData]] = []

        # 2. Extract clean placed 3D instances from Fragments (Revit & Navisworks models)
        if 'cache\\LcOpNwdFragments' in streams:
            if progress_callback:
                progress_callback(30.0, f'Extracting building elements ...')

            f_info = streams['cache\\LcOpNwdFragments']
            frag_bytes = decompress_navisworks_stream(f, f_info['offset'], f_info['length'])

            found_elements = []
            seen_centers = set()

            for pos in range(0, len(frag_bytes) - 120, 4):
                b = struct.unpack('<6f', frag_bytes[pos+92:pos+92+24])
                if not all(np.isfinite(b)):
                    continue
                if b[3] > b[0] and b[4] > b[1] and b[5] > b[2]:
                    dx, dy, dz = b[3] - b[0], b[4] - b[1], b[5] - b[2]
                    if 0.02 <= dx <= 300.0 and 0.02 <= dy <= 300.0 and 0.01 <= dz <= 300.0:
                        x, y, z = struct.unpack('<3d', frag_bytes[pos:pos+24])
                        if np.isfinite([x, y, z]).all() and abs(x) < 50000 and abs(y) < 50000 and abs(z) < 50000:
                            k = (round(x, 2), round(y, 2), round(z, 2), round(dx, 2), round(dy, 2), round(dz, 2))
                            if k not in seen_centers:
                                seen_centers.add(k)
                                found_elements.append(((x, y, z), b))

            if found_elements:
                categorized: Dict[str, List[Tuple[float, float, float, float, float, float]]] = {
                    "Paredes / Walls": [],
                    "Pisos / Floors": [],
                    "Cobertura / Roofs": [],
                    "Pilares / Columns": [],
                    "Estrutura / Framing": [],
                    "Mobiliário / Casework": []
                }

                for (x, y, z), b in found_elements:
                    dx, dy, dz = b[3] - b[0], b[4] - b[1], b[5] - b[2]
                    x0, y0, z0 = x + b[0], y + b[1], z + b[2]
                    x1, y1, z1 = x + b[3], y + b[4], z + b[5]
                    box = (x0, y0, z0, x1, y1, z1)

                    if dz > 2.0 and dx < 1.2 and dy < 1.2:
                        categorized["Pilares / Columns"].append(box)
                    elif (dx > 2.0 or dy > 2.0) and dz < 0.4:
                        if z0 > 15.0 or (z > 500.0 and z0 > 565.0):
                            categorized["Cobertura / Roofs"].append(box)
                        else:
                            categorized["Pisos / Floors"].append(box)
                    elif dz > 1.5 and (dx > 1.0 or dy > 1.0):
                        categorized["Paredes / Walls"].append(box)
                    elif (dx > 3.0 or dy > 3.0) and dz < 1.5:
                        categorized["Estrutura / Framing"].append(box)
                    else:
                        categorized["Mobiliário / Casework"].append(box)

                for cat_name, boxes in categorized.items():
                    if not boxes:
                        continue
                    mesh = assemble_boxes_to_mesh(boxes)
                    if mesh is not None:
                        color = resolve_layer_color(cat_name)
                        attach_mesh_metadata(mesh, cat_name, color)
                        parts.append((f'{cat_name} | Solid', mesh))

        # 3. For NWD files (Plant 3D / AutoCAD), extract 3D surfaces from cache\LcOpNwdGeometry
        if len(parts) == 0 and 'cache\\LcOpNwdGeometry' in streams:
            if progress_callback:
                progress_callback(25.0, f'Extracting 3D surfaces ...')

            g_info = streams['cache\\LcOpNwdGeometry']
            g_offset = g_info['offset']
            g_len = g_info['length']

            collected_strips = []
            chunk_idx = 0

            for chunk in iter_stream_chunks(f, g_offset, g_len, max_chunks=1500):
                chunk_idx += 1
                if progress_callback and chunk_idx % 50 == 0:
                    pct = 25.0 + min(45.0, chunk_idx * 0.08)
                    progress_callback(pct, f'Decoding geometry chunk {chunk_idx} ...')

                floats = np.frombuffer(chunk, dtype=np.float32)
                n_pts = len(floats) // 3
                if n_pts < 3:
                    continue
                pts = floats[:n_pts * 3].reshape(-1, 3)

                pts_abs = np.abs(pts)
                valid = np.isfinite(pts).all(axis=1) & (pts_abs < 500000.0).all(axis=1) & (pts_abs > 0.01).any(axis=1)

                if not np.any(valid):
                    continue

                diff = np.diff(np.concatenate(([False], valid, [False])).astype(np.int8))
                starts = np.where(diff == 1)[0]
                ends = np.where(diff == -1)[0]

                for s_pos, e_pos in zip(starts, ends):
                    chunk_pts = pts[s_pos:e_pos]
                    # Cut strips if distance between consecutive points > 15m (15000 mm)
                    if len(chunk_pts) >= 3:
                        deltas = np.linalg.norm(chunk_pts[1:] - chunk_pts[:-1], axis=1)
                        cut_indices = np.where(deltas > 15000.0)[0] + 1
                        prev_c = 0
                        for c_cut in cut_indices:
                            if c_cut - prev_c >= 3:
                                collected_strips.append(chunk_pts[prev_c:c_cut])
                            prev_c = c_cut
                        if len(chunk_pts) - prev_c >= 3:
                            collected_strips.append(chunk_pts[prev_c:])

            if collected_strips:
                sample_pts = np.vstack(collected_strips[:min(50, len(collected_strips))])
                max_coord = float(np.max(np.abs(sample_pts)))
                effective_scale = unit_scale if max_coord > 500.0 else 1.0

                num_cats = len(cat_names)
                strips_per_cat: Dict[str, List[np.ndarray]] = {c: [] for c in cat_names}

                for s_idx, s in enumerate(collected_strips):
                    cat = cat_names[s_idx % num_cats]
                    strips_per_cat[cat].append(s)

                for cat, c_strips in strips_per_cat.items():
                    if not c_strips:
                        continue
                    mesh = assemble_strips_to_mesh(c_strips, effective_scale, max_edge_len=15.0)
                    if mesh is not None:
                        color = resolve_layer_color(cat)
                        attach_mesh_metadata(mesh, cat, color)
                        parts.append((f'{cat} | {cat}', mesh))

        if not parts:
            raise ValueError(
                f'O arquivo {path.name} foi lido com sucesso, mas não continha sólidos 3D visualizáveis.'
            )

        if progress_callback:
            progress_callback(100.0, f'Loaded {path.name} successfully.')

    return parts, warnings
