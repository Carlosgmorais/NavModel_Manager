//! Independent read-only DWG tessellator with Plant 3D Object Enabler and ACIS B-Rep caching.
mod acds;
use acadrust::entities::extended_entity::ExtendedEntityData;
use acadrust::entities::solid3d::AcisData;
use acadrust::entities::Solid3D;
use acadrust::io::dwg::DwgReader;
use acadrust::types::Color;
use acadrust::types::{Matrix4, Vector3};
use acadrust::{CadDocument, DwgWriter, EntityType};
use opencadkernel::{acis, brep::mesh};
use serde::Serialize;
use std::collections::{BTreeMap, HashMap};
use std::fs::File;
use std::io::BufWriter;

#[derive(Serialize, Clone, Default)]
struct Mesh {
    name: String,
    color: [u8; 3],
    #[serde(skip_serializing_if = "Vec::is_empty")]
    points: Vec<[f64; 3]>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    faces: Vec<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    points_b64: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    points_origin: Option<[f64; 3]>,
    #[serde(skip_serializing_if = "Option::is_none")]
    faces_b64: Option<String>,
}

fn base64_encode(data: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0];
        let b1 = if chunk.len() > 1 { chunk[1] } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] } else { 0 };

        let triple = ((b0 as u32) << 16) | ((b1 as u32) << 8) | (b2 as u32);

        out.push(TABLE[((triple >> 18) & 0x3F) as usize] as char);
        out.push(TABLE[((triple >> 12) & 0x3F) as usize] as char);

        if chunk.len() > 1 {
            out.push(TABLE[((triple >> 6) & 0x3F) as usize] as char);
        } else {
            out.push('=');
        }

        if chunk.len() > 2 {
            out.push(TABLE[(triple & 0x3F) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

fn encode_local_points(points: &[[f64; 3]]) -> ([f64; 3], Vec<u8>) {
    let origin = points[0];
    let mut bytes = Vec::with_capacity(points.len() * 12);
    for point in points {
        for axis in 0..3 {
            bytes.extend_from_slice(&((point[axis] - origin[axis]) as f32).to_le_bytes());
        }
    }
    (origin, bytes)
}

#[derive(Serialize, Clone, Default)]
struct EntityDiagnostic {
    handle: String,
    layer: String,
    entity_type: String,
    has_acis: bool,
    acis_size: usize,
    acis_format: String,
    parse_result: String,
    bodies_count: usize,
    reconstructed_faces: usize,
    missing_faces: usize,
    vertices: usize,
    triangles: usize,
    bounds: Option<[f64; 6]>,
    status: String,
    notes: String,
}

#[derive(Serialize, Default, Clone)]
struct CoverageReport {
    total_solids_found: usize,
    acis_success: usize,
    proxy_fallback: usize,
    wire_fallback: usize,
    failed_solids: usize,
    partial_solids: usize,
    missing_faces: usize,
    total_meshes_emitted: usize,
    total_triangles_emitted: usize,
    status: String, // "complete" | "partial" | "empty"
}

#[derive(Serialize, Default)]
struct Output {
    units: i16,
    read_mode: String,
    total_entities: usize,
    coverage: CoverageReport,
    diagnostics: Vec<EntityDiagnostic>,
    meshes: Vec<Mesh>,
    warnings: Vec<String>,
    skipped: BTreeMap<String, usize>,
}

#[derive(Clone)]
struct LocalMesh {
    local_points: Vec<Vector3>,
    faces: Vec<usize>,
    missing_faces: usize,
    color: Option<Color>,
}

// ACIS can assign colours to individual faces independently of the owning
// 3DSOLID. Keep that appearance in the shared local cache, resolving ByLayer
// only when the part is inserted into its actual drawing context.
fn acis_colors(sat: &acadrust::entities::acis::SatDocument) -> HashMap<usize, Color> {
    use acadrust::entities::acis::{SatAttribute, SatToken};
    let mut result = HashMap::new();
    for true_color in [false, true] {
        for record in &sat.records {
            if record.entity_type
                != if true_color {
                    "truecolor-adesk-attrib"
                } else {
                    "color-adesk-attrib"
                }
            {
                continue;
            }
            let Some(attr) = SatAttribute::from_record(record) else {
                continue;
            };
            if attr.owner().0 < 0 {
                continue;
            }
            let value = attr.payload().iter().find_map(|token| match token {
                SatToken::Integer(n) => Some(*n as u32),
                SatToken::Sab { tag: 4, data } if data.len() == 4 => {
                    Some(u32::from_le_bytes(data.as_slice().try_into().unwrap()))
                }
                _ => None,
            });
            let Some(value) = value else {
                continue;
            };
            let color = if true_color {
                match value >> 24 {
                    0xC0 => Color::ByLayer,
                    0xC1 => Color::ByBlock,
                    0xC2 => Color::from_rgb((value >> 16) as u8, (value >> 8) as u8, value as u8),
                    0xC3 => Color::from_index((value & 0xFFFF) as i16),
                    _ => continue,
                }
            } else {
                Color::from_index(value as i16)
            };
            if color != Color::None {
                result.insert(attr.owner().0 as usize, color);
            }
        }
    }
    result
}

fn tessellate_local(
    body: &opencadkernel::brep::Body,
    sat: &acadrust::entities::acis::SatDocument,
    tol: mesh::TessellationTolerance,
) -> Vec<LocalMesh> {
    let colors = acis_colors(sat);
    let body_color = body
        .provenance
        .source()
        .and_then(|s| colors.get(&(s.index() as usize)).copied());
    let mut face_colors = body.faces.iter().map(|(_, f)| {
        f.provenance
            .source()
            .and_then(|s| colors.get(&(s.index() as usize)).copied())
            .or(body_color)
    });
    let first_color = face_colors.next().unwrap_or(body_color);
    let uniform = face_colors.all(|c| c == first_color);
    if uniform {
        let tess = mesh::tessellate_fast(body, tol);
        if tess.mesh.triangles.is_empty() {
            return Vec::new();
        }
        return vec![LocalMesh {
            local_points: tess
                .mesh
                .positions
                .iter()
                .map(|p| Vector3::new(p[0], p[1], p[2]))
                .collect(),
            faces: tess
                .mesh
                .triangles
                .iter()
                .flat_map(|t| [3, t[0], t[1], t[2]])
                .collect(),
            missing_faces: tess.missing_faces.len(),
            color: first_color,
        }];
    }
    let tess = mesh::tessellate_for_render(body, tol, true);
    let mut groups: Vec<LocalMesh> = Vec::new();
    for (triangle, face_key) in tess.mesh.triangles.iter().zip(&tess.triangle_faces) {
        let color = body
            .faces
            .get(*face_key)
            .and_then(|f| f.provenance.source())
            .and_then(|s| colors.get(&(s.index() as usize)).copied())
            .or(body_color);
        let index = groups
            .iter()
            .position(|g| g.color == color)
            .unwrap_or_else(|| {
                groups.push(LocalMesh {
                    local_points: Vec::new(),
                    faces: Vec::new(),
                    missing_faces: 0,
                    color,
                });
                groups.len() - 1
            });
        let group = &mut groups[index];
        let offset = group.local_points.len();
        for &i in triangle {
            let p = tess.mesh.positions[i];
            group.local_points.push(Vector3::new(p[0], p[1], p[2]));
        }
        group.faces.extend([3, offset, offset + 1, offset + 2]);
    }
    if let Some(first) = groups.first_mut() {
        first.missing_faces = tess.missing_faces.len();
    }
    groups
}

fn rgb(doc: &CadDocument, color: Color, layer: &str, parent: [u8; 3]) -> [u8; 3] {
    let resolved = match color {
        Color::ByBlock => return parent,
        Color::ByLayer => doc
            .layers
            .get(layer)
            .map(|l| l.color)
            .unwrap_or(Color::Index(7)),
        other => other,
    };
    if let Some((r, g, b)) = resolved.rgb() {
        [r, g, b]
    } else {
        parent
    }
}

fn compute_bounds(points: &[[f64; 3]]) -> Option<[f64; 6]> {
    if points.is_empty() {
        return None;
    }
    let mut min = [points[0][0], points[0][1], points[0][2]];
    let mut max = [points[0][0], points[0][1], points[0][2]];
    for p in &points[1..] {
        min[0] = min[0].min(p[0]);
        min[1] = min[1].min(p[1]);
        min[2] = min[2].min(p[2]);
        max[0] = max[0].max(p[0]);
        max[1] = max[1].max(p[1]);
        max[2] = max[2].max(p[2]);
    }
    Some([min[0], min[1], min[2], max[0], max[1], max[2]])
}

fn append_proxy_polygon(points: &mut Vec<[f64; 3]>, rings: &[Vec<usize>], faces: &mut Vec<usize>) {
    if rings.is_empty() || rings[0].len() < 3 {
        return;
    }
    let outer = &rings[0];
    if rings.len() == 1 && outer.len() == 3 {
        faces.extend([3, outer[0], outer[1], outer[2]]);
        return;
    }
    if rings.len() == 1 && outer.len() == 4 {
        faces.extend([
            3, outer[0], outer[1], outer[2], 3, outer[0], outer[2], outer[3],
        ]);
        return;
    }
    let origin = Vector3::new(
        points[outer[0]][0],
        points[outer[0]][1],
        points[outer[0]][2],
    );
    let mut normal = Vector3::new(0.0, 0.0, 0.0);
    for i in 0..outer.len() {
        let a = points[outer[i]];
        let b = points[outer[(i + 1) % outer.len()]];
        normal = normal
            + Vector3::new(
                (a[1] - b[1]) * (a[2] + b[2]),
                (a[2] - b[2]) * (a[0] + b[0]),
                (a[0] - b[0]) * (a[1] + b[1]),
            );
    }
    if normal.length_squared() < 1e-20 {
        return;
    }
    let (u, v) = orthonormal_basis(normal.normalize());
    let projected: Vec<Vec<[f64; 2]>> = rings
        .iter()
        .map(|ring| {
            ring.iter()
                .map(|&i| {
                    let p = points[i];
                    let d = Vector3::new(p[0], p[1], p[2]) - origin;
                    [d.dot(&u), d.dot(&v)]
                })
                .collect()
        })
        .collect();
    let (vertices, triangles) =
        opencadkernel::geom2d::triangulate::polygon(&projected[0], &projected[1..]);
    let base = points.len();
    for q in vertices {
        let p = origin + u * q[0] + v * q[1];
        points.push([p.x, p.y, p.z]);
    }
    for t in triangles {
        faces.extend([3, base + t[0], base + t[1], base + t[2]]);
    }
}

/// Decode Autodesk Proxy Graphics SHELL (opcode 9) into raw local points and faces.
// Expand the original saved profile before polygon triangulation can append
// cap vertices. The paired halves and the saved axis identify a collapsed beam.
fn expand_collapsed_shell(local_pts: &mut [[f64; 3]], axis: Option<[Vector3; 2]>) {
    // Check for Plant 3D AcPpDb3dStructureMember collapsed cross-section shell:
    // When an AcPpDb3dStructureMember is saved without Object Enabler body expansion,
    // its SHELL has 2*M vertices where the first M and second M vertices are identical
    // (a 0-thickness mid-beam cross-section slice) preceded by a 2-point Opcode 6 beam axis!
    if let Some([p_start, p_end]) = axis {
        let axis_vec = p_end - p_start;
        let axis_len = axis_vec.length();
        let n_v = local_pts.len();
        if n_v >= 6 && n_v % 2 == 0 && axis_len > 20.0 {
            let half = n_v / 2;
            let mut collapsed = true;
            for k in 0..half {
                let dx = local_pts[k][0] - local_pts[k + half][0];
                let dy = local_pts[k][1] - local_pts[k + half][1];
                let dz = local_pts[k][2] - local_pts[k + half][2];
                if dx * dx + dy * dy + dz * dz > 1.0 {
                    collapsed = false;
                    break;
                }
            }
            if collapsed {
                let dir = axis_vec * (1.0 / axis_len);
                let mid = (p_start + p_end) * 0.5;
                for k in 0..half {
                    let v = Vector3::new(local_pts[k][0], local_pts[k][1], local_pts[k][2]);
                    let offset_from_mid = v - mid;
                    let in_plane = offset_from_mid - dir * offset_from_mid.dot(&dir);
                    let v0 = p_start + in_plane;
                    let v1 = p_end + in_plane;
                    local_pts[k] = [v0.x, v0.y, v0.z];
                    local_pts[k + half] = [v1.x, v1.y, v1.z];
                }
            }
        }
    }
}

fn parse_proxy_shell_local(
    payload: &[u8],
    axis: Option<[Vector3; 2]>,
) -> Option<(Vec<[f64; 3]>, Vec<usize>)> {
    if payload.len() < 12 {
        return None;
    }
    let mut cur = 0;
    let read_u32 = |cur: &mut usize| -> Option<u32> {
        if *cur + 4 > payload.len() {
            return None;
        }
        let val = u32::from_le_bytes(payload[*cur..*cur + 4].try_into().ok()?);
        *cur += 4;
        Some(val)
    };
    let read_i32 = |cur: &mut usize| -> Option<i32> {
        if *cur + 4 > payload.len() {
            return None;
        }
        let val = i32::from_le_bytes(payload[*cur..*cur + 4].try_into().ok()?);
        *cur += 4;
        Some(val)
    };
    let read_f64 = |cur: &mut usize| -> Option<f64> {
        if *cur + 8 > payload.len() {
            return None;
        }
        let val = f64::from_le_bytes(payload[*cur..*cur + 8].try_into().ok()?);
        *cur += 8;
        Some(val)
    };

    let total_vertices = read_u32(&mut cur)? as usize;
    if total_vertices == 0 || total_vertices > 1_000_000 {
        return None;
    }
    if cur + total_vertices * 24 > payload.len() {
        return None;
    }

    let mut points = Vec::with_capacity(total_vertices);
    for _ in 0..total_vertices {
        let x = read_f64(&mut cur)?;
        let y = read_f64(&mut cur)?;
        let z = read_f64(&mut cur)?;
        points.push([x, y, z]);
    }

    expand_collapsed_shell(&mut points, axis);

    let face_entry_count = read_u32(&mut cur)? as usize;
    if face_entry_count == 0 || face_entry_count > 3_000_000 {
        return None;
    }

    let mut faces = Vec::new();
    let mut rings: Vec<Vec<usize>> = Vec::new();
    let mut read_entries = 0;
    while read_entries < face_entry_count {
        let edge_count_raw = read_i32(&mut cur)?;
        let edge_count = edge_count_raw.unsigned_abs() as usize;
        if edge_count > 1_000_000 || read_entries + 1 + edge_count > face_entry_count {
            return None;
        }
        if edge_count_raw > 0 && !rings.is_empty() {
            append_proxy_polygon(&mut points, &rings, &mut faces);
            rings.clear();
        }
        let mut indices = Vec::with_capacity(edge_count);
        for _ in 0..edge_count {
            let index = read_u32(&mut cur)? as usize;
            if index >= total_vertices {
                return None;
            }
            indices.push(index);
        }
        if indices.len() >= 3 {
            rings.push(indices);
        }
        read_entries += 1 + edge_count;
    }
    if !rings.is_empty() {
        append_proxy_polygon(&mut points, &rings, &mut faces);
    }

    if !faces.is_empty() && !points.is_empty() {
        Some((points, faces))
    } else {
        None
    }
}

/// Decode Autodesk Proxy Graphics MESH (opcode 8) - MxN grid surfaces.
fn decode_proxy_mesh(
    payload: &[u8],
    matrix: Matrix4,
    color: [u8; 3],
    name: &str,
    out: &mut Output,
) -> bool {
    if payload.len() < 8 {
        return false;
    }
    let mut cur = 0;
    let read_u32 = |cur: &mut usize| -> Option<u32> {
        if *cur + 4 > payload.len() {
            return None;
        }
        let val = u32::from_le_bytes(payload[*cur..*cur + 4].try_into().ok()?);
        *cur += 4;
        Some(val)
    };
    let read_f64 = |cur: &mut usize| -> Option<f64> {
        if *cur + 8 > payload.len() {
            return None;
        }
        let val = f64::from_le_bytes(payload[*cur..*cur + 8].try_into().ok()?);
        *cur += 8;
        Some(val)
    };

    let num_rows = match read_u32(&mut cur) {
        Some(v) => v as usize,
        None => return false,
    };
    let num_cols = match read_u32(&mut cur) {
        Some(v) => v as usize,
        None => return false,
    };
    if num_rows < 2 || num_cols < 2 || num_rows * num_cols > 1_000_000 {
        return false;
    }
    let total_pts = num_rows * num_cols;
    if cur + total_pts * 24 > payload.len() {
        return false;
    }
    let mut points = Vec::with_capacity(total_pts);
    for _ in 0..total_pts {
        let x = match read_f64(&mut cur) {
            Some(v) => v,
            None => return false,
        };
        let y = match read_f64(&mut cur) {
            Some(v) => v,
            None => return false,
        };
        let z = match read_f64(&mut cur) {
            Some(v) => v,
            None => return false,
        };
        let p = matrix.transform_point(Vector3::new(x, y, z));
        points.push([p.x, p.y, p.z]);
    }
    let mut faces = Vec::new();
    for r in 0..num_rows - 1 {
        for c in 0..num_cols - 1 {
            let i0 = r * num_cols + c;
            let i1 = r * num_cols + (c + 1);
            let i2 = (r + 1) * num_cols + (c + 1);
            let i3 = (r + 1) * num_cols + c;
            faces.extend([3, i0, i1, i2, 3, i0, i2, i3]);
        }
    }
    if !faces.is_empty() {
        out.meshes.push(Mesh {
            name: format!("{} [Plant3D-Mesh]", name),
            color,
            points,
            faces,
            ..Default::default()
        });
        true
    } else {
        false
    }
}

#[derive(Clone, Copy, Debug)]
struct ProxyCircle3D {
    center: Vector3,
    radius: f64,
    normal: Vector3,
}

fn circle_from_3_pts(p1: Vector3, p2: Vector3, p3: Vector3) -> Option<ProxyCircle3D> {
    let u = p2 - p1;
    let v = p3 - p1;
    let w = u.cross(&v);
    let w_len2 = w.length_squared();
    if w_len2 < 1e-10 {
        return None;
    }
    let u2 = u.length_squared();
    let v2 = v.length_squared();
    let num = (v * u2 - u * v2).cross(&w);
    let center = p1 + num * (1.0 / (2.0 * w_len2));
    let radius = (p1 - center).length();
    if !radius.is_finite() || radius < 0.25 || radius > 50_000.0 {
        return None;
    }
    let normal = w.normalize();
    Some(ProxyCircle3D {
        center,
        radius,
        normal,
    })
}

fn orthonormal_basis(normal: Vector3) -> (Vector3, Vector3) {
    let n = normal.normalize();
    let helper = if n.x.abs() < 0.85 {
        Vector3::new(1.0, 0.0, 0.0)
    } else {
        Vector3::new(0.0, 1.0, 0.0)
    };
    let u = n.cross(&helper).normalize();
    let v = n.cross(&u).normalize();
    (u, v)
}

fn append_circle_ring(
    pts: &mut Vec<[f64; 3]>,
    center: Vector3,
    u: Vector3,
    v: Vector3,
    radius: f64,
    segs: usize,
    matrix: Matrix4,
) -> usize {
    let start = pts.len();
    for i in 0..segs {
        let ang = (i as f64) * std::f64::consts::TAU / (segs as f64);
        let (s, c) = ang.sin_cos();
        let lp = center + u * (radius * c) + v * (radius * s);
        let wp = matrix.transform_point(lp);
        pts.push([wp.x, wp.y, wp.z]);
    }
    start
}

fn bridge_rings(faces: &mut Vec<usize>, r0: usize, r1: usize, segs: usize) {
    for i in 0..segs {
        let next = (i + 1) % segs;
        let a = r0 + i;
        let b = r0 + next;
        let c = r1 + next;
        let d = r1 + i;
        faces.extend([3, a, b, c, 3, a, c, d]);
    }
}

fn emit_frustum_between_circles(
    c0: ProxyCircle3D,
    c1: ProxyCircle3D,
    segs: usize,
    matrix: Matrix4,
    pts: &mut Vec<[f64; 3]>,
    faces: &mut Vec<usize>,
) {
    let (u0, v0) = orthonormal_basis(c0.normal);
    // Align u1, v1 with u0, v0 via parallel transport so the frustum never twists
    let n1 = if c0.normal.dot(&c1.normal) >= 0.0 {
        c1.normal.normalize()
    } else {
        (c1.normal * -1.0).normalize()
    };
    let mut u1 = u0 - n1 * u0.dot(&n1);
    if u1.length_squared() < 1e-8 {
        let (ua, _) = orthonormal_basis(n1);
        u1 = ua;
    } else {
        u1 = u1.normalize();
    }
    let v1 = n1.cross(&u1).normalize();

    let r0 = append_circle_ring(pts, c0.center, u0, v0, c0.radius, segs, matrix);
    let r1 = append_circle_ring(pts, c1.center, u1, v1, c1.radius, segs, matrix);
    bridge_rings(faces, r0, r1, segs);
}

fn emit_swept_elbow(
    c_start: ProxyCircle3D,
    c_end: ProxyCircle3D,
    c_mid_opt: Option<ProxyCircle3D>,
    segs: usize,
    steps: usize,
    matrix: Matrix4,
    pts: &mut Vec<[f64; 3]>,
    faces: &mut Vec<usize>,
) {
    let p0 = c_start.center;
    let p2 = c_end.center;
    let chord = p2 - p0;
    let chord_len = chord.length();
    if chord_len < 1e-3 {
        return;
    }

    // Orient tangent t0 from p0 toward the bend, and t2 toward p2
    let mut t0 = c_start.normal.normalize();
    if t0.dot(&chord) < 0.0 {
        t0 = t0 * -1.0;
    }
    let mut t2 = c_end.normal.normalize();
    if t2.dot(&chord) < 0.0 {
        t2 = t2 * -1.0;
    }

    // Control point for quadratic Bezier / circular arc spine
    let p1_ctrl = if let Some(cm) = c_mid_opt {
        // Quadratic Bezier passing through cm.center at t = 0.5: B(0.5) = 0.25*p0 + 0.5*p1 + 0.25*p2
        cm.center * 2.0 - (p0 + p2) * 0.5
    } else {
        // Intersection of ray (p0, t0) and ray (p2, -t2)
        let cross_t = t0.cross(&t2);
        let denom = cross_t.length_squared();
        if denom > 1e-6 {
            let s = chord.cross(&t2).dot(&cross_t) / denom;
            if s > 0.0 && s < chord_len * 2.5 {
                p0 + t0 * s
            } else {
                (p0 + p2) * 0.5
            }
        } else {
            (p0 + p2) * 0.5
        }
    };

    let (mut prev_u, prev_v) = orthonormal_basis(t0);
    let mut prev_n = t0;
    let mut prev_ring = append_circle_ring(pts, p0, prev_u, prev_v, c_start.radius, segs, matrix);

    for step in 1..=steps {
        let s = (step as f64) / (steps as f64);
        let oms = 1.0 - s;
        let center = p0 * (oms * oms) + p1_ctrl * (2.0 * oms * s) + p2 * (s * s);
        let deriv = (p1_ctrl - p0) * (2.0 * oms) + (p2 - p1_ctrl) * (2.0 * s);
        let n = if deriv.length_squared() > 1e-8 {
            deriv.normalize()
        } else {
            prev_n
        };
        // Parallel-transport frame (prev_u, prev_v) from prev_n to n
        let rot_axis = prev_n.cross(&n);
        let sin_a = rot_axis.length();
        let cos_a = prev_n.dot(&n).clamp(-1.0, 1.0);
        let u = if sin_a > 1e-7 {
            let k = rot_axis * (1.0 / sin_a);
            (prev_u * cos_a + k.cross(&prev_u) * sin_a + k * (k.dot(&prev_u) * (1.0 - cos_a)))
                .normalize()
        } else {
            prev_u
        };
        let v = n.cross(&u).normalize();
        let radius = c_start.radius * oms + c_end.radius * s;
        let ring = append_circle_ring(pts, center, u, v, radius, segs, matrix);
        bridge_rings(faces, prev_ring, ring, segs);
        prev_u = u;
        prev_n = n;
        prev_ring = ring;
    }
}

fn reconstruct_fitting_from_primitives(
    circles: &[ProxyCircle3D],
    arcs: &[[Vector3; 3]],
    polylines: &[Vec<Vector3>],
    matrix: Matrix4,
    color: [u8; 3],
    name: &str,
    out: &mut Output,
) -> bool {
    let mut all_circles: Vec<ProxyCircle3D> = circles.to_vec();

    // Detect closed circular polylines (e.g. 33-point socket-weld inner bend rings in Plant 3D elbows)
    for poly in polylines {
        if poly.len() >= 12 {
            let first = poly[0];
            let last = poly[poly.len() - 1];
            let p_mid1 = poly[poly.len() / 3];
            let p_mid2 = poly[(poly.len() * 2) / 3];
            if let Some(c) = circle_from_3_pts(first, p_mid1, p_mid2) {
                if (first - last).length() <= c.radius * 0.45 {
                    all_circles.push(c);
                }
            }
        }
    }

    let mut pts: Vec<[f64; 3]> = Vec::new();
    let mut faces: Vec<usize> = Vec::new();
    let segs = 24usize;

    if !all_circles.is_empty() {
        // Group circles into coaxial groups
        let mut groups: Vec<Vec<ProxyCircle3D>> = Vec::new();
        for c in &all_circles {
            let mut placed = false;
            for g in &mut groups {
                let ref_c = g[0];
                let dot = ref_c.normal.dot(&c.normal).abs();
                if dot > 0.99999 {
                    let delta = c.center - ref_c.center;
                    let along = ref_c.normal * delta.dot(&ref_c.normal);
                    let perp_dist = (delta - along).length();
                    let max_r = ref_c.radius.max(c.radius);
                    if perp_dist <= (max_r * 1e-5).max(1e-3) {
                        g.push(*c);
                        placed = true;
                        break;
                    }
                }
            }
            if !placed {
                groups.push(vec![*c]);
            }
        }

        // Sort circles within each coaxial group along the group's axis and connect consecutive circles
        let mut group_axes: Vec<Vector3> = Vec::with_capacity(groups.len());
        for g in &mut groups {
            let axis = g[0].normal.normalize();
            group_axes.push(axis);
            g.sort_by(|a, b| {
                let ta = a.center.dot(&axis);
                let tb = b.center.dot(&axis);
                ta.partial_cmp(&tb).unwrap_or(std::cmp::Ordering::Equal)
            });
            // Deduplicate overlapping circles in the same plane with same radius
            let mut dedup: Vec<ProxyCircle3D> = Vec::with_capacity(g.len());
            for c in g.iter() {
                if let Some(prev) = dedup.last() {
                    if (c.center - prev.center).length() < 0.25
                        && (c.radius - prev.radius).abs() < 0.25
                    {
                        continue;
                    }
                }
                dedup.push(*c);
            }
            *g = dedup;

            for w in g.windows(2) {
                emit_frustum_between_circles(w[0], w[1], segs, matrix, &mut pts, &mut faces);
            }
        }

        // Case 1: Single coaxial group (Flange, Reducer, Coupling, or Pipe Cap)
        if groups.len() == 1 {
            let g = &groups[0];
            let axis = group_axes[0];
            // Check if there are Opcode 5 arcs forming a hemispherical cap beyond the end of the group
            if !arcs.is_empty() {
                let c_first = g[0];
                let c_last = g[g.len() - 1];
                let mut best_apex: Option<(Vector3, ProxyCircle3D)> = None;
                let mut best_dist = 0.0f64;
                for arc in arcs {
                    let mid = arc[1];
                    let t_mid = (mid - c_first.center).dot(&axis);
                    let t_first = 0.0;
                    let t_last = (c_last.center - c_first.center).dot(&axis);
                    if t_mid > t_last + 1.0 {
                        let d = t_mid - t_last;
                        if d > best_dist {
                            best_dist = d;
                            best_apex = Some((mid, c_last));
                        }
                    } else if t_mid < t_first - 1.0 {
                        let d = t_first - t_mid;
                        if d > best_dist {
                            best_dist = d;
                            best_apex = Some((mid, c_first));
                        }
                    }
                }
                if let Some((apex, base_c)) = best_apex {
                    let dome_dir = (apex - base_c.center).normalize();
                    let dome_h = (apex - base_c.center).dot(&dome_dir).abs();
                    let dome_steps = 6usize;
                    let (u, v) = orthonormal_basis(dome_dir);
                    let mut prev_ring = append_circle_ring(
                        &mut pts,
                        base_c.center,
                        u,
                        v,
                        base_c.radius,
                        segs,
                        matrix,
                    );
                    for s_i in 1..=dome_steps {
                        let theta =
                            (s_i as f64) * std::f64::consts::FRAC_PI_2 / (dome_steps as f64);
                        let r = (base_c.radius * theta.cos()).max(0.5);
                        let h = dome_h * theta.sin();
                        let ring = append_circle_ring(
                            &mut pts,
                            base_c.center + dome_dir * h,
                            u,
                            v,
                            r,
                            segs,
                            matrix,
                        );
                        bridge_rings(&mut faces, prev_ring, ring, segs);
                        prev_ring = ring;
                    }
                }
            } else if g.len() == 1 {
                // Single circle + ruling segments (extrude circle along segment)
                let c0 = g[0];
                for poly in polylines {
                    if poly.len() == 2 {
                        let seg = poly[1] - poly[0];
                        let len = seg.dot(&axis);
                        let relative = poly[0] - c0.center;
                        let axial = relative.dot(&axis).abs();
                        let radial = (relative - axis * relative.dot(&axis)).length();
                        let matching_ruling = axial < 0.1
                            && (radial - c0.radius).abs() < (c0.radius * 0.005).max(0.1)
                            && seg.normalize().dot(&axis).abs() > 0.9999;
                        if len.abs() > 1.0 && matching_ruling {
                            let c1 = ProxyCircle3D {
                                center: c0.center + axis * len,
                                radius: c0.radius,
                                normal: c0.normal,
                            };
                            emit_frustum_between_circles(
                                c0, c1, segs, matrix, &mut pts, &mut faces,
                            );
                            break;
                        }
                    }
                }
            }
        } else if groups.len() >= 2
            && groups.len() <= 3
            && group_axes
                .iter()
                .any(|axis| axis.dot(&group_axes[0]).abs() < 0.9999)
        {
            // Distinguish between an Elbow (2 main end groups + optional 1-circle mid-bend group)
            // and a Tee/Cross (a main coaxial run group spanning across the junction + perpendicular branch groups)
            // Find the two groups with the most circles (or largest span)
            let mut order: Vec<usize> = (0..groups.len()).collect();
            order.sort_by_key(|&i| std::cmp::Reverse(groups[i].len()));

            // Check if any group is a "main run" of a Tee (2+ circles whose segment passes near the other group's axis)
            let mut tee_run_idx: Option<usize> = None;
            if groups.iter().all(|g| g.len() <= 2) && arcs.is_empty() {
                for (gi, g) in groups.iter().enumerate() {
                    if g.len() >= 2 {
                        let p_start = g[0].center;
                        let p_end = g[g.len() - 1].center;
                        let run_len = (p_end - p_start).length();
                        if run_len > g[0].radius * 1.2 {
                            tee_run_idx = Some(gi);
                            break;
                        }
                    }
                }
            }

            if let Some(run_i) = tee_run_idx {
                let run_g = &groups[run_i];
                let run_center = (run_g[0].center + run_g[run_g.len() - 1].center) * 0.5;
                let run_axis = group_axes[run_i];
                for (bi, bg) in groups.iter().enumerate() {
                    if bi == run_i {
                        continue;
                    }
                    // Connect branch group's inner-most circle to the projection on the main run axis
                    let b_inner = bg
                        .iter()
                        .min_by(|a, b| {
                            let da = (a.center - run_center).length();
                            let db = (b.center - run_center).length();
                            da.partial_cmp(&db).unwrap_or(std::cmp::Ordering::Equal)
                        })
                        .copied()
                        .unwrap();
                    let delta = b_inner.center - run_g[0].center;
                    let proj = run_g[0].center + run_axis * delta.dot(&run_axis);
                    let junc_c = ProxyCircle3D {
                        center: proj,
                        radius: b_inner.radius,
                        normal: b_inner.normal,
                    };
                    emit_frustum_between_circles(
                        b_inner, junc_c, segs, matrix, &mut pts, &mut faces,
                    );
                }
            } else {
                // Elbow (90°, 45°, or custom angle): pick the 2 main end groups
                let g0 = &groups[order[0]];
                let g1 = &groups[order[1]];
                let mid_circle = if order.len() >= 3 && groups[order[2]].len() == 1 {
                    Some(groups[order[2]][0])
                } else {
                    None
                };
                // Pick the pair of circles (one from g0, one from g1) that are closest to each other (the inner ends of the collars)
                let mut best_pair = (g0[0], g1[0]);
                let mut min_dist = f64::MAX;
                for &ca in g0 {
                    for &cb in g1 {
                        let d = (ca.center - cb.center).length();
                        if d < min_dist {
                            min_dist = d;
                            best_pair = (ca, cb);
                        }
                    }
                }
                emit_swept_elbow(
                    best_pair.0,
                    best_pair.1,
                    mid_circle,
                    segs,
                    10,
                    matrix,
                    &mut pts,
                    &mut faces,
                );
            }
        }
    }

    if !faces.is_empty() && !pts.is_empty() {
        out.meshes.push(Mesh {
            name: format!("{} [Plant3D-Fitting]", name),
            color,
            points: pts,
            faces,
            ..Default::default()
        });
        return true;
    }
    false
}

/// Parse Autodesk Proxy Graphics binary stream for 3D Shells, Meshes, Plant 3D Piping Fittings, and Structural Steel Members.
fn process_proxy_graphics(
    data: &[u8],
    matrix: Matrix4,
    color: [u8; 3],
    name: &str,
    out: &mut Output,
) -> bool {
    if data.len() < 8 {
        return false;
    }
    let total_size = match data.get(0..4) {
        Some(b) => u32::from_le_bytes(b.try_into().unwrap()) as usize,
        None => return false,
    };
    let record_count = match data.get(4..8) {
        Some(b) => u32::from_le_bytes(b.try_into().unwrap()) as usize,
        None => return false,
    };
    if total_size < 8 || total_size > data.len() || record_count > 500_000 {
        return false;
    }

    let mut opcodes = BTreeMap::new();
    let mut current_color = color;
    let mut scope_mesh_start = out.meshes.len();
    let mut scope_starts = Vec::new();
    let mut primitive_stack = Vec::new();
    let mut explicit_geometry = false;
    let mut offset = 8;
    let initial_count = out.meshes.len();
    let mut matrix_stack: Vec<Matrix4> = Vec::new();
    let mut cur_matrix = matrix;

    let mut scope_circles: Vec<ProxyCircle3D> = Vec::new();
    let mut scope_arcs: Vec<[Vector3; 3]> = Vec::new();
    let mut scope_polylines: Vec<Vec<Vector3>> = Vec::new();
    let mut all_polylines_world: Vec<[Vector3; 2]> = Vec::new();
    let mut last_poly_segment: Option<[Vector3; 2]> = None;

    for _ in 0..record_count {
        if offset + 8 > total_size {
            break;
        }
        let record_size = match data.get(offset..offset + 4) {
            Some(b) => u32::from_le_bytes(b.try_into().unwrap()) as usize,
            None => break,
        };
        let record_type = match data.get(offset + 4..offset + 8) {
            Some(b) => u32::from_le_bytes(b.try_into().unwrap()),
            None => break,
        };
        if record_size < 8 || offset + record_size > total_size {
            break;
        }
        let payload = &data[offset + 8..offset + record_size];

        *opcodes.entry(record_type).or_insert(0usize) += 1;
        match record_type {
            14 | 22 if payload.len() >= 4 => {
                let raw = u32::from_le_bytes(payload[..4].try_into().unwrap());
                current_color = if record_type == 14 {
                    Color::from_index(raw as i16)
                        .rgb()
                        .map(|(r, g, b)| [r, g, b])
                        .unwrap_or(color)
                } else {
                    match raw >> 24 {
                        0xC2 => [
                            ((raw >> 16) & 255) as u8,
                            ((raw >> 8) & 255) as u8,
                            (raw & 255) as u8,
                        ],
                        0xC3 => Color::from_index((raw & 255) as i16)
                            .rgb()
                            .map(|(r, g, b)| [r, g, b])
                            .unwrap_or(color),
                        _ => color,
                    }
                };
            }
            4 if payload.len() >= 88 => {
                let f =
                    |i: usize| f64::from_le_bytes(payload[i * 8..(i + 1) * 8].try_into().unwrap());
                let center = Vector3::new(f(0), f(1), f(2));
                let radius = f(3);
                let normal = Vector3::new(f(4), f(5), f(6));
                let start = Vector3::new(f(7), f(8), f(9));
                let sweep = f(10);
                if radius.is_finite()
                    && radius > 0.0
                    && normal.length_squared() > 1e-12
                    && start.length_squared() > 1e-12
                    && sweep.is_finite()
                {
                    let u = start.normalize();
                    let v = normal.normalize().cross(&u);
                    let at = |t: f64| center + (u * t.cos() + v * t.sin()) * radius;
                    scope_arcs.push([at(0.0), at(sweep * 0.5), at(sweep)]);
                }
            }
            29 => {
                // PushModelTransform (16 f64s = 128 bytes row-major 4x4 matrix)
                if payload.len() >= 128 {
                    let mut m = [[0.0f64; 4]; 4];
                    for r in 0..4 {
                        for c in 0..4 {
                            let idx = (r * 4 + c) * 8;
                            m[r][c] = f64::from_le_bytes(payload[idx..idx + 8].try_into().unwrap());
                        }
                    }
                    primitive_stack.push((
                        std::mem::take(&mut scope_circles),
                        std::mem::take(&mut scope_arcs),
                        std::mem::take(&mut scope_polylines),
                        last_poly_segment.take(),
                        explicit_geometry,
                    ));
                    explicit_geometry = false;
                    scope_starts.push(scope_mesh_start);
                    scope_mesh_start = out.meshes.len();
                    matrix_stack.push(cur_matrix);
                    cur_matrix = cur_matrix * Matrix4 { m };
                }
            }
            31 => {
                // PopModelTransform: flush any fitting primitives accumulated in this local transform scope
                if !scope_circles.is_empty() && !explicit_geometry {
                    reconstruct_fitting_from_primitives(
                        &scope_circles,
                        &scope_arcs,
                        &scope_polylines,
                        cur_matrix,
                        current_color,
                        name,
                        out,
                    );
                    scope_circles.clear();
                    scope_arcs.clear();
                    scope_polylines.clear();
                }
                scope_circles.clear();
                scope_arcs.clear();
                scope_polylines.clear();
                if let Some((circles, arcs, polylines, segment, has_explicit)) =
                    primitive_stack.pop()
                {
                    scope_circles = circles;
                    scope_arcs = arcs;
                    scope_polylines = polylines;
                    last_poly_segment = segment;
                    explicit_geometry = has_explicit;
                }
                scope_mesh_start = scope_starts.pop().unwrap_or(initial_count);
                if let Some(prev) = matrix_stack.pop() {
                    cur_matrix = prev;
                }
            }
            2 => {
                // Circle (center, radius, normal) = 7 f64s = 56 bytes
                if payload.len() >= 56 {
                    let f = |i: usize| {
                        f64::from_le_bytes(payload[i * 8..(i + 1) * 8].try_into().unwrap())
                    };
                    let center = Vector3::new(f(0), f(1), f(2));
                    let radius = f(3);
                    let normal = Vector3::new(f(4), f(5), f(6));
                    if radius.is_finite()
                        && radius >= 0.25
                        && radius <= 50_000.0
                        && normal.length_squared() > 1e-8
                    {
                        scope_circles.push(ProxyCircle3D {
                            center,
                            radius,
                            normal: normal.normalize(),
                        });
                    }
                }
            }
            3 => {
                // Circle3Pt = 9 f64s = 72 bytes
                if payload.len() >= 72 {
                    let f = |i: usize| {
                        f64::from_le_bytes(payload[i * 8..(i + 1) * 8].try_into().unwrap())
                    };
                    let p1 = Vector3::new(f(0), f(1), f(2));
                    let p2 = Vector3::new(f(3), f(4), f(5));
                    let p3 = Vector3::new(f(6), f(7), f(8));
                    if let Some(c) = circle_from_3_pts(p1, p2, p3) {
                        scope_circles.push(c);
                    }
                }
            }
            5 => {
                // CircularArc3Pt = 9 f64s + u32 = 76 bytes
                if payload.len() >= 72 {
                    let f = |i: usize| {
                        f64::from_le_bytes(payload[i * 8..(i + 1) * 8].try_into().unwrap())
                    };
                    let p1 = Vector3::new(f(0), f(1), f(2));
                    let p2 = Vector3::new(f(3), f(4), f(5));
                    let p3 = Vector3::new(f(6), f(7), f(8));
                    scope_arcs.push([p1, p2, p3]);
                }
            }
            6 | 32 => {
                // Polyline = u32 count + count * 24 bytes
                if payload.len() >= 4 {
                    let cnt = u32::from_le_bytes(payload[0..4].try_into().unwrap()) as usize;
                    if cnt >= 2 && cnt <= 100_000 && 4 + cnt * 24 <= payload.len() {
                        let mut pts = Vec::with_capacity(cnt);
                        for i in 0..cnt {
                            let base = 4 + i * 24;
                            let x = f64::from_le_bytes(payload[base..base + 8].try_into().unwrap());
                            let y = f64::from_le_bytes(
                                payload[base + 8..base + 16].try_into().unwrap(),
                            );
                            let z = f64::from_le_bytes(
                                payload[base + 16..base + 24].try_into().unwrap(),
                            );
                            pts.push(Vector3::new(x, y, z));
                        }
                        if (pts[cnt - 1] - pts[0]).length() > 1e-3 || cnt >= 12 {
                            if cnt == 2 {
                                last_poly_segment = Some([pts[0], pts[1]]);
                                let w0 = cur_matrix.transform_point(pts[0]);
                                let w1 = cur_matrix.transform_point(pts[1]);
                                all_polylines_world.push([w0, w1]);
                            }
                            scope_polylines.push(pts);
                        }
                    }
                }
            }
            7 if payload.len() >= 4 => {
                let count = u32::from_le_bytes(payload[..4].try_into().unwrap()) as usize;
                if count >= 3 && count <= 1_000_000 && 4 + count * 24 <= payload.len() {
                    let mut points = Vec::with_capacity(count);
                    for i in 0..count {
                        let at = 4 + i * 24;
                        let f = |j: usize| {
                            f64::from_le_bytes(
                                payload[at + j * 8..at + (j + 1) * 8].try_into().unwrap(),
                            )
                        };
                        points.push([f(0), f(1), f(2)]);
                    }
                    let mut faces = Vec::new();
                    append_proxy_polygon(&mut points, &[(0..count).collect()], &mut faces);
                    if !faces.is_empty() {
                        let points = points
                            .iter()
                            .map(|p| {
                                let q = cur_matrix.transform_point(Vector3::new(p[0], p[1], p[2]));
                                [q.x, q.y, q.z]
                            })
                            .collect();
                        explicit_geometry = true;
                        out.meshes.push(Mesh {
                            name: format!("{} [Plant3D-Polygon]", name),
                            color: current_color,
                            points,
                            faces,
                            ..Default::default()
                        });
                    }
                }
            }
            9 => {
                // Opcode 9 = SHELL
                if let Some((local_pts, faces)) =
                    parse_proxy_shell_local(payload, last_poly_segment)
                {
                    let points = local_pts
                        .into_iter()
                        .map(|p| {
                            let tp = cur_matrix.transform_point(Vector3::new(p[0], p[1], p[2]));
                            [tp.x, tp.y, tp.z]
                        })
                        .collect();
                    explicit_geometry = true;
                    out.meshes.push(Mesh {
                        name: format!("{} [Plant3D]", name),
                        color: current_color,
                        points,
                        faces,
                        ..Default::default()
                    });
                }
            }
            8 => {
                // Opcode 8 = MESH
                if decode_proxy_mesh(payload, cur_matrix, current_color, name, out) {
                    explicit_geometry = true;
                }
            }
            _ => {}
        }
        offset += record_size;
    }

    // Flush any remaining fitting primitives outside a PopModelTransform
    if !explicit_geometry && !scope_circles.is_empty() {
        reconstruct_fitting_from_primitives(
            &scope_circles,
            &scope_arcs,
            &scope_polylines,
            cur_matrix,
            current_color,
            name,
            out,
        );
    }

    // Fallback for wireframe-only structural members / handrails (e.g. E019..E025 in Doghouse)
    if out.meshes.len() == initial_count && !all_polylines_world.is_empty() {
        let mut pts: Vec<[f64; 3]> = Vec::new();
        let mut faces: Vec<usize> = Vec::new();
        let tube_r = 21.0; // standard 1-1/4" industrial handrail tube radius (42mm OD)
        let segs = 8usize;
        for [w0, w1] in &all_polylines_world {
            let axis = *w1 - *w0;
            let len = axis.length();
            if len > 5.0 {
                let c0 = ProxyCircle3D {
                    center: *w0,
                    radius: tube_r,
                    normal: axis.normalize(),
                };
                let c1 = ProxyCircle3D {
                    center: *w1,
                    radius: tube_r,
                    normal: axis.normalize(),
                };
                emit_frustum_between_circles(
                    c0,
                    c1,
                    segs,
                    Matrix4::identity(),
                    &mut pts,
                    &mut faces,
                );
            }
        }
        if !faces.is_empty() {
            out.meshes.push(Mesh {
                name: format!("{} [Plant3D-Structure]", name),
                color: current_color,
                points: pts,
                faces,
                ..Default::default()
            });
        }
    }

    if std::env::var_os("PLANTVIEW_DWG_AUDIT").is_some() {
        eprintln!(
            "[DWG proxy ops] {} {:?} meshes={}",
            name,
            opcodes,
            out.meshes.len() - initial_count
        );
    }
    out.meshes.len() > initial_count
}

fn push_diagnostic(out: &mut Output, diag: EntityDiagnostic) {
    if diag.missing_faces > 0 || !diag.status.starts_with("SUCCESS") || out.diagnostics.len() < 50 {
        out.diagnostics.push(diag);
    }
}

/// Process conventional solid entities (Solid3D, Body, Region, Surface)
/// following strict priority: Native ACIS -> Proxy GraphicData Fallback -> Wire Fallback.
fn process_solid_entity(
    doc: &CadDocument,
    common_handle: u64,
    common_layer: &str,
    entity_kind: &str,
    acis_data: &AcisData,
    graphic_data: Option<&[u8]>,
    solid_wires: Option<&[acadrust::entities::solid3d::Wire]>,
    matrix: Matrix4,
    color: [u8; 3],
    name: &str,
    acis_cache: &mut HashMap<u64, Option<Vec<LocalMesh>>>,
    out: &mut Output,
) {
    out.coverage.total_solids_found += 1;

    let mut diag = EntityDiagnostic {
        handle: format!("{:X}", common_handle),
        layer: common_layer.to_string(),
        entity_type: entity_kind.to_string(),
        has_acis: acis_data.has_data(),
        acis_size: acis_data.size(),
        acis_format: if acis_data.is_binary {
            "SAB".to_string()
        } else if !acis_data.sat_data.is_empty() {
            "SAT".to_string()
        } else {
            "NONE".to_string()
        },
        parse_result: "Pending".to_string(),
        bodies_count: 0,
        reconstructed_faces: 0,
        missing_faces: 0,
        vertices: 0,
        triangles: 0,
        bounds: None,
        status: "PENDING".to_string(),
        notes: String::new(),
    };

    // ========================================================================
    // PRIORITY 1: Native ACIS B-Rep Geometry
    // ========================================================================
    let skip_acis = match acis_cache.get(&common_handle) {
        Some(Some(cached_meshes)) => {
            let mut total_v = 0;
            let mut total_t = 0;
            let mut all_points = Vec::new();
            for (index, local) in cached_meshes.iter().enumerate() {
                let points = local
                    .local_points
                    .iter()
                    .map(|p| {
                        let p = matrix.transform_point(*p);
                        [p.x, p.y, p.z]
                    })
                    .collect::<Vec<_>>();
                total_v += points.len();
                total_t += local.faces.len() / 4;
                diag.missing_faces += local.missing_faces;
                all_points.extend(points.clone());
                out.meshes.push(Mesh {
                    name: format!("{} #{}", name, index + 1),
                    color: local
                        .color
                        .map(|c| rgb(doc, c, common_layer, color))
                        .unwrap_or(color),
                    points,
                    faces: local.faces.clone(),
                    ..Default::default()
                });
            }
            diag.parse_result = "Cached".to_string();
            diag.status = "SUCCESS_ACIS_CACHE".to_string();
            if diag.missing_faces > 0 {
                out.warnings.push(format!(
                    "Sólido {:X}: {} face(s) ACIS não reconstruída(s)",
                    common_handle, diag.missing_faces
                ));
            }
            diag.vertices = total_v;
            diag.triangles = total_t;
            diag.bounds = compute_bounds(&all_points);
            out.coverage.acis_success += 1;
            push_diagnostic(out, diag);
            return;
        }
        Some(None) => true,
        None => false,
    };

    if !skip_acis {
        let parsed_sat = acis_data.parse();
        if let Some(sat) = parsed_sat {
            diag.parse_result = "Ok".to_string();
            let (bodies, loss) = acis::lift(&sat);
            diag.bodies_count = bodies.len();

            let mut local_meshes = Vec::new();
            let mut total_v = 0;
            let mut total_t = 0;
            let mut total_missing = 0;
            let mut all_local_pts: Vec<[f64; 3]> = Vec::new();
            let tol = if acis_data.size() > 40_000 {
                mesh::TessellationTolerance::new(0.55, 1.5)
            } else {
                mesh::TessellationTolerance::new(0.40, 1.0)
            };

            for body in bodies.iter() {
                let locals = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    tessellate_local(body, &sat, tol)
                })).unwrap_or_default();
                for local in locals {
                    total_missing += local.missing_faces;
                    total_v += local.local_points.len();
                    total_t += local.faces.len() / 4;
                    all_local_pts.extend(local.local_points.iter().map(|p| [p.x, p.y, p.z]));
                    local_meshes.push(local);
                }
            }

            // Validate ACIS local bounds against inline DWG wires when present so a
            // mis-attached SAB body from a corrupted datastore can never explode the scene.
            let mut bounds_mismatch = false;
            if let (Some(wires), Some(ab)) = (solid_wires, compute_bounds(&all_local_pts)) {
                let mut w_pts: Vec<[f64; 3]> = Vec::new();
                for w in wires {
                    for pt in &w.points {
                        w_pts.push([pt.x, pt.y, pt.z]);
                    }
                }
                if w_pts.len() >= 4 {
                    if let Some(wb) = compute_bounds(&w_pts) {
                        let w_span = ((wb[1] - wb[0]).powi(2)
                            + (wb[3] - wb[2]).powi(2)
                            + (wb[5] - wb[4]).powi(2))
                        .sqrt();
                        let a_span = ((ab[1] - ab[0]).powi(2)
                            + (ab[3] - ab[2]).powi(2)
                            + (ab[5] - ab[4]).powi(2))
                        .sqrt();
                        if w_span > 1e-4 {
                            let w_cx = 0.5 * (wb[0] + wb[1]);
                            let w_cy = 0.5 * (wb[2] + wb[3]);
                            let w_cz = 0.5 * (wb[4] + wb[5]);
                            let a_cx = 0.5 * (ab[0] + ab[1]);
                            let a_cy = 0.5 * (ab[2] + ab[3]);
                            let a_cz = 0.5 * (ab[4] + ab[5]);
                            let center_dist = ((a_cx - w_cx).powi(2)
                                + (a_cy - w_cy).powi(2)
                                + (a_cz - w_cz).powi(2))
                            .sqrt();
                            if center_dist > (w_span * 10.0).max(25.0)
                                || a_span > (w_span * 25.0).max(25.0)
                            {
                                bounds_mismatch = true;
                                diag.notes = format!(
                                    "ACIS descartado por divergencia espacial com wires (dist={:.1}, a_span={:.1}, w_span={:.1})",
                                    center_dist, a_span, w_span
                                );
                            }
                        }
                    }
                }
            }

            if !bounds_mismatch && !local_meshes.is_empty() {
                let mut all_points = Vec::new();
                for (index, local) in local_meshes.iter().enumerate() {
                    let points = local
                        .local_points
                        .iter()
                        .map(|p| {
                            let p = matrix.transform_point(*p);
                            [p.x, p.y, p.z]
                        })
                        .collect::<Vec<_>>();
                    all_points.extend(points.clone());
                    out.meshes.push(Mesh {
                        name: format!("{} #{}", name, index + 1),
                        color: local
                            .color
                            .map(|c| rgb(doc, c, common_layer, color))
                            .unwrap_or(color),
                        points,
                        faces: local.faces.clone(),
                        ..Default::default()
                    });
                }
                diag.missing_faces = total_missing;
                diag.reconstructed_faces = total_t;
                diag.vertices = total_v;
                diag.triangles = total_t;
                diag.bounds = compute_bounds(&all_points);
                acis_cache.insert(common_handle, Some(local_meshes));
                diag.status = "SUCCESS_ACIS_NATIVE".to_string();
                out.coverage.acis_success += 1;
                push_diagnostic(out, diag);
                return;
            } else if !bounds_mismatch {
                diag.notes = format!("ACIS corpos vazios: loss={:?}", loss);
            }
            acis_cache.insert(common_handle, None);
        } else {
            diag.parse_result = "ParseFailed".to_string();
            diag.notes = "acis_data.parse() retornou None".to_string();
            acis_cache.insert(common_handle, None);
        }
    } else {
        diag.parse_result = "CachedSkip".to_string();
    }

    // ========================================================================
    // PRIORITY 2: GraphicData Proxy Fallback (pre-rendered representation)
    // ========================================================================
    if let Some(gd) = graphic_data {
        if !gd.is_empty() {
            let initial_len = out.meshes.len();
            if process_proxy_graphics(gd, matrix, color, name, out) {
                let added = out.meshes.len() - initial_len;
                let mut added_pts = 0;
                let mut added_tri = 0;
                let mut all_fallback_pts = Vec::new();
                for m in &out.meshes[initial_len..] {
                    added_pts += m.points.len();
                    added_tri += m.faces.len() / 4;
                    all_fallback_pts.extend(m.points.clone());
                }
                diag.status = "FALLBACK_GRAPHIC_DATA".to_string();
                diag.vertices = added_pts;
                diag.triangles = added_tri;
                diag.bounds = compute_bounds(&all_fallback_pts);
                diag.notes = format!("Recuperado via proxy graphic_data: {} malhas", added);
                out.coverage.proxy_fallback += 1;
                push_diagnostic(out, diag);
                return;
            }
        }
    }

    // ========================================================================
    // PRIORITY 3: Wires 3D Fallback (solid boundary curves & isoparametrics)
    // ========================================================================
    if let Some(wires) = solid_wires {
        let mut wire_pts = Vec::new();
        for wire in wires {
            for pt in &wire.points {
                let wp = matrix.transform_point(*pt);
                wire_pts.push([wp.x, wp.y, wp.z]);
            }
        }
        if wire_pts.len() >= 3 {
            let mut faces = Vec::new();
            for i in 1..wire_pts.len().saturating_sub(1) {
                faces.extend([3, 0, i, i + 1]);
            }
            if !faces.is_empty() {
                diag.bounds = compute_bounds(&wire_pts);
                diag.vertices = wire_pts.len();
                diag.triangles = faces.len() / 4;
                diag.status = "FALLBACK_WIRE".to_string();
                diag.notes = format!("Recuperado via arames 3D ({} fios)", wires.len());
                out.meshes.push(Mesh {
                    name: format!("{} [WireFrame]", name),
                    color,
                    points: wire_pts,
                    faces,
                    ..Default::default()
                });
                out.coverage.wire_fallback += 1;
                push_diagnostic(out, diag);
                return;
            }
        }
    }

    // ========================================================================
    // FAILURE REPORTING (Never mask failures as success)
    // ========================================================================
    diag.status = "FAILED".to_string();
    out.coverage.failed_solids += 1;
    out.warnings.push(format!(
        "Falha ao reconstruir solido 3D: handle {:X}, layer {}. Sem geometria recuperavel.",
        common_handle, common_layer
    ));
    push_diagnostic(out, diag);
}

