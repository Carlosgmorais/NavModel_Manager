//! Resolve current modeler records through the datastore's active index.
//! Physical segment order also contains retired revisions and is not save order.
use std::collections::HashMap;

fn u32_at(b: &[u8], p: usize) -> Option<u32> {
    Some(u32::from_le_bytes(
        b.get(p..p.checked_add(4)?)?.try_into().ok()?,
    ))
}
fn u64_at(b: &[u8], p: usize) -> Option<u64> {
    Some(u64::from_le_bytes(
        b.get(p..p.checked_add(8)?)?.try_into().ok()?,
    ))
}
fn segment<'a>(
    b: &'a [u8],
    table: usize,
    count: usize,
    id: usize,
    name: &[u8; 6],
) -> Option<(usize, &'a [u8])> {
    if id >= count {
        return None;
    }
    let offset = usize::try_from(u64_at(b, table.checked_add(id.checked_mul(12)?)?)?).ok()?;
    if b.get(offset..offset.checked_add(8)?)? != [b"\xac\xd5".as_slice(), name].concat() {
        return None;
    }
    if u32_at(b, offset + 8)? as usize != id {
        return None;
    }
    // blob01's segment-size field can describe only its fixed 48-byte header.
    // Its actual page payload length is the separate uint64 at header +72.
    let size = if name == b"blob01" {
        80usize.checked_add(usize::try_from(u64_at(b, offset + 72)?).ok()?)?
    } else {
        usize::try_from(u64_at(b, offset + 16)?).ok()?
    };
    if size < 48 {
        return None;
    }
    Some((offset, b.get(offset..offset.checked_add(size)?)?))
}

