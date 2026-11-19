//! hoard-mft: read the NTFS master file table (MFT) of a volume directly and write Hoard's scan tree.
//!
//! Usage: hoard-mft <drive letter> <out.json> <progress.txt> <extmap.tsv>
//!
//! Reading the raw volume needs administrator rights. Instead of walking every folder (one system
//! call per file), this reads the MFT sequentially, so a whole drive takes seconds.
//! Output is the same nested-array format Hoard's Python scanner saves (see core._enc):
//!   [name, size, nfiles, cats[9], osize, ocount, [[file, size, cat, mtime]...], [child dirs...],
//!    newest_mtime, size*mtime sum, newest direct-file mtime, online-only bytes]

#[cfg(windows)]
mod mft {
use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Read, Seek, SeekFrom, Write};
use std::os::windows::fs::OpenOptionsExt;
use std::time::Instant;

const KEEP_FILE: u64 = 1 << 20; // files >= 1 MB are listed individually
const MAX_FILES: usize = 5000; // per folder (so search and duplicates see big media folders whole)
const NONE: u32 = u32::MAX;
const ROOT: u32 = 5; // MFT record number of the root directory
// RECALL_ON_DATA_ACCESS | OFFLINE. (Not RECALL_ON_OPEN, 0x40000: on disk that bit also means
// "has extended attributes", which ordinary local files carry.)
const CLOUD_ATTRS: u32 = 0x40_0000 | 0x1000;
const OTHER: u8 = 8;

fn u16le(b: &[u8], o: usize) -> u16 { u16::from_le_bytes([b[o], b[o + 1]]) }
fn u32le(b: &[u8], o: usize) -> u32 { u32::from_le_bytes(b[o..o + 4].try_into().unwrap()) }
fn u64le(b: &[u8], o: usize) -> u64 { u64::from_le_bytes(b[o..o + 8].try_into().unwrap()) }

fn unix_time(filetime: u64) -> i64 {
    if filetime == 0 { 0 } else { (filetime / 10_000_000) as i64 - 11_644_473_600 }
}

/// Apply the NTFS "update sequence" fixups to a record in place. Returns false if the record is torn.
fn fixup(rec: &mut [u8], sector: usize) -> bool {
    if rec.len() < 48 || &rec[0..4] != b"FILE" { return false; }
    let off = u16le(rec, 4) as usize;
    let cnt = u16le(rec, 6) as usize;
    if cnt == 0 || off + cnt * 2 > rec.len() { return false; }
    let usn = [rec[off], rec[off + 1]];
    for i in 1..cnt {
        let pos = i * sector - 2;
        if pos + 2 > rec.len() { break; }
        if rec[pos] != usn[0] || rec[pos + 1] != usn[1] { return false; }
        rec[pos] = rec[off + i * 2];
        rec[pos + 1] = rec[off + i * 2 + 1];
    }
    true
}

/// Volume geometry from the NTFS boot sector: (bytes per sector, bytes per cluster, bytes per MFT record,
/// byte offset of the MFT). Every value is checked, so a damaged or hostile boot sector gives an error
/// instead of a panic, a huge allocation or a read loop that never advances.
fn geometry(boot: &[u8]) -> Result<(u64, u64, u64, u64), String> {
    if boot.len() < 512 || &boot[3..11] != b"NTFS    " { return Err("not an NTFS volume".into()); }
    let sector = u16le(boot, 0x0B) as u64;
    if ![512, 1024, 2048, 4096].contains(&sector) { return Err(format!("unexpected sector size {sector}")); }
    let spc = match boot[0x0D] {                      // > 0x80 encodes big clusters as 2^(256 - n)
        0 => return Err("zero sectors per cluster".into()),
        n if n <= 0x80 => n as u64,
        n => { let s = 256 - n as u32; if s > 12 { return Err("cluster size out of range".into()); } 1u64 << s }
    };
    if !spc.is_power_of_two() { return Err("sectors per cluster isn't a power of two".into()); }
    let cluster = sector * spc;
    if cluster > 2 << 20 { return Err("cluster size out of range".into()); }
    let cpr = boot[0x40] as i8;
    let rec_size = if cpr > 0 {
        (cpr as u64).checked_mul(cluster).ok_or("record size overflow")?
    } else {
        let s = -(cpr as i32);
        if !(9..=16).contains(&s) { return Err("record size out of range".into()); }
        1u64 << s
    };
    if !(256..=65536).contains(&rec_size) || !rec_size.is_power_of_two() {
        return Err(format!("unexpected MFT record size {rec_size}"));
    }
    let mft_at = u64le(boot, 0x30).checked_mul(cluster).ok_or("MFT position overflow")?;
    Ok((sector, cluster, rec_size, mft_at))
}