fn proxy_bounds_and_root(data: &[u8]) -> Option<([f64; 6], Matrix4)> {
    if data.len() < 8 {
        return None;
    }
    let total = u32::from_le_bytes(data[..4].try_into().ok()?) as usize;
    let count = u32::from_le_bytes(data[4..8].try_into().ok()?) as usize;
    if total > data.len() || count > 500_000 {
        return None;
    }
    let mut offset = 8;
    let mut current = Matrix4::identity();
    let mut stack = Vec::new();
    let mut root = None;
    let mut points = Vec::new();
    for _ in 0..count {
        if offset + 8 > total {
            break;
        }
        let size = u32::from_le_bytes(data[offset..offset + 4].try_into().ok()?) as usize;
        let kind = u32::from_le_bytes(data[offset + 4..offset + 8].try_into().ok()?);
        if size < 8 || offset + size > total {
            return None;
        }
        let payload = &data[offset + 8..offset + size];
        let f = |i: usize| f64::from_le_bytes(payload[i * 8..(i + 1) * 8].try_into().unwrap());
        let mut local = Vec::new();
        match kind {
            29 if payload.len() >= 128 => {
                let mut m = [[0.; 4]; 4];
                for r in 0..4 {
                    for c in 0..4 {
                        m[r][c] = f(r * 4 + c);
                    }
                }
                let next = Matrix4 { m };
                if stack.is_empty() && root.is_none() {
                    root = Some(next);
                }
                stack.push(current);
                current = current * next;
            }
            31 => {
                if let Some(previous) = stack.pop() {
                    current = previous;
                }
            }
            6 | 7 | 32 if payload.len() >= 4 => {
                let n = u32::from_le_bytes(payload[..4].try_into().ok()?) as usize;
                if n <= 1_000_000 && 4 + n * 24 <= payload.len() {
                    for i in 0..n {
                        let k = 4 + i * 24;
                        local.push(Vector3::new(
                            f64::from_le_bytes(payload[k..k + 8].try_into().unwrap()),
                            f64::from_le_bytes(payload[k + 8..k + 16].try_into().unwrap()),
                            f64::from_le_bytes(payload[k + 16..k + 24].try_into().unwrap()),
                        ));
                    }
                }
            }
            3 if payload.len() >= 72 => {
                if let Some(c) = circle_from_3_pts(
                    Vector3::new(f(0), f(1), f(2)),
                    Vector3::new(f(3), f(4), f(5)),
                    Vector3::new(f(6), f(7), f(8)),
                ) {
                    let (u, v) = orthonormal_basis(c.normal);
                    for i in 0..32 {
                        let angle = i as f64 * std::f64::consts::TAU / 32.;
                        local.push(c.center + (u * angle.cos() + v * angle.sin()) * c.radius);
                    }
                }
            }
            2 if payload.len() >= 56 => {
                let c = Vector3::new(f(0), f(1), f(2));
                let n = Vector3::new(f(4), f(5), f(6));
                if n.length_squared() > 1e-12 {
                    let (u, v) = orthonormal_basis(n);
                    for i in 0..32 {
                        let angle = i as f64 * std::f64::consts::TAU / 32.;
                        local.push(c + (u * angle.cos() + v * angle.sin()) * f(3));
                    }
                }
            }
            5 if payload.len() >= 72 => {
                for i in 0..3 {
                    local.push(Vector3::new(f(i * 3), f(i * 3 + 1), f(i * 3 + 2)));
                }
            }
            _ => {}
        }
        for p in local {
            let q = current.transform_point(p);
            if q.x.is_finite() && q.y.is_finite() && q.z.is_finite() {
                points.push([q.x, q.y, q.z]);
            }
        }
        offset += size;
    }
    Some((
        compute_bounds(&points)?,
        root.unwrap_or(Matrix4::identity()),
    ))
}