/// None means the source has no supported active index; preserve its normal
/// reader path. A valid index yields only its live records, never old segments.
pub fn active_blobs(b: &[u8]) -> Option<Vec<(u64, Vec<u8>)>> {
    if b.get(..4)? != b"jard" {
        return None;
    }
    let root = usize::try_from(u64_at(b, 24)?).ok()?;
    if b.get(root..root.checked_add(8)?)? != b"\xac\xd5segidx" {
        return None;
    }
    let count = u32_at(b, 32)? as usize;
    let table = root.checked_add(48)?;
    b.get(table..table.checked_add(count.checked_mul(12)?)?)?;
    let (_, index) = segment(b, table, count, u32_at(b, 40)? as usize, b"datidx")?;
    let entries = u32_at(index, 48)? as usize;
    index.get(56..56usize.checked_add(entries.checked_mul(12)?)?)?;
    let mut blobs = HashMap::new();
    for i in 0..entries {
        let p = 56 + i * 12;
        let id = u32_at(index, p)? as usize;
        if id == 0 {
            continue;
        }
        let Some((_, data)) = segment(b, table, count, id, b"_data_") else {
            continue;
        };
        // datidx offsets address the record-header array following the fixed
        // 48-byte segment header (including for files written by AutoCAD).
        let record = 48usize.checked_add(u32_at(index, p + 4)? as usize)?;
        if u32_at(data, record) != Some(20) {
            continue;
        }
        let handle = u64_at(data, record + 8)?;
        let base = (u32_at(data, 36)? as usize).checked_mul(16)?;
        if base < record + 20 || base > data.len() {
            continue;
        }
        let start = base.checked_add(u32_at(data, record + 16)? as usize)?;
        let Some(size) = u32_at(data, start) else {
            continue;
        };
        let payload = if size == 0xBB10_6BB1 {
            let Some(total) = u64_at(data, start + 4).and_then(|v| usize::try_from(v).ok()) else {
                continue;
            };
            let Some(pages) = u32_at(data, start + 12).map(|v| v as usize) else {
                continue;
            };
            if pages == 0 || pages > count || total > b.len() {
                continue;
            }
            let Some(descriptor) = data.get(start..start.saturating_add(36 + pages * 8)) else {
                continue;
            };
            let mut result = Vec::with_capacity(total);
            let mut valid = true;
            for page in 0..pages {
                let page_id = u32_at(descriptor, 36 + page * 8)? as usize;
                let Some((_, chunk)) = segment(b, table, count, page_id, b"blob01") else {
                    valid = false;
                    break;
                };
                let size = if page + 1 == pages {
                    u32_at(descriptor, 24)?
                } else {
                    u32_at(descriptor, 20)?
                } as usize;
                let Some(bytes) = chunk.get(80..80usize.saturating_add(size)) else {
                    valid = false;
                    break;
                };
                result.extend_from_slice(bytes);
            }
            if !valid || result.len() != total {
                continue;
            }
            result
        } else {
            let Some(bytes) = data.get(
                start.saturating_add(4)..start.saturating_add(4).saturating_add(size as usize),
            ) else {
                continue;
            };
            bytes.to_vec()
        };
        if !(payload.starts_with(b"ASM BinaryFile") || payload.starts_with(b"ACIS BinaryFile")) {
            continue;
        }
        // Ambiguous live bindings must not overwrite one another silently.
        if let Some(previous) = blobs.get(&handle) {
            if previous != &payload {
                return None;
            }
        }
        blobs.insert(handle, payload);
    }
    let mut result: Vec<_> = blobs.into_iter().collect();
    result.sort_by_key(|v| v.0);
    Some(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn put32(b: &mut [u8], p: usize, v: u32) {
        b[p..p + 4].copy_from_slice(&v.to_le_bytes());
    }
    fn put64(b: &mut [u8], p: usize, v: u64) {
        b[p..p + 8].copy_from_slice(&v.to_le_bytes());
    }
    fn header(b: &mut [u8], p: usize, id: u32, name: &[u8; 6], size: u64) {
        b[p..p + 2].copy_from_slice(b"\xac\xd5");
        b[p + 2..p + 8].copy_from_slice(name);
        put32(b, p + 8, id);
        put64(b, p + 16, size);
    }
    fn fixture() -> Vec<u8> {
        let mut b = vec![0; 1536];
        b[..4].copy_from_slice(b"jard");
        put64(&mut b, 24, 128);
        put32(&mut b, 32, 4);
        put32(&mut b, 40, 1);
        header(&mut b, 128, 0, b"segidx", 128);
        for (id, offset) in [(0, 128), (1, 256), (2, 512), (3, 1024)] {
            put64(&mut b, 176 + id * 12, offset as u64);
            put32(&mut b, 184 + id * 12, 256);
        }
        header(&mut b, 256, 1, b"datidx", 256);
        put32(&mut b, 304, 1);
        put32(&mut b, 312, 2);
        put32(&mut b, 320, 5);
        for (offset, id, text) in [
            (512, 2, b"ASM BinaryFile CURRENT".as_slice()),
            (1024, 3, b"ASM BinaryFile RETIRED".as_slice()),
        ] {
            header(&mut b, offset, id, b"_data_", 256);
            put32(&mut b, offset + 36, 5);
            put32(&mut b, offset + 48, 20);
            put32(&mut b, offset + 52, 1);
            put64(&mut b, offset + 56, 0x1_2345_6789);
            put32(&mut b, offset + 80, text.len() as u32);
            b[offset + 84..offset + 84 + text.len()].copy_from_slice(text);
        }
        b
    }
    #[test]
    fn physical_last_revision_is_not_the_active_revision() {
        let records = active_blobs(&fixture()).unwrap();
        assert_eq!(
            records,
            vec![(0x1_2345_6789, b"ASM BinaryFile CURRENT".to_vec())]
        );
    }
    #[test]
    fn truncated_directory_falls_back_without_guessing() {
        let mut b = fixture();
        put32(&mut b, 32, u32::MAX);
        assert!(active_blobs(&b).is_none());
    }
    #[test]
    fn stub_entries_do_not_resurrect_retired_solids() {
        let mut b = fixture();
        put32(&mut b, 312, 0);
        assert!(active_blobs(&b).unwrap().is_empty());
    }
    #[test]
    fn active_paged_body_joins_only_its_referenced_payloads() {
        let mut b = fixture();
        b.resize(2048, 0);
        put32(&mut b, 32, 6);
        let payload = b"ASM BinaryFile MULTIPAGE";
        let split = 8;
        for (id, offset, part) in [(4, 1536, &payload[..split]), (5, 1792, &payload[split..])] {
            put64(&mut b, 176 + id * 12, offset as u64);
            put32(&mut b, 184 + id * 12, 256);
            header(&mut b, offset, id as u32, b"blob01", 48);
            put64(&mut b, offset + 72, part.len() as u64);
            b[offset + 80..offset + 80 + part.len()].copy_from_slice(part);
        }
        let p = 512 + 80;
        put32(&mut b, p, 0xBB10_6BB1);
        put64(&mut b, p + 4, payload.len() as u64);
        put32(&mut b, p + 12, 2);
        put32(&mut b, p + 16, 52);
        put32(&mut b, p + 20, split as u32);
        put32(&mut b, p + 24, (payload.len() - split) as u32);
        put32(&mut b, p + 36, 4);
        put32(&mut b, p + 40, split as u32);
        put32(&mut b, p + 44, 5);
        put32(&mut b, p + 48, (payload.len() - split) as u32);
        assert_eq!(
            active_blobs(&b).unwrap(),
            vec![(0x1_2345_6789, payload.to_vec())]
        );
    }
}