/// Decode a non-resident attribute's data runs into (starting cluster, cluster count) pairs.
/// Never reads past the attribute or the record, so a damaged entry can't crash the scan.
fn data_runs(rec: &[u8], attr: usize) -> Vec<(i64, u64)> {
    let mut runs = Vec::new();
    if attr + 0x40 > rec.len() { return runs; }
    let end = (attr + u32le(rec, attr + 4) as usize).min(rec.len());
    let mut p = attr + u16le(rec, attr + 0x20) as usize;
    let mut lcn: i64 = 0;
    while p < end && rec[p] != 0 {
        let h = rec[p];
        let (ln, lo) = ((h & 0x0f) as usize, (h >> 4) as usize);
        p += 1;
        if ln > 8 || lo > 8 || p + ln + lo > end { break; }
        let mut len: u64 = 0;
        for i in 0..ln { len |= (rec[p + i] as u64) << (8 * i); }
        p += ln;
        if lo > 0 {
            let mut off: i64 = 0;
            for i in 0..lo { off |= (rec[p + i] as i64) << (8 * i); }
            if rec[p + lo - 1] & 0x80 != 0 { off -= 1i64 << (8 * lo); } // sign-extend
            lcn += off;
            runs.push((lcn, len));
        } else {
            runs.push((-1, len)); // sparse run (not expected in $MFT)
        }
        p += lo;
    }
    runs
}

struct Records {
    parent: Vec<u32>,
    flags: Vec<u8>, // 1 = in use, 2 = directory, 4 = has a name, 8 = online-only
    name_ns: Vec<u8>,
    size: Vec<u64>,
    mtime: Vec<i64>,
    name: Vec<Box<str>>,
}

fn parse_record(rec: &[u8], idx: usize, r: &mut Records) {
    if rec.len() < 0x30 { return; }
    let base = (u64le(rec, 0x20) & 0xFFFF_FFFF_FFFF) as usize;
    let target = if base != 0 { base } else { idx };
    if target >= r.parent.len() { return; }
    let rflags = u16le(rec, 0x16);
    if rflags & 1 == 0 { return; } // deleted record (base or extension): ignore its stale contents
    if base == 0 {
        r.flags[idx] |= 1;
        if rflags & 2 != 0 { r.flags[idx] |= 2; }
    }
    let mut off = u16le(rec, 0x14) as usize;
    while off + 16 <= rec.len() {
        let typ = u32le(rec, off);
        if typ == 0xFFFF_FFFF { break; }
        let len = u32le(rec, off + 4) as usize;
        if len == 0 || off + len > rec.len() { break; }
        let nonres = rec[off + 8];
        let name_len = rec[off + 9];
        // the header must fit inside the attribute: 0x18 bytes resident, 0x40 non-resident
        if len < if nonres == 0 { 0x18 } else { 0x40 } { off += len; continue; }
        let attr_end = off + len;
        match typ {
            0x10 if nonres == 0 && base == 0 => { // $STANDARD_INFORMATION
                let c = off + u16le(rec, off + 0x14) as usize;
                if c + 0x24 <= attr_end {
                    r.mtime[idx] = unix_time(u64le(rec, c + 0x08));
                    if u32le(rec, c + 0x20) & CLOUD_ATTRS != 0 { r.flags[idx] |= 8; }
                }
            }
            0x30 if nonres == 0 => { // $FILE_NAME
                let c = off + u16le(rec, off + 0x14) as usize;
                if c + 0x42 > attr_end { off += len; continue; }
                let nlen = rec[c + 0x40] as usize;
                let ns = rec[c + 0x41];
                if c + 0x42 + nlen * 2 > attr_end { off += len; continue; }
                let have = r.flags[target] & 4 != 0;
                // prefer Win32 / POSIX names over 8.3 DOS names; keep the first real name for hard links
                if !have || (r.name_ns[target] == 2 && ns != 2) {
                    let units: Vec<u16> = (0..nlen).map(|i| u16le(rec, c + 0x42 + i * 2)).collect();
                    r.name[target] = String::from_utf16_lossy(&units).into_boxed_str();
                    r.name_ns[target] = ns;
                    r.parent[target] = (u64le(rec, c) & 0xFFFF_FFFF_FFFF).min(u32::MAX as u64 - 1) as u32;
                    r.flags[target] |= 4;
                }
            }
            0x80 if name_len == 0 => { // unnamed $DATA (named streams are alternate data streams)
                if nonres == 0 {
                    r.size[target] = u32le(rec, off + 0x10) as u64;
                } else if u64le(rec, off + 0x10) == 0 { // first extent holds the real size
                    // sparse (0x8000) or compressed (0x0001) streams: count the clusters really
                    // allocated ("total allocated", at 0x40), not the logical length
                    let flags = u16le(rec, off + 0x0C);
                    r.size[target] = if flags & 0x8001 != 0 && len >= 0x48 && off + 0x48 <= rec.len() {
                        u64le(rec, off + 0x40)
                    } else {
                        u64le(rec, off + 0x30)
                    };
                }
            }
            _ => {}
        }
        off += len;
    }
}