fn plant_block_reference(
    doc: &CadDocument,
    value: &acadrust::entities::UnknownEntity,
) -> Option<(String, Matrix4, [f64; 6])> {
    use acadrust::io::dwg::{
        dwg_stream_readers::bit_reader::DwgBitReader, dwg_version::DwgVersion,
    };
    if !value.dxf_name.to_ascii_uppercase().starts_with("ACPP") {
        return None;
    }
    let raw = value.raw_dwg_data.as_ref()?;
    let dxf = value.dwg_source_version?;
    if dxf < acadrust::types::DxfVersion::AC1024 || value.dwg_handle_bits <= 0 {
        return None;
    }
    let start = raw.len() as i64 * 8 - value.dwg_handle_bits;
    if start < 0 {
        return None;
    }
    let mut reader = DwgBitReader::new(raw.clone(), DwgVersion::from_dxf_version(dxf).ok()?, dxf);
    reader.set_position_in_bits(start);
    let mut referenced = Vec::new();
    while reader.position_in_bits() + 8 <= raw.len() as i64 * 8 {
        let before = reader.position_in_bits();
        let h = reader.read_handle_relative(value.common.handle.value());
        if reader.check_error().is_err() || reader.position_in_bits() <= before {
            break;
        }
        if let Some(block) = doc.block_records.iter().find(|b| b.handle.value() == h) {
            if block.name != "*Model_Space"
                && !block.name.starts_with("*Paper_Space")
                && !block.flags.is_xref
                && !block.flags.is_xref_overlay
                && !block.flags.is_external
            {
                referenced.push(block.name.clone());
            }
        }
    }
    referenced.sort();
    referenced.dedup();
    if referenced.len() != 1 {
        return None;
    }
    let (bounds, root) = proxy_bounds_and_root(value.common.graphic_data.as_deref()?)?;
    Some((referenced.pop()?, root, bounds))
}

