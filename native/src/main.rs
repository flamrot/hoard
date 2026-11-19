//! hoard-mft: read the NTFS master file table (MFT) of a volume directly and write Hoard's scan tree.
//!
//! Usage: hoard-mft <drive letter> <out.json> <progress.txt> <extmap.tsv>
//!
//! Reading the raw volume needs administrator rights. Instead of walking every folder (one system
//! call per file), this reads the MFT sequentially, so a whole drive takes seconds.
//! Output is the same nested-array format Hoard's Python scanner saves (see core._enc):
//!   [name, size, nfiles, cats[9], osize, ocount, [[file, size, cat, mtime]...], [child dirs...],
//!    newest_mtime, size*mtime sum, newest direct-file mtime, online-only bytes]

use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Read, Seek, SeekFrom, Write};
use std::os::windows::fs::OpenOptionsExt;
use std::time::Instant;

const KEEP_FILE: u64 = 1 << 20; // files >= 1 MB are listed individually
const MAX_FILES: usize = 300; // per folder
const NONE: u32 = u32::MAX;
const ROOT: u32 = 5; // MFT record number of the root directory
const CLOUD_ATTRS: u32 = 0x40_0000 | 0x4_0000 | 0x1000; // RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE
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

/// Decode a non-resident attribute's data runs into (starting cluster, cluster count) pairs.
fn data_runs(rec: &[u8], attr: usize) -> Vec<(i64, u64)> {
    let mut runs = Vec::new();
    let mut p = attr + u16le(rec, attr + 0x20) as usize;
    let end = attr + u32le(rec, attr + 4) as usize;
    let mut lcn: i64 = 0;
    while p < end && rec[p] != 0 {
        let h = rec[p];
        let (ln, lo) = ((h & 0x0f) as usize, (h >> 4) as usize);
        p += 1;
        if p + ln + lo > end { break; }
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
        match typ {
            0x10 if nonres == 0 && base == 0 => { // $STANDARD_INFORMATION
                let c = off + u16le(rec, off + 0x14) as usize;
                if c + 0x24 <= rec.len() {
                    r.mtime[idx] = unix_time(u64le(rec, c + 0x08));
                    if u32le(rec, c + 0x20) & CLOUD_ATTRS != 0 { r.flags[idx] |= 8; }
                }
            }
            0x30 if nonres == 0 => { // $FILE_NAME
                let c = off + u16le(rec, off + 0x14) as usize;
                if c + 0x42 > rec.len() { off += len; continue; }
                let nlen = rec[c + 0x40] as usize;
                let ns = rec[c + 0x41];
                if c + 0x42 + nlen * 2 > rec.len() { off += len; continue; }
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
                    r.size[target] = u64le(rec, off + 0x30);
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
    if &boot[3..11] != b"NTFS    " { return Err("not an NTFS volume".into()); }
    let sector = u16le(&boot, 0x0B) as u64;
    let cluster = sector * boot[0x0D] as u64;
    let mft_lcn = u64le(&boot, 0x30);
    let cpr = boot[0x40] as i8;
    let rec_size: u64 = if cpr > 0 { cpr as u64 * cluster } else { 1u64 << (-(cpr as i32)) };

    // record 0 describes the $MFT itself: where it lives on disk and how big it is
    let mut rec0 = vec![0u8; rec_size as usize];
    vol.seek(SeekFrom::Start(mft_lcn * cluster)).map_err(|e| e.to_string())?;
    vol.read_exact(&mut rec0).map_err(|e| format!("read $MFT: {e}"))?;
    if !fixup(&mut rec0, sector as usize) { return Err("bad $MFT record".into()); }
    let (mut runs, mut mft_size) = (Vec::new(), 0u64);
    let mut off = u16le(&rec0, 0x14) as usize;
    while off + 16 <= rec0.len() {
        let typ = u32le(&rec0, off);
        if typ == 0xFFFF_FFFF { break; }
        let len = u32le(&rec0, off + 4) as usize;
        if len == 0 { break; }
        if typ == 0x80 && rec0[off + 8] == 1 && rec0[off + 9] == 0 {
            runs = data_runs(&rec0, off);
            mft_size = u64le(&rec0, off + 0x30);
            break;
        }
        off += len;
    }
    if runs.is_empty() { return Err("couldn't find the $MFT data".into()); }
    let n = (mft_size / rec_size) as usize;
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

fn main() {
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