#[derive(Default, Clone)]
struct Agg {
    size: u64, nfiles: u64, cats: [u64; 9], osize: u64, ocount: u64,
    msum: f64, mtime: i64, own: i64, cloud: u64,
    kept: Vec<(u64, u32)>, // (size, record) of files >= KEEP_FILE
}

fn json_str(out: &mut Vec<u8>, s: &str) {
    out.push(b'"');
    for ch in s.chars() {
        match ch {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            c if (c as u32) < 0x20 => out.extend_from_slice(format!("\\u{:04x}", c as u32).as_bytes()),
            c => { let mut b = [0u8; 4]; out.extend_from_slice(c.encode_utf8(&mut b).as_bytes()); }
        }
    }
    out.push(b'"');
}

fn progress(path: &str, phase: &str, done: u64, total: u64, bytes: u64) {
    let _ = std::fs::write(path, format!("{}\t{}\t{}\t{}\n", phase, done, total, bytes));
}

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 5 { return Err("usage: hoard-mft <drive letter> <out.json> <progress.txt> <extmap.tsv>".into()); }
    let letter = args[1].trim_end_matches(&[':', '\\'][..]).to_uppercase();
    let (out_path, prog_path) = (&args[2], &args[3]);
    let t0 = Instant::now();

    let mut extmap: HashMap<String, u8> = HashMap::new();
    for line in std::fs::read_to_string(&args[4]).map_err(|e| format!("extmap: {e}"))?.lines() {
        if let Some((e, c)) = line.split_once('\t') { extmap.insert(e.to_string(), c.parse().unwrap_or(OTHER)); }
    }

    let mut vol: File = OpenOptions::new().read(true).share_mode(1 | 2 | 4)
        .open(format!("\\\\.\\{}:", letter)).map_err(|e| format!("open volume: {e}"))?;
    let mut boot = [0u8; 512];
    vol.read_exact(&mut boot).map_err(|e| format!("read boot sector: {e}"))?;
    let (sector, cluster, rec_size, mft_at) = geometry(&boot)?;

    // record 0 describes the $MFT itself: where it lives on disk and how big it is
    let mut rec0 = vec![0u8; rec_size as usize];
    vol.seek(SeekFrom::Start(mft_at)).map_err(|e| e.to_string())?;
    vol.read_exact(&mut rec0).map_err(|e| format!("read $MFT: {e}"))?;
    if !fixup(&mut rec0, sector as usize) { return Err("bad $MFT record".into()); }
    let (mut runs, mut mft_size) = (Vec::new(), 0u64);
    let mut off = u16le(&rec0, 0x14) as usize;
    while off + 16 <= rec0.len() {
        let typ = u32le(&rec0, off);
        if typ == 0xFFFF_FFFF { break; }
        let len = u32le(&rec0, off + 4) as usize;
        if len == 0 || off + len > rec0.len() { break; }
        if typ == 0x80 && rec0[off + 8] == 1 && rec0[off + 9] == 0 && len >= 0x40 {
            runs = data_runs(&rec0, off);
            mft_size = u64le(&rec0, off + 0x30);
            break;
        }
        off += len;
    }
    if runs.is_empty() { return Err("couldn't find the $MFT data".into()); }
    let n = mft_size / rec_size;
    if n == 0 || n > 1 << 31 { return Err(format!("the file table looks wrong ({n} records)")); }
    for &(lcn, clusters) in &runs {               // positions must fit before any arithmetic on them
        if clusters.checked_mul(cluster).is_none() || (lcn.max(0) as u64).checked_mul(cluster).is_none() {
            return Err("the file table's location looks wrong".into());
        }
    }
    let n = n as usize;
    let mut r = Records {
        parent: vec![NONE; n], flags: vec![0; n], name_ns: vec![0; n], size: vec![0; n],
        mtime: vec![0; n], name: vec![Box::from(""); n],
    };

    // read the whole MFT sequentially, run by run, in big chunks
    let chunk_bytes: u64 = (16 << 20) / rec_size * rec_size;
    let mut buf = vec![0u8; chunk_bytes as usize];
    let mut idx: usize = 0;
    'outer: for &(lcn, clusters) in &runs {
        let mut remaining = clusters * cluster;
        let mut pos = lcn.max(0) as u64 * cluster;
        while remaining > 0 {
            let want = remaining.min(chunk_bytes);
            if lcn < 0 { idx += (want / rec_size) as usize; remaining -= want; continue; }
            vol.seek(SeekFrom::Start(pos)).map_err(|e| e.to_string())?;
            vol.read_exact(&mut buf[..want as usize]).map_err(|e| format!("read MFT: {e}"))?;
            for rec in buf[..want as usize].chunks_exact_mut(rec_size as usize) {
                if idx >= n { break 'outer; }
                if fixup(rec, sector as usize) { parse_record(rec, idx, &mut r); }
                idx += 1;
            }
            pos += want;
            remaining -= want;
            progress(prog_path, "Reading the file table", idx as u64, n as u64, 0);
        }
    }

    // children lists (records reachable from the root only, so orphans and deleted entries drop out)
    let mut first_child = vec![NONE; n];
    let mut next_sibling = vec![NONE; n];
    for i in 0..n {
        if r.flags[i] & 5 != 5 || i as u32 == ROOT { continue; }
        let p = r.parent[i] as usize;
        if p >= n || r.flags[p] & 3 != 3 || p == i { continue; }
        next_sibling[i] = first_child[p];
        first_child[p] = i as u32;
    }

    // post-order over directories: sum files, then fold children into parents
    let mut order: Vec<u32> = Vec::new();
    let mut stack = vec![ROOT];
    let mut seen = vec![false; n];
    while let Some(d) = stack.pop() {
        if seen[d as usize] { continue; }
        seen[d as usize] = true;
        order.push(d);
        let mut c = first_child[d as usize];
        while c != NONE {
            if r.flags[c as usize] & 2 != 0 { stack.push(c); }
            c = next_sibling[c as usize];
        }
    }
    let mut aggs: HashMap<u32, Agg> = HashMap::with_capacity(order.len());
    let mut total_bytes = 0u64;
    for (k, &d) in order.iter().enumerate().rev() {
        let mut a = Agg::default();
        let mut c = first_child[d as usize];
        while c != NONE {
            let ci = c as usize;
            if r.flags[ci] & 2 != 0 {
                if let Some(ca) = aggs.get(&c) {
                    a.size += ca.size; a.nfiles += ca.nfiles; a.msum += ca.msum; a.cloud += ca.cloud;
                    for i in 0..9 { a.cats[i] += ca.cats[i]; }
                    a.mtime = a.mtime.max(ca.mtime);
                }
            } else {
                let logical = r.size[ci];
                let (sz, cl) = if r.flags[ci] & 8 != 0 { (0, logical) } else { (logical, 0) };
                let mt = r.mtime[ci];
                let name = &r.name[ci];
                let ext = name.rfind('.').filter(|&i| i > 0).map(|i| name[i + 1..].to_lowercase()).unwrap_or_default();
                let cat = *extmap.get(&ext).unwrap_or(&OTHER);
                a.size += sz; a.nfiles += 1; a.cats[cat as usize] += sz; a.cloud += cl;
                a.msum += sz as f64 * mt as f64; a.own = a.own.max(mt);
                total_bytes += sz;
                if sz >= KEEP_FILE { a.kept.push((sz, c)); } else { a.osize += sz; a.ocount += 1; }
            }
            c = next_sibling[ci];
        }
        a.kept.sort_unstable_by(|x, y| y.0.cmp(&x.0));
        for &(s, _) in a.kept.iter().skip(MAX_FILES) { a.osize += s; a.ocount += 1; }
        a.kept.truncate(MAX_FILES);
        a.mtime = a.mtime.max(a.own);
        aggs.insert(d, a);
        if k % 20000 == 0 { progress(prog_path, "Adding up folders", (order.len() - k) as u64, order.len() as u64, total_bytes); }
    }

    // write the tree (iteratively, so very deep folders can't overflow the stack)
    progress(prog_path, "Saving", 0, 0, total_bytes);
    let mut out: Vec<u8> = Vec::with_capacity(64 << 20);
    enum Step { Open(u32), Close(u32), Comma }
    let mut todo = vec![Step::Open(ROOT)];
    while let Some(step) = todo.pop() {
        match step {
            Step::Comma => out.push(b','),
            Step::Open(d) => {
                let a = &aggs[&d];
                out.push(b'[');
                if d == ROOT { json_str(&mut out, &format!("{}:", letter)); } else { json_str(&mut out, &r.name[d as usize]); }
                write!(out, ",{},{},[{}],{},{},[", a.size, a.nfiles,
                       a.cats.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(","), a.osize, a.ocount).unwrap();
                for (i, &(s, f)) in a.kept.iter().enumerate() {
                    if i > 0 { out.push(b','); }
                    out.push(b'[');
                    json_str(&mut out, &r.name[f as usize]);
                    let name = &r.name[f as usize];
                    let ext = name.rfind('.').filter(|&i| i > 0).map(|i| name[i + 1..].to_lowercase()).unwrap_or_default();
                    write!(out, ",{},{},{}]", s, extmap.get(&ext).unwrap_or(&OTHER), r.mtime[f as usize]).unwrap();
                }
                out.extend_from_slice(b"],[");
                todo.push(Step::Close(d));
                let mut kids = Vec::new();
                let mut c = first_child[d as usize];
                while c != NONE {
                    if r.flags[c as usize] & 2 != 0 && aggs.contains_key(&c) { kids.push(c); }
                    c = next_sibling[c as usize];
                }
                for (i, &k) in kids.iter().enumerate().rev() {
                    todo.push(Step::Open(k));
                    if i > 0 { todo.push(Step::Comma); }
                }
            }
            Step::Close(d) => {
                let a = &aggs[&d];
                write!(out, "],{},{:.0},{},{}]", a.mtime, a.msum, a.own, a.cloud).unwrap();
            }
        }
    }
    let f = File::create(out_path).map_err(|e| format!("write output: {e}"))?;
    let mut w = BufWriter::new(f);
    w.write_all(&out).map_err(|e| e.to_string())?;
    w.flush().map_err(|e| e.to_string())?;
    let files: u64 = aggs[&ROOT].nfiles;
    progress(prog_path, &format!("done in {:.1}s", t0.elapsed().as_secs_f64()), files, files, total_bytes);
    Ok(())
}