fn walk(
    doc: &CadDocument,
    entity: &EntityType,
    matrix: Matrix4,
    prefix: &str,
    depth: usize,
    parent_color: [u8; 3],
    parent_layer: &str,
    visited_blocks: &mut Vec<String>,
    acis_cache: &mut HashMap<u64, Option<Vec<LocalMesh>>>,
    out: &mut Output,
) {
    if depth > 32 {
        out.warnings
            .push("Bloco recursivo ou profundidade superior a 32".into());
        return;
    }
    let common = entity.common();
    if common.invisible {
        return;
    }
    let effective_layer = if depth > 0 && common.layer == "0" {
        parent_layer
    } else {
        common.layer.as_str()
    };
    let color = rgb(doc, common.color, effective_layer, parent_color);
    let name = format!(
        "{}{:X} | {}",
        prefix,
        common.handle.value(),
        effective_layer
    );
    if std::env::var_os("PLANTVIEW_DWG_AUDIT").is_some() {
        if let Some(data) = &common.graphic_data {
            if data.len() > 8 {
                eprintln!(
                    "[DWG proxy] handle={:X} bytes={} type={}",
                    common.handle.value(),
                    data.len(),
                    entity.as_entity().entity_type()
                );
            }
        }
    }

    // 1. Block Inserts (References)
    if let EntityType::Insert(insert) = entity {
        if insert.block_name.eq_ignore_ascii_case("*Model_Space")
            || insert.block_name.eq_ignore_ascii_case("*Paper_Space")
        {
            return;
        }
        if visited_blocks
            .iter()
            .any(|b| b.eq_ignore_ascii_case(&insert.block_name))
        {
            return; // Cycle detection
        }
        let Some(block) = doc.block_records.get(&insert.block_name) else {
            out.warnings
                .push(format!("Bloco ausente: {}", insert.block_name));
            return;
        };
        if block.flags.is_xref || block.flags.is_xref_overlay || block.flags.is_external {
            out.warnings.push(format!(
                "Xref nao carregada: {}. Adicione o DWG referenciado separadamente.",
                insert.block_name
            ));
            return;
        }
        if insert.row_count > 1 || insert.column_count > 1 {
            out.warnings
                .push(format!("MINSERT nao suportado: {}", name));
            return;
        }

        let b = block.base_point;
        let world = matrix * insert.get_transform().matrix * Matrix4::translation(-b.x, -b.y, -b.z);
        visited_blocks.push(insert.block_name.clone());
        for child in doc.entities_in_block(&insert.block_name) {
            walk(
                doc,
                child,
                world,
                &format!("{}{}/", prefix, insert.block_name),
                depth + 1,
                color,
                effective_layer,
                visited_blocks,
                acis_cache,
                out,
            );
        }
        visited_blocks.pop();
        return;
    }

    // 2. Conventional 3D Solides / Bodies / Regions / Surfaces (Priority 3: Native -> Proxy -> Wire)
    match entity {
        EntityType::Solid3D(s) => {
            process_solid_entity(
                doc,
                common.handle.value(),
                effective_layer,
                "Solid3D",
                &s.acis_data,
                common.graphic_data.as_deref(),
                Some(&s.wires),
                matrix,
                color,
                &name,
                acis_cache,
                out,
            );
            return;
        }
        EntityType::Body(b) => {
            process_solid_entity(
                doc,
                common.handle.value(),
                effective_layer,
                "Body",
                &b.acis_data,
                common.graphic_data.as_deref(),
                None,
                matrix,
                color,
                &name,
                acis_cache,
                out,
            );
            return;
        }
        EntityType::Region(r) => {
            process_solid_entity(
                doc,
                common.handle.value(),
                effective_layer,
                "Region",
                &r.acis_data,
                common.graphic_data.as_deref(),
                None,
                matrix,
                color,
                &name,
                acis_cache,
                out,
            );
            return;
        }
        EntityType::Surface(s) => {
            process_solid_entity(
                doc,
                common.handle.value(),
                effective_layer,
                "Surface",
                &s.acis_data,
                common.graphic_data.as_deref(),
                None,
                matrix,
                color,
                &name,
                acis_cache,
                out,
            );
            return;
        }
        _ => {}
    }

    // 3. PolyfaceMesh (Standard Plant 3D & AutoCAD 3D Mesh)
    if let EntityType::PolyfaceMesh(pf) = entity {
        if !pf.vertices.is_empty() && !pf.faces.is_empty() {
            let mut points = Vec::with_capacity(pf.vertices.len());
            for v in &pf.vertices {
                let p = matrix.transform_point(v.location);
                points.push([p.x, p.y, p.z]);
            }
            let mut faces = Vec::new();
            for face in &pf.faces {
                let i1 = face.index1.abs() as usize;
                let i2 = face.index2.abs() as usize;
                let i3 = face.index3.abs() as usize;
                let i4 = face.index4.abs() as usize;
                let n_pts = points.len();
                if i1 > 0 && i2 > 0 && i3 > 0 && i1 <= n_pts && i2 <= n_pts && i3 <= n_pts {
                    if i4 == 0 {
                        faces.extend([3, i1 - 1, i2 - 1, i3 - 1]);
                    } else if i4 <= n_pts {
                        faces.extend([3, i1 - 1, i2 - 1, i3 - 1, 3, i1 - 1, i3 - 1, i4 - 1]);
                    }
                }
            }
            if !faces.is_empty() {
                out.meshes.push(Mesh {
                    name: format!("{} #1", name),
                    color,
                    points,
                    faces,
                    ..Default::default()
                });
                return;
            }
        }
    }

    // 4. SubD / Polygon Mesh (EntityType::Mesh)
    if let EntityType::Mesh(m) = entity {
        if !m.vertices.is_empty() && !m.faces.is_empty() {
            let mut points = Vec::with_capacity(m.vertices.len());
            for v in &m.vertices {
                let p = matrix.transform_point(*v);
                points.push([p.x, p.y, p.z]);
            }
            let mut faces = Vec::new();
            for f in &m.faces {
                if f.vertices.len() == 3 {
                    faces.extend([3, f.vertices[0], f.vertices[1], f.vertices[2]]);
                } else if f.vertices.len() == 4 {
                    faces.extend([
                        3,
                        f.vertices[0],
                        f.vertices[1],
                        f.vertices[2],
                        3,
                        f.vertices[0],
                        f.vertices[2],
                        f.vertices[3],
                    ]);
                } else if f.vertices.len() > 4 {
                    for k in 1..f.vertices.len() - 1 {
                        faces.extend([3, f.vertices[0], f.vertices[k], f.vertices[k + 1]]);
                    }
                }
            }
            if !faces.is_empty() {
                out.meshes.push(Mesh {
                    name: format!("{} #1", name),
                    color,
                    points,
                    faces,
                    ..Default::default()
                });
                return;
            }
        }
    }

    // 5. 3DFace (Facet surfaces)
    if let EntityType::Face3D(f) = entity {
        let p1 = matrix.transform_point(f.first_corner);
        let p2 = matrix.transform_point(f.second_corner);
        let p3 = matrix.transform_point(f.third_corner);
        let p4 = matrix.transform_point(f.fourth_corner);
        let points = vec![
            [p1.x, p1.y, p1.z],
            [p2.x, p2.y, p2.z],
            [p3.x, p3.y, p3.z],
            [p4.x, p4.y, p4.z],
        ];
        let faces = if f.third_corner == f.fourth_corner {
            vec![3, 0, 1, 2]
        } else {
            vec![3, 0, 1, 2, 3, 0, 2, 3]
        };
        out.meshes.push(Mesh {
            name: format!("{} #1", name),
            color,
            points,
            faces,
            ..Default::default()
        });
        return;
    }

    // Follow the explicit catalog block pointer and validate its saved placement
    // against the original proxy extents before accepting exact B-rep geometry.
    if let EntityType::Unknown(value) = entity {
        if let Some((block_name, placement, saved_bounds)) = plant_block_reference(doc, value) {
            if !visited_blocks
                .iter()
                .any(|b| b.eq_ignore_ascii_case(&block_name))
            {
                let mut recovered = Output::default();
                visited_blocks.push(block_name.clone());
                for child in doc.entities_in_block(&block_name) {
                    walk(
                        doc,
                        child,
                        matrix * placement,
                        &format!("{}{}/", prefix, block_name),
                        depth + 1,
                        color,
                        effective_layer,
                        visited_blocks,
                        acis_cache,
                        &mut recovered,
                    );
                }
                visited_blocks.pop();
                let actual: Vec<_> = recovered
                    .meshes
                    .iter()
                    .flat_map(|m| m.points.iter().copied())
                    .collect();
                let mut expected = Vec::new();
                for x in [saved_bounds[0], saved_bounds[3]] {
                    for y in [saved_bounds[1], saved_bounds[4]] {
                        for z in [saved_bounds[2], saved_bounds[5]] {
                            let p = matrix.transform_point(Vector3::new(x, y, z));
                            expected.push([p.x, p.y, p.z]);
                        }
                    }
                }
                if let (Some(ab), Some(eb)) = (compute_bounds(&actual), compute_bounds(&expected)) {
                    let span = (eb[3] - eb[0]).max(eb[4] - eb[1]).max(eb[5] - eb[2]);
                    let tolerance = (span * 0.05).max(2.);
                    let error = (0..6).map(|i| (ab[i] - eb[i]).abs()).fold(0., f64::max);
                    if std::env::var_os("PLANTVIEW_DWG_AUDIT").is_some() {
                        eprintln!(
                            "[Plant exact bounds] {:X} block={} error={} tolerance={} meshes={}",
                            value.common.handle.value(),
                            block_name,
                            error,
                            tolerance,
                            recovered.meshes.len()
                        );
                    }
                    if error <= tolerance && !recovered.meshes.is_empty() {
                        out.meshes.extend(recovered.meshes);
                        out.diagnostics.extend(recovered.diagnostics);
                        out.warnings.extend(recovered.warnings);
                        out.coverage.total_solids_found += recovered.coverage.total_solids_found;
                        out.coverage.acis_success += recovered.coverage.acis_success;
                        out.coverage.failed_solids += recovered.coverage.failed_solids;
                        out.coverage.proxy_fallback += recovered.coverage.proxy_fallback;
                        out.coverage.wire_fallback += recovered.coverage.wire_fallback;
                        return;
                    }
                }
            }
        }
    }

    // 6. Extended Entities (AutoCAD Plant 3D / Advance Steel / Civil Proxy Entities)
    if let EntityType::Extended(ext) = entity {
        if let ExtendedEntityData::Proxy(proxy) = &ext.data {
            let data = proxy.graphics.data();
            if !data.is_empty() && process_proxy_graphics(&data, matrix, color, &name, out) {
                return;
            }
        }
    }

    // 7. Check common graphic_data for any remaining custom/unknown entities
    if let Some(gd) = &common.graphic_data {
        if !gd.is_empty() && process_proxy_graphics(gd.as_slice(), matrix, color, &name, out) {
            return;
        }
    }

    *out.skipped
        .entry(entity.as_entity().entity_type().to_string())
        .or_default() += 1;
}

