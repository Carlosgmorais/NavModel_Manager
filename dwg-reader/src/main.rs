//! Independent read-only DWG tessellator with Plant 3D Object Enabler and ACIS B-Rep caching.
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

#[derive(Serialize, Clone)]
struct Mesh {
    name: String,
    color: [u8; 3],
    points: Vec<[f64; 3]>,
    faces: Vec<usize>,
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
}

fn rgb(doc: &CadDocument, color: Color, layer: &str, parent: [u8; 3]) -> [u8; 3] {
    let resolved = match color {
        Color::ByBlock => return parent,
        Color::ByLayer => doc.layers.get(layer).map(|l| l.color).unwrap_or(Color::Index(7)),
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

/// Decode Autodesk Proxy Graphics SHELL (opcode 9) into raw local points and faces.
fn parse_proxy_shell_local(payload: &[u8]) -> Option<(Vec<[f64; 3]>, Vec<usize>)> {
    if payload.len() < 12 {
        return None;
    }
    let mut cur = 0;
    let read_u32 = |cur: &mut usize| -> Option<u32> {
        if *cur + 4 > payload.len() { return None; }
        let val = u32::from_le_bytes(payload[*cur..*cur + 4].try_into().ok()?);
        *cur += 4;
        Some(val)
    };
    let read_i32 = |cur: &mut usize| -> Option<i32> {
        if *cur + 4 > payload.len() { return None; }
        let val = i32::from_le_bytes(payload[*cur..*cur + 4].try_into().ok()?);
        *cur += 4;
        Some(val)
    };
    let read_f64 = |cur: &mut usize| -> Option<f64> {
        if *cur + 8 > payload.len() { return None; }
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

    let face_entry_count = read_u32(&mut cur)? as usize;
    if face_entry_count == 0 || face_entry_count > 3_000_000 {
        return None;
    }

    let mut faces = Vec::new();
    let mut read_entries = 0;
    while read_entries < face_entry_count && cur + 4 <= payload.len() {
        let edge_count_raw = match read_i32(&mut cur) {
            Some(v) => v,
            None => break,
        };
        let edge_count = edge_count_raw.abs() as usize;
        read_entries += 1 + edge_count;
        if edge_count < 3 || edge_count > 64 {
            if cur + edge_count * 4 <= payload.len() {
                cur += edge_count * 4;
            } else {
                break;
            }
            continue;
        }
        let mut idxs = Vec::with_capacity(edge_count);
        for _ in 0..edge_count {
            if let Some(idx) = read_u32(&mut cur) {
                if (idx as usize) < points.len() {
                    idxs.push(idx as usize);
                }
            }
        }
        if idxs.len() == 3 {
            faces.extend([3, idxs[0], idxs[1], idxs[2]]);
        } else if idxs.len() == 4 {
            faces.extend([3, idxs[0], idxs[1], idxs[2], 3, idxs[0], idxs[2], idxs[3]]);
        } else if idxs.len() > 4 {
            for k in 1..idxs.len() - 1 {
                faces.extend([3, idxs[0], idxs[k], idxs[k + 1]]);
            }
        }
    }

    if !faces.is_empty() && !points.is_empty() {
        Some((points, faces))
    } else {
        None
    }
}

/// Decode Autodesk Proxy Graphics MESH (opcode 8) - MxN grid surfaces.
fn decode_proxy_mesh(payload: &[u8], matrix: Matrix4, color: [u8; 3], name: &str, out: &mut Output) -> bool {
    if payload.len() < 8 {
        return false;
    }
    let mut cur = 0;
    let read_u32 = |cur: &mut usize| -> Option<u32> {
        if *cur + 4 > payload.len() { return None; }
        let val = u32::from_le_bytes(payload[*cur..*cur + 4].try_into().ok()?);
        *cur += 4;
        Some(val)
    };
    let read_f64 = |cur: &mut usize| -> Option<f64> {
        if *cur + 8 > payload.len() { return None; }
        let val = f64::from_le_bytes(payload[*cur..*cur + 8].try_into().ok()?);
        *cur += 8;
        Some(val)
    };

    let num_rows = match read_u32(&mut cur) { Some(v) => v as usize, None => return false };
    let num_cols = match read_u32(&mut cur) { Some(v) => v as usize, None => return false };
    if num_rows < 2 || num_cols < 2 || num_rows * num_cols > 1_000_000 {
        return false;
    }
    let total_pts = num_rows * num_cols;
    if cur + total_pts * 24 > payload.len() {
        return false;
    }
    let mut points = Vec::with_capacity(total_pts);
    for _ in 0..total_pts {
        let x = match read_f64(&mut cur) { Some(v) => v, None => return false };
        let y = match read_f64(&mut cur) { Some(v) => v, None => return false };
        let z = match read_f64(&mut cur) { Some(v) => v, None => return false };
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
            (prev_u * cos_a + k.cross(&prev_u) * sin_a + k * (k.dot(&prev_u) * (1.0 - cos_a))).normalize()
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
                if dot > 0.975 {
                    let delta = c.center - ref_c.center;
                    let along = ref_c.normal * delta.dot(&ref_c.normal);
                    let perp_dist = (delta - along).length();
                    let max_r = ref_c.radius.max(c.radius);
                    if perp_dist <= max_r * 0.45 + 2.0 {
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
                    if (c.center - prev.center).length() < 0.25 && (c.radius - prev.radius).abs() < 0.25 {
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
                    let mut prev_ring = append_circle_ring(&mut pts, base_c.center, u, v, base_c.radius, segs, matrix);
                    for s_i in 1..=dome_steps {
                        let theta = (s_i as f64) * std::f64::consts::FRAC_PI_2 / (dome_steps as f64);
                        let r = (base_c.radius * theta.cos()).max(0.5);
                        let h = dome_h * theta.sin();
                        let ring = append_circle_ring(&mut pts, base_c.center + dome_dir * h, u, v, r, segs, matrix);
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
                        if len.abs() > 1.0 {
                            let c1 = ProxyCircle3D {
                                center: c0.center + axis * len,
                                radius: c0.radius,
                                normal: c0.normal,
                            };
                            emit_frustum_between_circles(c0, c1, segs, matrix, &mut pts, &mut faces);
                            break;
                        }
                    }
                }
            }
        } else if groups.len() >= 2 {
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
                    emit_frustum_between_circles(b_inner, junc_c, segs, matrix, &mut pts, &mut faces);
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
                emit_swept_elbow(best_pair.0, best_pair.1, mid_circle, segs, 10, matrix, &mut pts, &mut faces);
            }
        }
    }

    if !faces.is_empty() && !pts.is_empty() {
        out.meshes.push(Mesh {
            name: format!("{} [Plant3D-Fitting]", name),
            color,
            points: pts,
            faces,
        });
        return true;
    }
    false
}

/// Parse Autodesk Proxy Graphics binary stream for 3D Shells, Meshes, Plant 3D Piping Fittings, and Structural Steel Members.
fn process_proxy_graphics(data: &[u8], matrix: Matrix4, color: [u8; 3], name: &str, out: &mut Output) -> bool {
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

        match record_type {
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
                    matrix_stack.push(cur_matrix);
                    cur_matrix = cur_matrix * Matrix4 { m };
                }
            }
            31 => {
                // PopModelTransform: flush any fitting primitives accumulated in this local transform scope
                if !scope_circles.is_empty() {
                    reconstruct_fitting_from_primitives(
                        &scope_circles,
                        &scope_arcs,
                        &scope_polylines,
                        cur_matrix,
                        color,
                        name,
                        out,
                    );
                    scope_circles.clear();
                    scope_arcs.clear();
                    scope_polylines.clear();
                }
                if let Some(prev) = matrix_stack.pop() {
                    cur_matrix = prev;
                }
            }
            2 => {
                // Circle (center, radius, normal) = 7 f64s = 56 bytes
                if payload.len() >= 56 {
                    let f = |i: usize| f64::from_le_bytes(payload[i * 8..(i + 1) * 8].try_into().unwrap());
                    let center = Vector3::new(f(0), f(1), f(2));
                    let radius = f(3);
                    let normal = Vector3::new(f(4), f(5), f(6));
                    if radius.is_finite() && radius >= 0.25 && radius <= 50_000.0 && normal.length_squared() > 1e-8 {
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
                    let f = |i: usize| f64::from_le_bytes(payload[i * 8..(i + 1) * 8].try_into().unwrap());
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
                    let f = |i: usize| f64::from_le_bytes(payload[i * 8..(i + 1) * 8].try_into().unwrap());
                    let p1 = Vector3::new(f(0), f(1), f(2));
                    let p2 = Vector3::new(f(3), f(4), f(5));
                    let p3 = Vector3::new(f(6), f(7), f(8));
                    scope_arcs.push([p1, p2, p3]);
                }
            }
            6 => {
                // Polyline = u32 count + count * 24 bytes
                if payload.len() >= 4 {
                    let cnt = u32::from_le_bytes(payload[0..4].try_into().unwrap()) as usize;
                    if cnt >= 2 && cnt <= 100_000 && 4 + cnt * 24 <= payload.len() {
                        let mut pts = Vec::with_capacity(cnt);
                        for i in 0..cnt {
                            let base = 4 + i * 24;
                            let x = f64::from_le_bytes(payload[base..base + 8].try_into().unwrap());
                            let y = f64::from_le_bytes(payload[base + 8..base + 16].try_into().unwrap());
                            let z = f64::from_le_bytes(payload[base + 16..base + 24].try_into().unwrap());
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
            9 => {
                // Opcode 9 = SHELL
                if let Some((mut local_pts, faces)) = parse_proxy_shell_local(payload) {
                    // Check for Plant 3D AcPpDb3dStructureMember collapsed cross-section shell:
                    // When an AcPpDb3dStructureMember is saved without Object Enabler body expansion,
                    // its SHELL has 2*M vertices where the first M and second M vertices are identical
                    // (a 0-thickness mid-beam cross-section slice) preceded by a 2-point Opcode 6 beam axis!
                    if let Some([p_start, p_end]) = last_poly_segment {
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
                    let points = local_pts
                        .into_iter()
                        .map(|p| {
                            let tp = cur_matrix.transform_point(Vector3::new(p[0], p[1], p[2]));
                            [tp.x, tp.y, tp.z]
                        })
                        .collect();
                    out.meshes.push(Mesh {
                        name: format!("{} [Plant3D]", name),
                        color,
                        points,
                        faces,
                    });
                }
            }
            8 => {
                // Opcode 8 = MESH
                decode_proxy_mesh(payload, cur_matrix, color, name, out);
            }
            _ => {}
        }
        offset += record_size;
    }

    // Flush any remaining fitting primitives outside a PopModelTransform
    if out.meshes.len() == initial_count && !scope_circles.is_empty() {
        reconstruct_fitting_from_primitives(
            &scope_circles,
            &scope_arcs,
            &scope_polylines,
            cur_matrix,
            color,
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
                emit_frustum_between_circles(c0, c1, segs, Matrix4::identity(), &mut pts, &mut faces);
            }
        }
        if !faces.is_empty() {
            out.meshes.push(Mesh {
                name: format!("{} [Plant3D-Structure]", name),
                color,
                points: pts,
                faces,
            });
        }
    }

    out.meshes.len() > initial_count
}

/// Process conventional solid entities (Solid3D, Body, Region, Surface)
/// following strict priority: Native ACIS -> Proxy GraphicData Fallback -> Wire Fallback.
fn process_solid_entity(
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
                all_points.extend(points.clone());
                out.meshes.push(Mesh {
                    name: format!("{} #{}", name, index + 1),
                    color,
                    points,
                    faces: local.faces.clone(),
                });
            }
            diag.parse_result = "Cached".to_string();
            diag.status = "SUCCESS_ACIS_CACHE".to_string();
            diag.vertices = total_v;
            diag.triangles = total_t;
            diag.bounds = compute_bounds(&all_points);
            out.coverage.acis_success += 1;
            out.diagnostics.push(diag);
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
                mesh::TessellationTolerance::new(0.55, 0.5)
            } else {
                mesh::TessellationTolerance::new(0.35, 0.5)
            };

            for body in bodies.iter() {
                let tess = mesh::tessellate_fast(body, tol);
                total_missing += tess.missing_faces.len();
                let local_points = tess
                    .mesh
                    .positions
                    .iter()
                    .map(|p| Vector3::new(p[0], p[1], p[2]))
                    .collect::<Vec<_>>();
                let faces = tess
                    .mesh
                    .triangles
                    .iter()
                    .flat_map(|t| [3, t[0] as usize, t[1] as usize, t[2] as usize])
                    .collect::<Vec<_>>();

                if !faces.is_empty() {
                    total_v += local_points.len();
                    total_t += faces.len() / 4;
                    for lp in &local_points {
                        all_local_pts.push([lp.x, lp.y, lp.z]);
                    }
                    local_meshes.push(LocalMesh {
                        local_points,
                        faces,
                    });
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
                        color,
                        points,
                        faces: local.faces.clone(),
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
                out.diagnostics.push(diag);
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
                out.diagnostics.push(diag);
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
                });
                out.coverage.wire_fallback += 1;
                out.diagnostics.push(diag);
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
    out.diagnostics.push(diag);
}

fn walk(
    doc: &CadDocument,
    entity: &EntityType,
    matrix: Matrix4,
    prefix: &str,
    depth: usize,
    parent_color: [u8; 3],
    visited_blocks: &mut Vec<String>,
    acis_cache: &mut HashMap<u64, Option<Vec<LocalMesh>>>,
    out: &mut Output,
) {
    if depth > 32 {
        out.warnings.push("Bloco recursivo ou profundidade superior a 32".into());
        return;
    }
    let common = entity.common();
    if common.invisible {
        return;
    }
    let color = rgb(doc, common.color, &common.layer, parent_color);
    let name = format!("{}{:X} | {}", prefix, common.handle.value(), common.layer);

    // 1. Block Inserts (References)
    if let EntityType::Insert(insert) = entity {
        if insert.block_name.eq_ignore_ascii_case("*Model_Space")
            || insert.block_name.eq_ignore_ascii_case("*Paper_Space")
        {
            return;
        }
        if visited_blocks.iter().any(|b| b.eq_ignore_ascii_case(&insert.block_name)) {
            return; // Cycle detection
        }
        let Some(block) = doc.block_records.get(&insert.block_name) else {
            out.warnings.push(format!("Bloco ausente: {}", insert.block_name));
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
            out.warnings.push(format!("MINSERT nao suportado: {}", name));
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
                common.handle.value(),
                &common.layer,
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
                common.handle.value(),
                &common.layer,
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
                common.handle.value(),
                &common.layer,
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
                common.handle.value(),
                &common.layer,
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
                    faces.extend([3, f.vertices[0], f.vertices[1], f.vertices[2], 3, f.vertices[0], f.vertices[2], f.vertices[3]]);
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
        let points = vec![[p1.x, p1.y, p1.z], [p2.x, p2.y, p2.z], [p3.x, p3.y, p3.z], [p4.x, p4.y, p4.z]];
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
        });
        return;
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

    *out.skipped.entry(entity.as_entity().entity_type().to_string()).or_default() += 1;
}

fn fixture(path: &str) -> Result<(), Box<dyn std::error::Error>> {
    use acadrust::entities::acis::primitives;
    let mut doc = CadDocument::new();
    doc.header.insertion_units = 4;
    let mut s = Solid3D::new();
    s.set_sat_document(&primitives::build_box([10000., 20000., 3000.], 1000., 2000., 3000.));
    doc.add_entity(EntityType::Solid3D(s))?;
    let mut s = Solid3D::new();
    s.set_sat_document(&primitives::build_cylinder([13000., 20000., 0.], 500., 2000.));
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
    let mut acis_jobs: Vec<(u64, &AcisData, Option<&[acadrust::entities::solid3d::Wire]>)> = Vec::new();
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
                _ => {}
            }
        }
    }

    if acis_jobs.len() < 8 {
        return;
    }

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
                    let Some(sat) = acis_data.parse() else {
                        out_chunk.push((h, None));
                        continue;
                    };
                    let (bodies, _) = acis::lift(&sat);
                    let tol = if acis_data.size() > 40_000 {
                        mesh::TessellationTolerance::new(0.55, 0.5)
                    } else {
                        mesh::TessellationTolerance::new(0.35, 0.5)
                    };
                    let mut local_meshes = Vec::new();
                    let mut all_local_pts: Vec<[f64; 3]> = Vec::new();
                    for body in bodies.iter() {
                        let tess = mesh::tessellate_fast(body, tol);
                        let local_points = tess
                            .mesh
                            .positions
                            .iter()
                            .map(|p| Vector3::new(p[0], p[1], p[2]))
                            .collect::<Vec<_>>();
                        let faces = tess
                            .mesh
                            .triangles
                            .iter()
                            .flat_map(|t| [3, t[0] as usize, t[1] as usize, t[2] as usize])
                            .collect::<Vec<_>>();
                        if !faces.is_empty() {
                            for lp in &local_points {
                                all_local_pts.push([lp.x, lp.y, lp.z]);
                            }
                            local_meshes.push(LocalMesh {
                                local_points,
                                faces,
                            });
                        }
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
        return fixture(&args[2]);
    }
    let t0 = std::time::Instant::now();
    eprintln!("[plantview-dwg] Abrindo arquivo DWG: {}", args[1]);

    let mut read_mode = "normal_failsafe".to_string();
    let mut reader = DwgReader::from_file(&args[1])?;
    reader.options.failsafe = true;
    let doc = match reader.read() {
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
            &mut visited_blocks,
            &mut acis_cache,
            &mut out,
        );
    }

    // Finalize coverage calculations
    out.coverage.total_meshes_emitted = out.meshes.len();
    out.coverage.total_triangles_emitted = out.meshes.iter().map(|m| m.faces.len() / 4).sum();
    out.coverage.status = if out.coverage.total_solids_found == 0 && out.meshes.is_empty() {
        "empty".to_string()
    } else if out.coverage.failed_solids == 0 {
        "complete".to_string()
    } else if out.coverage.acis_success > 0 || out.coverage.proxy_fallback > 0 || out.coverage.wire_fallback > 0 {
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
        out.warnings.push(format!("{} entidade(s) {} ignorada(s)", count, kind));
    }
    for m in &mut out.meshes {
        for p in &mut m.points {
            p[0] = (p[0] * 100.0).round() * 0.01;
            p[1] = (p[1] * 100.0).round() * 0.01;
            p[2] = (p[2] * 100.0).round() * 0.01;
        }
    }
    serde_json::to_writer(BufWriter::with_capacity(4 * 1024 * 1024, File::create(&args[2])?), &out)?;
    eprintln!("[plantview-dwg] Concluido com sucesso em {:.2}s", t0.elapsed().as_secs_f32());
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{}", error);
        std::process::exit(1);
    }
}