pub fn main() {
    // a big stack for the worker thread, just in case
    let h = std::thread::Builder::new().stack_size(64 << 20).spawn(run).unwrap();
    match h.join().unwrap() {
        Ok(()) => std::process::exit(0),
        Err(e) => {
            let args: Vec<String> = std::env::args().collect();
            if args.len() > 3 { let _ = std::fs::write(&args[3], format!("error\t{}\n", e)); }
            eprintln!("hoard-mft: {e}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A minimal 1 KB FILE record with an update-sequence array, ready for attributes at 0x38.
    fn record() -> Vec<u8> {
        let mut r = vec![0u8; 1024];
        r[0..4].copy_from_slice(b"FILE");
        r[4..6].copy_from_slice(&0x30u16.to_le_bytes()); // USA offset
        r[6..8].copy_from_slice(&3u16.to_le_bytes()); // USN + 2 sector entries
        r[0x14..0x16].copy_from_slice(&0x38u16.to_le_bytes()); // first attribute
        r[0x16..0x18].copy_from_slice(&1u16.to_le_bytes()); // in use
        r[0x30..0x32].copy_from_slice(&[0xAB, 0xCD]); // USN
        r[0x32..0x34].copy_from_slice(&[1, 2]); // real bytes for the end of sector 1
        r[0x34..0x36].copy_from_slice(&[3, 4]); // ... and sector 2
        r[510..512].copy_from_slice(&[0xAB, 0xCD]);
        r[1022..1024].copy_from_slice(&[0xAB, 0xCD]);
        r
    }

    fn put_attr(r: &mut Vec<u8>, off: usize, typ: u32, body: &[u8], nonres: bool) -> usize {
        let hdr = if nonres { 0x40 } else { 0x18 };
        let len = (hdr + body.len() + 7) & !7;
        r[off..off + 4].copy_from_slice(&typ.to_le_bytes());
        r[off + 4..off + 8].copy_from_slice(&(len as u32).to_le_bytes());
        r[off + 8] = nonres as u8;
        if !nonres {
            r[off + 0x10..off + 0x14].copy_from_slice(&(body.len() as u32).to_le_bytes());
            r[off + 0x14..off + 0x16].copy_from_slice(&(hdr as u16).to_le_bytes());
        }
        r[off + hdr..off + hdr + body.len()].copy_from_slice(body);
        off + len
    }

    fn records(n: usize) -> Records {
        Records { parent: vec![NONE; n], flags: vec![0; n], name_ns: vec![0; n], size: vec![0; n],
                  mtime: vec![0; n], name: vec![Box::from(""); n] }
    }

    fn boot(sector: u16, spc: u8, cpr: i8, mft_lcn: u64) -> Vec<u8> {
        let mut b = vec![0u8; 512];
        b[3..11].copy_from_slice(b"NTFS    ");
        b[0x0B..0x0D].copy_from_slice(&sector.to_le_bytes());
        b[0x0D] = spc;
        b[0x30..0x38].copy_from_slice(&mft_lcn.to_le_bytes());
        b[0x40] = cpr as u8;
        b
    }

    #[test]
    fn geometry_reads_normal_volumes() {
        // the common layout: 512-byte sectors, 4 KB clusters, 1 KB records (encoded as -10)
        assert_eq!(geometry(&boot(512, 8, -10, 786432)).unwrap(), (512, 4096, 1024, 786432 * 4096));
        // 4Kn drive with 4 KB records, and big clusters encoded as 2^(256 - n)
        assert_eq!(geometry(&boot(4096, 1, -12, 2)).unwrap().2, 4096);
        assert_eq!(geometry(&boot(512, 0xF4, -10, 1)).unwrap().1, 512 << 12);
        // positive clusters-per-record
        assert_eq!(geometry(&boot(512, 2, 1, 1)).unwrap().2, 1024);
    }

    #[test]
    fn geometry_rejects_damaged_boot_sectors() {
        assert!(geometry(&boot(0, 8, -10, 1)).is_err());              // no sector size
        assert!(geometry(&boot(513, 8, -10, 1)).is_err());            // odd sector size
        assert!(geometry(&boot(512, 0, -10, 1)).is_err());            // zero sectors per cluster
        assert!(geometry(&boot(512, 3, -10, 1)).is_err());            // not a power of two
        assert!(geometry(&boot(512, 0x81, -10, 1)).is_err());         // absurd cluster size
        assert!(geometry(&boot(512, 8, -100, 1)).is_err());           // shift that would panic
        assert!(geometry(&boot(512, 8, -30, 1)).is_err());            // 1 GB records (zero-sized read chunks)
        assert!(geometry(&boot(512, 128, 127, 1)).is_err());          // records larger than 64 KB
        assert!(geometry(&boot(512, 8, -10, u64::MAX)).is_err());     // MFT position overflows
        let mut not_ntfs = boot(512, 8, -10, 1);
        not_ntfs[3] = b'X';
        assert!(geometry(&not_ntfs).is_err());
        assert!(geometry(&[0u8; 100]).is_err());                       // short buffer
    }

    #[test]
    fn fixup_restores_sector_ends_and_rejects_torn_records() {
        let mut r = record();
        assert!(fixup(&mut r, 512));
        assert_eq!(&r[510..512], &[1, 2]);
        assert_eq!(&r[1022..1024], &[3, 4]);
        let mut torn = record();
        torn[1022] = 0; // sector 2 written by a different update
        assert!(!fixup(&mut torn, 512));
        let mut short = vec![0u8; 20];
        assert!(!fixup(&mut short, 512));
    }

    #[test]
    fn parses_name_size_time_and_cloud_flag() {
        let mut r = record();
        let mut si = vec![0u8; 0x30];
        let ft: u64 = (1_700_000_000 + 11_644_473_600) * 10_000_000;
        si[8..16].copy_from_slice(&ft.to_le_bytes());
        si[0x20..0x24].copy_from_slice(&0x40_0000u32.to_le_bytes()); // RECALL_ON_DATA_ACCESS
        let mut off = put_attr(&mut r, 0x38, 0x10, &si, false);
        let name: Vec<u16> = "movie.mp4".encode_utf16().collect();
        let mut fname = vec![0u8; 0x42 + name.len() * 2];
        fname[0..8].copy_from_slice(&5u64.to_le_bytes()); // parent = root
        fname[0x40] = name.len() as u8;
        fname[0x41] = 1; // Win32 name
        for (i, u) in name.iter().enumerate() { fname[0x42 + i * 2..0x44 + i * 2].copy_from_slice(&u.to_le_bytes()); }
        off = put_attr(&mut r, off, 0x30, &fname, false);
        off = put_attr(&mut r, off, 0x80, &[7u8; 100], false); // resident data, 100 bytes
        r[off..off + 4].copy_from_slice(&0xFFFF_FFFFu32.to_le_bytes());
        let mut rs = records(10);
        parse_record(&r, 7, &mut rs);
        assert_eq!(&*rs.name[7], "movie.mp4");
        assert_eq!(rs.parent[7], 5);
        assert_eq!(rs.size[7], 100);
        assert_eq!(rs.mtime[7], 1_700_000_000);
        assert_eq!(rs.flags[7] & 8, 8, "online-only flag");
    }

    #[test]
    fn damaged_records_are_skipped_not_crashing() {
        // attribute claims to run past the end of the record
        let mut r = record();
        r[0x38..0x3C].copy_from_slice(&0x30u32.to_le_bytes());
        r[0x3C..0x40].copy_from_slice(&5000u32.to_le_bytes());
        let mut rs = records(4);
        parse_record(&r, 1, &mut rs);
        // a FILE_NAME whose content offset points outside the attribute
        let mut r2 = record();
        let off = put_attr(&mut r2, 0x38, 0x30, &[0u8; 8], false);
        r2[0x38 + 0x14..0x38 + 0x16].copy_from_slice(&900u16.to_le_bytes());
        r2[off..off + 4].copy_from_slice(&0xFFFF_FFFFu32.to_le_bytes());
        parse_record(&r2, 2, &mut rs);
        assert_eq!(rs.flags[2] & 4, 0, "no name taken from a bogus offset");
        // random garbage
        let mut x: u32 = 12345;
        for _ in 0..2000 {
            let mut g = record();
            for b in g[0x38..].iter_mut() { x = x.wrapping_mul(1_103_515_245).wrapping_add(12345); *b = (x >> 16) as u8; }
            parse_record(&g, 3, &mut rs);
            let _ = data_runs(&g, 0x38);
        }
    }

    #[test]
    fn data_runs_decode_and_stay_in_bounds() {
        let mut r = vec![0u8; 0x80];
        r[4..8].copy_from_slice(&0x60u32.to_le_bytes());
        r[0x20..0x22].copy_from_slice(&0x40u16.to_le_bytes());
        // run 1: 0x10 clusters at LCN 0x1000; run 2: 8 clusters at -0x100 relative
        r[0x40..0x46].copy_from_slice(&[0x21, 0x10, 0x00, 0x10, 0x00, 0x00]);
        r[0x43] = 0x10;
        r[0x40..0x44].copy_from_slice(&[0x21, 0x10, 0x00, 0x10]);
        r[0x44..0x48].copy_from_slice(&[0x21, 0x08, 0x00, 0xFF]);
        let runs = data_runs(&r, 0);
        assert_eq!(runs, vec![(0x1000, 0x10), (0x1000 - 0x100, 8)]);
        let mut bad = r.clone();
        bad[0x40] = 0xFF; // claims 15+15 byte fields
        assert!(data_runs(&bad, 0).is_empty());
    }
}
} // mod mft

#[cfg(windows)]
fn main() {
    mft::main()
}

#[cfg(not(windows))]
fn main() {
    eprintln!("hoard-mft reads NTFS volumes and only runs on Windows.");
    std::process::exit(2);
}