fn fixture(path: &str, offset: [f64; 3]) -> Result<(), Box<dyn std::error::Error>> {
    use acadrust::entities::acis::primitives;
    let mut doc = CadDocument::new();
    doc.header.insertion_units = 4;
    let mut s = Solid3D::new();
    s.set_sat_document(&primitives::build_box(
        [10000. + offset[0], 20000. + offset[1], 3000. + offset[2]],
        1000.,
        2000.,
        3000.,
    ));
    doc.add_entity(EntityType::Solid3D(s))?;
    let mut s = Solid3D::new();
    s.set_sat_document(&primitives::build_cylinder(
        [13000. + offset[0], 20000. + offset[1], offset[2]],
        500.,
        2000.,
    ));
    doc.add_entity(EntityType::Solid3D(s))?;
    DwgWriter::write_to_file(path, &doc)?;
    Ok(())
}

fn prewarm_acis_cache_parallel(
    doc: &CadDocument,
    acis_cache: &mut HashMap<u64, Option<Vec<LocalMesh>>>,
) {
    // Collect unique ACIS entities reachable from *Model_Space
    let mut blocks_to_visit = vec!["*Model_Space".to_string()];
    let mut visited_b = std::collections::HashSet::new();
    let mut acis_jobs: Vec<(u64, &AcisData, Option<&[acadrust::entities::solid3d::Wire]>)> =
        Vec::new();
    let mut seen_handles = std::collections::HashSet::new();

    while let Some(bname) = blocks_to_visit.pop() {
        if !visited_b.insert(bname.to_uppercase()) {
            continue;
        }
        for e in doc.entities_in_block(&bname) {
            if e.common().invisible {
                continue;
            }
            match e {
                EntityType::Insert(ins) => {
                    if !ins.block_name.eq_ignore_ascii_case("*Model_Space")
                        && !ins.block_name.eq_ignore_ascii_case("*Paper_Space")
                    {
                        blocks_to_visit.push(ins.block_name.clone());
                    }
                }
                EntityType::Solid3D(s) => {
                    let h = s.common.handle.value();
                    if seen_handles.insert(h) {
                        acis_jobs.push((h, &s.acis_data, Some(&s.wires)));
                    }
                }
                EntityType::Body(b) => {
                    let h = b.common.handle.value();
                    if seen_handles.insert(h) {
                        acis_jobs.push((h, &b.acis_data, None));
                    }
                }
                EntityType::Region(r) => {
                    let h = r.common.handle.value();
                    if seen_handles.insert(h) {
                        acis_jobs.push((h, &r.acis_data, None));
                    }
                }
                EntityType::Surface(s) => {
                    let h = s.common.handle.value();
                    if seen_handles.insert(h) {
                        acis_jobs.push((h, &s.acis_data, None));
                    }
                }
                EntityType::Unknown(value) => {
                    if let Some((name, _, _)) = plant_block_reference(doc, value) {
                        blocks_to_visit.push(name);
                    }
                }
                _ => {}
            }
        }
    }

    if acis_jobs.len() < 8 {
        return;
    }
    // Process smaller/standard solids first for immediate throughput and smooth progress,
    // leaving complex composite solids to be processed with adaptive coarse tolerance.
    acis_jobs.sort_by_key(|(_, data, _)| data.size());

    let n_threads = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4)
        .clamp(2, 16);
    use std::sync::atomic::{AtomicUsize, Ordering};
    let next_idx = AtomicUsize::new(0);
    let jobs_ref = &acis_jobs;

    let results: Vec<Vec<(u64, Option<Vec<LocalMesh>>)>> = std::thread::scope(|s| {
        let mut handles = Vec::new();
        for _ in 0..n_threads {
            let next_idx_ref = &next_idx;
            handles.push(s.spawn(move || {
                let mut out_chunk = Vec::new();
                loop {
                    let idx = next_idx_ref.fetch_add(1, Ordering::Relaxed);
                    if idx >= jobs_ref.len() {
                        break;
                    }
                    let (h, acis_data, solid_wires) = jobs_ref[idx];
                    if idx % 16 == 0 || idx + 1 == jobs_ref.len() || idx >= jobs_ref.len().saturating_sub(64) {
                        eprintln!("[plantview-dwg progress] {}/{} sólidos encaminhados", idx + 1, jobs_ref.len());
                    }
                    let Some(sat) = acis_data.parse() else {
                        out_chunk.push((h, None));
                        continue;
                    };
                    let (bodies, loss) = acis::lift(&sat);
                    if !loss.is_empty() {
                        eprintln!(
                            "[DWG ACIS loss] {:X} surfaces={} curves={} broken={}",
                            h,
                            loss.surfaces.len(),
                            loss.curves.len(),
                            loss.broken.len()
                        );
                    }
                    // Adaptive tolerance for massive industrial plant models:
                    let is_massive = jobs_ref.len() > 2000 || acis_data.size() > 50_000;
                    let is_large = jobs_ref.len() > 500 || acis_data.size() > 20_000;
                    let is_colossal = acis_data.size() > 100_000;
                    let tol = if is_colossal {
                        mesh::TessellationTolerance::new(0.95, 50.0)
                    } else if is_massive {
                        mesh::TessellationTolerance::new(0.80, 25.0)
                    } else if is_large {
                        mesh::TessellationTolerance::new(0.65, 10.0)
                    } else if acis_data.size() > 10_000 {
                        mesh::TessellationTolerance::new(0.55, 3.0)
                    } else {
                        mesh::TessellationTolerance::new(0.40, 1.0)
                    };
                    let mut local_meshes = Vec::new();
                    let mut all_local_pts: Vec<[f64; 3]> = Vec::new();
                    let mut skip_heavy = false;
                    for body in bodies.iter() {
                        // If a single B-Rep body has more than 600 faces in a heavy model,
                        // skip expensive B-Rep triangulation and let Priority 2 (GraphicData Proxy)
                        // or Wireframe render it instantly.
                        if is_massive && body.faces.len() > 600 {
                            skip_heavy = true;
                            break;
                        }
                        let body_tol = if is_massive && body.faces.len() > 120 {
                            mesh::TessellationTolerance::new(0.95, 50.0)
                        } else {
                            tol
                        };
                        let locals = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                            tessellate_local(body, &sat, body_tol)
                        })).unwrap_or_default();
                        for local in locals {
                            if local.missing_faces > 0 {
                                eprintln!(
                                    "[DWG face loss] {:X} missing={} triangles={}",
                                    h,
                                    local.missing_faces,
                                    local.faces.len() / 4
                                );
                            }
                            all_local_pts
                                .extend(local.local_points.iter().map(|p| [p.x, p.y, p.z]));
                            local_meshes.push(local);
                        }
                    }
                    if skip_heavy {
                        out_chunk.push((h, None));
                        continue;
                    }
                    let mut bounds_mismatch = false;
                    if let (Some(wires), Some(ab)) = (solid_wires, compute_bounds(&all_local_pts)) {
                        let mut w_pts: Vec<[f64; 3]> = Vec::new();
                        for w in wires {
                            for pt in &w.points {
                                w_pts.push([pt.x, pt.y, pt.z]);
                            }
                        }
                        if w_pts.len() >= 4 {
                            if let Some(wb) = compute_bounds(&w_pts) {
                                let w_span = ((wb[1] - wb[0]).powi(2)
                                    + (wb[3] - wb[2]).powi(2)
                                    + (wb[5] - wb[4]).powi(2))
                                .sqrt();
                                let a_span = ((ab[1] - ab[0]).powi(2)
                                    + (ab[3] - ab[2]).powi(2)
                                    + (ab[5] - ab[4]).powi(2))
                                .sqrt();
                                if w_span > 1e-4 {
                                    let w_cx = 0.5 * (wb[0] + wb[1]);
                                    let w_cy = 0.5 * (wb[2] + wb[3]);
                                    let w_cz = 0.5 * (wb[4] + wb[5]);
                                    let a_cx = 0.5 * (ab[0] + ab[1]);
                                    let a_cy = 0.5 * (ab[2] + ab[3]);
                                    let a_cz = 0.5 * (ab[4] + ab[5]);
                                    let center_dist = ((a_cx - w_cx).powi(2)
                                        + (a_cy - w_cy).powi(2)
                                        + (a_cz - w_cz).powi(2))
                                    .sqrt();
                                    if center_dist > (w_span * 10.0).max(25.0)
                                        || a_span > (w_span * 25.0).max(25.0)
                                    {
                                        bounds_mismatch = true;
                                    }
                                }
                            }
                        }
                    }
                    if !bounds_mismatch && !local_meshes.is_empty() {
                        out_chunk.push((h, Some(local_meshes)));
                    } else {
                        out_chunk.push((h, None));
                    }
                }
                out_chunk
            }));
        }
        handles.into_iter().filter_map(|h| h.join().ok()).collect()
    });

    for chunk in results {
        for (h, val) in chunk {
            acis_cache.insert(h, val);
        }
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 {
        return Err("Uso: plantview-dwg entrada.dwg geometria.json | --fixture exemplo.dwg".into());
    }
    if args[1] == "--fixture" {
        return fixture(&args[2], [0.; 3]);
    }
    if args[1] == "--fixture-georef" {
        return fixture(&args[2], [194_000_000., 8_745_000_000., -900_000.]);
    }
    let t0 = std::time::Instant::now();
    eprintln!("[plantview-dwg] Abrindo arquivo DWG: {}", args[1]);

    let mut read_mode = "normal_failsafe".to_string();
    let mut reader = DwgReader::from_file(&args[1])?;
    reader.options.failsafe = true;
    let mut doc = match reader.read() {
        Ok(doc) => {
            eprintln!("[plantview-dwg] Leitura padrão com failsafe concluída com sucesso.");
            doc
        }
        Err(e) => {
            eprintln!("[plantview-dwg] Aviso: Leitura padrão encontrou erro ({:?}), executando recover()...", e);
            read_mode = "recover".to_string();
            let mut rec_reader = DwgReader::from_file(&args[1])?;
            let outcome = rec_reader.recover()?;
            eprintln!("[plantview-dwg] Recover concluído. Estatísticas do recover: stats={:?}, repair={:?}", outcome.stats, outcome.repair);
            outcome.document
        }
    };
    if let Some(raw) = doc.raw_acds_data.take() {
        if let Some(records) = acds::active_blobs(raw.as_slice()) {
            let mut corrected = 0;
            for (handle, blob) in records {
                let Some(entity) = doc.get_entity_mut(acadrust::types::Handle::new(handle)) else {
                    continue;
                };
                let data = match entity {
                    EntityType::Solid3D(s) => &mut s.acis_data,
                    EntityType::Body(s) => &mut s.acis_data,
                    EntityType::Region(s) => &mut s.acis_data,
                    EntityType::Surface(s) => &mut s.acis_data,
                    _ => continue,
                };
                if data.sab_data != blob {
                    data.sab_data = blob;
                    data.sat_data.clear();
                    data.is_binary = true;
                    data.version = acadrust::entities::solid3d::AcisVersion::Version2;
                    corrected += 1;
                }
            }
            eprintln!(
                "[DWG active index] {} modeler bindings corrected using current datastore records",
                corrected
            );
        }
    }
    let total_entities_count = doc.entities().count();
    eprintln!(
        "[plantview-dwg] Documento DWG decodificado em {:.2}s. Total entidades: {}",
        t0.elapsed().as_secs_f32(),
        total_entities_count
    );

    let mut out = Output {
        units: doc.header.insertion_units,
        read_mode,
        total_entities: total_entities_count,
        ..Output::default()
    };
    let mut visited_blocks = Vec::new();
    let mut acis_cache = HashMap::new();

    let tw = std::time::Instant::now();
    prewarm_acis_cache_parallel(&doc, &mut acis_cache);
    for entity in doc.model_space_entities() {
        walk(
            &doc,
            entity,
            Matrix4::identity(),
            "",
            0,
            [230, 230, 230],
            "0",
            &mut visited_blocks,
            &mut acis_cache,
            &mut out,
        );
    }

    // Finalize coverage calculations
    out.coverage.total_meshes_emitted = out.meshes.len();
    out.coverage.total_triangles_emitted = out.meshes.iter().map(|m| m.faces.len() / 4).sum();
    out.coverage.partial_solids = out
        .diagnostics
        .iter()
        .filter(|d| d.missing_faces > 0)
        .count();
    out.coverage.missing_faces = out.diagnostics.iter().map(|d| d.missing_faces).sum();
    out.coverage.status = if out.coverage.total_solids_found == 0 && out.meshes.is_empty() {
        "empty".to_string()
    } else if out.coverage.failed_solids == 0 && out.coverage.partial_solids == 0 {
        "complete".to_string()
    } else if out.coverage.acis_success > 0
        || out.coverage.proxy_fallback > 0
        || out.coverage.wire_fallback > 0
    {
        "partial".to_string()
    } else {
        "empty".to_string()
    };

    eprintln!(
        "[plantview-dwg] Geometrias geradas em {:.2}s. Malhas 3D: {}, Pecas no cache: {}. Cobertura: {} (Solidos: {}, ACIS: {}, Proxy: {}, Fios: {}, Falhas: {})",
        tw.elapsed().as_secs_f32(),
        out.meshes.len(),
        acis_cache.len(),
        out.coverage.status,
        out.coverage.total_solids_found,
        out.coverage.acis_success,
        out.coverage.proxy_fallback,
        out.coverage.wire_fallback,
        out.coverage.failed_solids
    );

    for (kind, count) in &out.skipped {
        out.warnings
            .push(format!("{} entidade(s) {} ignorada(s)", count, kind));
    }

    if out.meshes.len() > 300 && std::env::var_os("PLANTVIEW_DWG_NO_BATCH").is_none() {
        let original_count = out.meshes.len();
        let mut batches: Vec<Mesh> = Vec::new();
        let mut map: HashMap<(String, [u8; 3]), usize> = HashMap::new();
        for m in std::mem::take(&mut out.meshes) {
            let layer = if let Some(pos) = m.name.find(" | ") {
                let right = &m.name[pos + 3..];
                right.split('#').next().unwrap_or(right).trim().to_string()
            } else {
                "0".to_string()
            };
            let key = (layer.clone(), m.color);
            let target_idx = match map.get(&key) {
                Some(&idx) if batches[idx].points.len() + m.points.len() < 500_000 => idx,
                _ => {
                    let idx = batches.len();
                    batches.push(Mesh {
                        name: format!("BATCH | {}", layer),
                        color: m.color,
                        points: Vec::new(),
                        faces: Vec::new(),
                        ..Default::default()
                    });
                    map.insert(key, idx);
                    idx
                }
            };
            let entry = &mut batches[target_idx];
            let pt_offset = entry.points.len();
            entry.points.extend(m.points);
            let mut i = 0;
            while i < m.faces.len() {
                let n = m.faces[i];
                entry.faces.push(n);
                for k in 0..n {
                    entry.faces.push(m.faces[i + 1 + k] + pt_offset);
                }
                i += 1 + n;
            }
        }
        out.meshes = batches;
        eprintln!(
            "[plantview-dwg] Otimização: {} malhas agrupadas em {} lotes GPU por camada/cor.",
            original_count,
            out.meshes.len()
        );
    }

    for m in &mut out.meshes {
        if m.points.len() > 50 {
            let (origin, p_bytes) = encode_local_points(&m.points);
            m.points_b64 = Some(base64_encode(&p_bytes));
            m.points_origin = Some(origin);
            m.points.clear();

            let mut f_bytes = Vec::with_capacity(m.faces.len() * 4);
            for &f in &m.faces {
                f_bytes.extend_from_slice(&(f as i32).to_le_bytes());
            }
            m.faces_b64 = Some(base64_encode(&f_bytes));
            m.faces.clear();
        } else {
            for p in &mut m.points {
                p[0] = (p[0] * 100.0).round() * 0.01;
                p[1] = (p[1] * 100.0).round() * 0.01;
                p[2] = (p[2] * 100.0).round() * 0.01;
            }
        }
    }
    serde_json::to_writer(
        BufWriter::with_capacity(4 * 1024 * 1024, File::create(&args[2])?),
        &out,
    )?;
    eprintln!(
        "[plantview-dwg] Concluido com sucesso em {:.2}s",
        t0.elapsed().as_secs_f32()
    );
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{}", error);
        std::process::exit(1);
    }
}
#[cfg(test)]
mod fidelity_tests {
    use super::*;
    #[test]
    fn concave_plant_steel_profile_is_expanded_before_cap_triangulation() {
        let profile = [
            [-3., -4.],
            [3., -4.],
            [3., -2.],
            [1., -2.],
            [1., 2.],
            [3., 2.],
            [3., 4.],
            [-3., 4.],
            [-3., 2.],
            [-1., 2.],
            [-1., -2.],
            [-3., -2.],
        ];
        let mut payload = 24u32.to_le_bytes().to_vec();
        for _ in 0..2 {
            for p in profile {
                payload.extend(doubles(&[p[0], p[1], 500.]));
            }
        }
        let mut entries = vec![12i32];
        entries.extend(0..12);
        entries.push(12);
        entries.extend((12..24).rev());
        for i in 0..12 {
            let j = (i + 1) % 12;
            entries.extend([4, i, j, j + 12, i + 12]);
        }
        payload.extend((entries.len() as u32).to_le_bytes());
        for e in entries {
            payload.extend(e.to_le_bytes());
        }
        let graphics = stream(vec![
            (6, {
                let mut p = 2u32.to_le_bytes().to_vec();
                p.extend(doubles(&[0., 0., 0., 0., 0., 1000.]));
                p
            }),
            (9, payload),
        ]);
        let mut out = Output::default();
        assert!(process_proxy_graphics(
            &graphics,
            Matrix4::identity(),
            [255; 3],
            "beam",
            &mut out
        ));
        let mesh = &out.meshes[0];
        let bounds = compute_bounds(&mesh.points).unwrap();
        assert_eq!(bounds, [-3., -4., 0., 3., 4., 1000.]);
        for t in mesh.faces.chunks_exact(4) {
            let v = [t[1], t[2], t[3]].map(|i| {
                let p = mesh.points[i];
                Vector3::new(p[0], p[1], p[2])
            });
            assert!(
                (v[1] - v[0]).cross(&(v[2] - v[0])).length_squared() > 1e-12,
                "collapsed steel face"
            );
        }
    }
    #[test]
    fn face_true_color_overrides_aci_without_leaking_to_other_faces() {
        use acadrust::entities::acis::{SatDocument, SatPointer, SatRecord, SatToken};
        let mut sat = SatDocument::new();
        for (index, kind, owner, value) in [
            (0, "truecolor-adesk-attrib", 12, 0xC2123456u32),
            (1, "color-adesk-attrib", 12, 5),
            (2, "color-adesk-attrib", 18, 256),
        ] {
            let mut r = SatRecord::new(index, kind);
            r.tokens = vec![
                SatToken::Pointer(SatPointer::NULL),
                SatToken::Pointer(SatPointer::NULL),
                SatToken::Pointer(SatPointer(owner)),
                SatToken::Sab {
                    tag: 4,
                    data: value.to_le_bytes().to_vec(),
                },
            ];
            sat.records.push(r);
        }
        let colors = acis_colors(&sat);
        assert_eq!(colors.get(&12), Some(&Color::from_rgb(0x12, 0x34, 0x56)));
        assert_eq!(colors.get(&18), Some(&Color::ByLayer));
        assert!(!colors.contains_key(&13));
    }
    #[test]
    fn partial_solid_is_reported_after_success_sample_limit() {
        let mut out = Output::default();
        for _ in 0..50 {
            push_diagnostic(
                &mut out,
                EntityDiagnostic {
                    status: "SUCCESS_ACIS_CACHE".into(),
                    ..Default::default()
                },
            );
        }
        push_diagnostic(
            &mut out,
            EntityDiagnostic {
                status: "SUCCESS_ACIS_CACHE".into(),
                missing_faces: 2,
                ..Default::default()
            },
        );
        assert_eq!(out.diagnostics.len(), 51);
        assert_eq!(out.diagnostics.last().unwrap().missing_faces, 2);
    }
    fn stream(records: Vec<(u32, Vec<u8>)>) -> Vec<u8> {
        let mut bytes = vec![0u8; 8];
        let count = records.len() as u32;
        for (kind, payload) in records {
            bytes.extend(((payload.len() + 8) as u32).to_le_bytes());
            bytes.extend(kind.to_le_bytes());
            bytes.extend(payload);
        }
        let length = bytes.len() as u32;
        bytes[..4].copy_from_slice(&length.to_le_bytes());
        bytes[4..8].copy_from_slice(&count.to_le_bytes());
        bytes
    }
    fn doubles(values: &[f64]) -> Vec<u8> {
        values.iter().flat_map(|n| n.to_le_bytes()).collect()
    }
    fn circle(x: f64, z: f64, r: f64) -> (u32, Vec<u8>) {
        (2, doubles(&[x, 0., z, r, 0., 0., 1.]))
    }
    #[test]
    fn bolt_circle_is_not_connected_to_flange_axis() {
        let mut output = Output::default();
        let graphics = stream(vec![
            circle(0., 0., 100.),
            circle(0., 10., 100.),
            circle(40., 0., 5.),
            circle(40., 10., 5.),
        ]);
        assert!(process_proxy_graphics(
            &graphics,
            Matrix4::identity(),
            [255, 255, 255],
            "test",
            &mut output
        ));
        let mesh = &output.meshes[0];
        for face in mesh.faces.chunks_exact(4) {
            let triangle = [
                mesh.points[face[1]],
                mesh.points[face[2]],
                mesh.points[face[3]],
            ];
            let radii = triangle.map(|p| ((p[0] - 40.).powi(2) + p[1].powi(2)).sqrt());
            let belongs_to_bolt = radii.iter().all(|r| (*r - 5.).abs() < 1e-6);
            let belongs_to_flange = triangle
                .iter()
                .all(|p| (p[0].hypot(p[1]) - 100.).abs() < 1e-6);
            assert!(
                belongs_to_bolt || belongs_to_flange,
                "triangle incorrectly joins separate axes"
            );
        }
    }
    #[test]
    fn polygon_hole_is_not_filled() {
        let mut p = vec![
            [0., 0., 0.],
            [4., 0., 0.],
            [4., 4., 0.],
            [0., 4., 0.],
            [1., 1., 0.],
            [1., 3., 0.],
            [3., 3., 0.],
            [3., 1., 0.],
        ];
        let mut faces = Vec::new();
        append_proxy_polygon(&mut p, &[vec![0, 1, 2, 3], vec![4, 5, 6, 7]], &mut faces);
        let area: f64 = faces
            .chunks_exact(4)
            .map(|t| {
                let a = p[t[1]];
                let b = p[t[2]];
                let c = p[t[3]];
                ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])).abs() / 2.
            })
            .sum();
        assert!((area - 12.).abs() < 1e-8, "annulus area was {}", area);
    }
    #[test]
    fn explicit_polygon_keeps_saved_true_color() {
        let mut payload = 3u32.to_le_bytes().to_vec();
        payload.extend(doubles(&[0., 0., 0., 1., 0., 0., 0., 1., 0.]));
        let graphics = stream(vec![
            (22, 0xC2123456u32.to_le_bytes().to_vec()),
            (7, payload),
        ]);
        let mut output = Output::default();
        assert!(process_proxy_graphics(
            &graphics,
            Matrix4::identity(),
            [255; 3],
            "test",
            &mut output
        ));
        assert_eq!(output.meshes[0].color, [0x12, 0x34, 0x56]);
    }
}
