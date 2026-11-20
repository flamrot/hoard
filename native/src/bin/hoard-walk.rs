//! hoard-walk: scan a folder tree in parallel (Windows, macOS, Linux; no admin needed) and write
//! Hoard's scan tree. Same rules as the Python scanner (core.scan_dir): links and junctions are
//! skipped, files >= 1 MB are kept individually (300 per folder), online-only cloud files count
//! separately, and on macOS/Linux sizes are the real allocation (blocks), so sparse files don't inflate.
//!
//! Usage: hoard-walk <root> <out.json> <progress.txt> <extmap.tsv> [--name NAME] [--exclude PATH]...

use hoard_native::{cat_of, load_extmap, progress, Agg, KEEP_FILE, MAX_FILES};
use std::collections::{HashMap, HashSet, VecDeque};
use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Instant, UNIX_EPOCH};

struct DirRec {
    name: String,
    children: Vec<usize>,
    agg: Agg,
    kept: Vec<(String, u64, u8, i64)>,
}

struct Task {
    id: usize,
    path: PathBuf,
}

struct Shared {
    queue: Mutex<VecDeque<Task>>,
    cv: Condvar,
    pending: AtomicUsize, // queued + being processed
    next_id: AtomicUsize,
    results: Mutex<Vec<Option<DirRec>>>,
    files: AtomicU64,
    bytes: AtomicU64,
    errors: AtomicU64,
    current: Mutex<String>,
    done: AtomicBool,
    /// Windows hard links: a file with several names is counted once, where file ids are trustworthy (NTFS/ReFS).
    /// Unix splits a linked file's size between its names instead (see list_std).
    #[cfg_attr(unix, allow(dead_code))]
    dedupe: bool,
    seen: Mutex<HashSet<(u64, u64)>>,
}

/// Hard-link duplicates smaller than this aren't worth remembering.
const DEDUPE_MIN: u64 = 64 << 10;

impl Shared {
    /// True the first time a file id is seen; false for another name of an already-counted file.
    #[cfg_attr(unix, allow(dead_code))]
    fn first_sighting(&self, key: (u64, u64)) -> bool {
        if !self.dedupe || key == (0, 0) { return true; }
        self.seen.lock().unwrap().insert(key)
    }
}

/// What one folder's listing adds up to.
#[derive(Default)]
struct Acc {
    agg: Agg,
    kept: Vec<(String, u64, u8, i64)>,
    children: Vec<usize>,
}

impl Acc {
    fn file(&mut self, sh: &Shared, name: String, sz: u64, cl: u64, mt: i64, cat: u8) {
        let keep = sz >= KEEP_FILE;
        self.agg.add_file(sz, cl, mt, cat, keep);
        if keep { self.kept.push((name, sz, cat, mt)); }
        sh.files.fetch_add(1, Ordering::Relaxed);
        sh.bytes.fetch_add(sz, Ordering::Relaxed);
    }
    fn dir(&mut self, sh: &Shared, name: String, path: PathBuf) {
        let id = sh.next_id.fetch_add(1, Ordering::Relaxed);
        self.children.push(id);
        {
            let mut r = sh.results.lock().unwrap();
            if r.len() <= id { r.resize_with(id + 1024, || None); }
            r[id] = Some(DirRec { name, children: vec![], agg: Agg::default(), kept: vec![] });
        }
        sh.pending.fetch_add(1, Ordering::SeqCst);
        sh.queue.lock().unwrap().push_back(Task { id, path });
        sh.cv.notify_one();
    }
}

/// Windows: list a folder with GetFileInformationByHandleEx(FileIdBothDirectoryInfo). One call per batch
/// gives every entry's attributes, logical *and allocated* size, write time and file id, so sparse and
/// compressed files count what they really use and hard links can be counted once, at no extra cost.
#[cfg(windows)]
mod winlist {
    use std::ffi::{c_void, OsString};
    use std::os::windows::ffi::{OsStrExt, OsStringExt};
    use std::path::Path;

    type Handle = *mut c_void;
    #[link(name = "kernel32")]
    extern "system" {
        fn CreateFileW(name: *const u16, access: u32, share: u32, sa: *mut c_void, disposition: u32, flags: u32, template: Handle) -> Handle;
        fn GetFileInformationByHandleEx(h: Handle, class: i32, buf: *mut c_void, size: u32) -> i32;
        fn CloseHandle(h: Handle) -> i32;
        fn GetLastError() -> u32;
        fn GetVolumePathNameW(file: *const u16, out: *mut u16, len: u32) -> i32;
        fn GetVolumeInformationW(root: *const u16, vname: *mut u16, vlen: u32, serial: *mut u32, maxc: *mut u32,
                                 flags: *mut u32, fsname: *mut u16, fslen: u32) -> i32;
    }

    pub struct Entry {
        pub name: OsString,
        pub attrs: u32,
        pub tag: u32,   // reparse tag (only when attrs has REPARSE_POINT)
        pub size: u64,
        pub alloc: u64,
        pub mtime: i64,
        pub id: u64,
    }

    /// Wide, NUL-terminated path; long paths get the \\?\ (or \\?\UNC\) prefix.
    pub fn wide(p: &Path) -> Vec<u16> {
        let mut v: Vec<u16> = p.as_os_str().encode_wide().collect();
        let starts = |v: &[u16], pre: &str| pre.encode_utf16().enumerate().all(|(i, c)| v.get(i) == Some(&c));
        if v.len() > 240 && !starts(&v, "\\\\?\\") {
            let mut n: Vec<u16> = if starts(&v, "\\\\") { "\\\\?\\UNC".encode_utf16().chain(v[1..].iter().copied()).collect() }
                                  else { "\\\\?\\".encode_utf16().chain(v.iter().copied()).collect() };
            std::mem::swap(&mut v, &mut n);
        }
        v.push(0);
        v
    }

    pub fn unix_time(filetime: i64) -> i64 { if filetime <= 0 { 0 } else { filetime / 10_000_000 - 11_644_473_600 } }

    fn u32at(b: &[u8], o: usize) -> u32 { u32::from_le_bytes(b[o..o + 4].try_into().unwrap()) }
    fn u64at(b: &[u8], o: usize) -> u64 { u64::from_le_bytes(b[o..o + 8].try_into().unwrap()) }

    /// Decode one buffer of FILE_ID_BOTH_DIR_INFO records (never reads past the buffer).
    pub fn parse(bytes: &[u8], out: &mut Vec<Entry>) {
        let mut off = 0usize;
        while off + 104 <= bytes.len() {
            let next = u32at(bytes, off) as usize;
            let nlen = u32at(bytes, off + 60) as usize;
            let start = off + 104;
            if start + nlen > bytes.len() { break; }
            let units: Vec<u16> = bytes[start..start + nlen].chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
            let dot = units == [46] || units == [46, 46];
            if !dot {
                let attrs = u32at(bytes, off + 56);
                out.push(Entry {
                    name: OsString::from_wide(&units), attrs,
                    tag: if attrs & 0x400 != 0 { u32at(bytes, off + 64) } else { 0 },   // EaSize holds the reparse tag
                    size: u64at(bytes, off + 40), alloc: u64at(bytes, off + 48),
                    mtime: unix_time(u64at(bytes, off + 24) as i64), id: u64at(bytes, off + 96),
                });
            }
            if next == 0 { break; }
            off += next;
        }
    }

    /// Ok(entries); Err(true) = this file system can't list this way (use read_dir); Err(false) = unreadable.
    pub fn list(dir: &Path) -> Result<Vec<Entry>, bool> {
        let w = wide(dir);
        // FILE_LIST_DIRECTORY, share read/write/delete, OPEN_EXISTING, BACKUP_SEMANTICS (needed for folders)
        let h = unsafe { CreateFileW(w.as_ptr(), 0x0001, 7, std::ptr::null_mut(), 3, 0x0200_0000, std::ptr::null_mut()) };
        if h.is_null() || h as isize == -1 { return Err(false); }
        let mut buf = vec![0u64; 8192];       // 64 KB, 8-byte aligned
        let mut out = Vec::new();
        loop {
            let ok = unsafe { GetFileInformationByHandleEx(h, 10, buf.as_mut_ptr() as *mut c_void, (buf.len() * 8) as u32) };
            if ok == 0 {
                let e = unsafe { GetLastError() };
                unsafe { CloseHandle(h) };
                return if e == 18 { Ok(out) } else { Err(matches!(e, 1 | 50 | 87 | 124)) };   // NO_MORE_FILES / unsupported
            }
            let bytes = unsafe { std::slice::from_raw_parts(buf.as_ptr() as *const u8, buf.len() * 8) };
            parse(bytes, &mut out);
        }
    }

    /// File ids are only trustworthy for hard-link detection on NTFS and ReFS (not FAT/exFAT or some shares).
    pub fn ids_reliable(root: &Path) -> bool {
        let w = wide(root);
        let mut vol = [0u16; 512];
        let mut fs = [0u16; 64];
        unsafe {
            if GetVolumePathNameW(w.as_ptr(), vol.as_mut_ptr(), vol.len() as u32) == 0 { return false; }
            if GetVolumeInformationW(vol.as_ptr(), std::ptr::null_mut(), 0, std::ptr::null_mut(), std::ptr::null_mut(),
                                     std::ptr::null_mut(), fs.as_mut_ptr(), fs.len() as u32) == 0 { return false; }
        }
        let name = String::from_utf16_lossy(&fs[..fs.iter().position(|&c| c == 0).unwrap_or(fs.len())]);
        name == "NTFS" || name == "ReFS"
    }
}

fn mtime_of(md: &fs::Metadata) -> i64 {
    md.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map(|d| d.as_secs() as i64).unwrap_or(0)
}

/// (bytes on this disk, bytes that live in the cloud) for one file.
#[cfg(windows)]
fn sizes(md: &fs::Metadata) -> (u64, u64) {
    use std::os::windows::fs::MetadataExt;
    const CLOUD: u32 = 0x40_0000 | 0x1000; // RECALL_ON_DATA_ACCESS | OFFLINE
    if md.file_attributes() & CLOUD != 0 { (0, md.len()) } else { (md.len(), 0) }
}
#[cfg(target_os = "macos")]
fn sizes(md: &fs::Metadata) -> (u64, u64) {
    use std::os::macos::fs::MetadataExt;
    if md.st_flags() & 0x4000_0000 != 0 { (0, md.len()) } else { (md.st_blocks() * 512, 0) } // SF_DATALESS (iCloud)
}
#[cfg(all(unix, not(target_os = "macos")))]
fn sizes(md: &fs::Metadata) -> (u64, u64) {
    use std::os::unix::fs::MetadataExt;
    (md.blocks() * 512, 0)
}

/// The portable listing (macOS/Linux, and Windows file systems that can't do FileIdBothDirectoryInfo).
fn list_std(sh: &Shared, dir: &PathBuf, acc: &mut Acc, extmap: &HashMap<String, u8>, exclude: &HashSet<PathBuf>) {
    let Ok(rd) = fs::read_dir(dir) else { sh.errors.fetch_add(1, Ordering::Relaxed); return; };
    for entry in rd {
        let Ok(e) = entry else { sh.errors.fetch_add(1, Ordering::Relaxed); continue; };
        let Ok(ft) = e.file_type() else { sh.errors.fetch_add(1, Ordering::Relaxed); continue; };
        if ft.is_symlink() { continue; } // includes Windows junctions and mount points
        let name = e.file_name().to_string_lossy().into_owned();
        if ft.is_dir() {
            let path = e.path();
            if !exclude.contains(&path) { acc.dir(sh, name, path); }
        } else if ft.is_file() {
            let Ok(md) = e.metadata() else { sh.errors.fetch_add(1, Ordering::Relaxed); continue; };
            #[allow(unused_mut)]
            let (mut sz, cl) = sizes(&md);
            // Unix hard links: each name carries an equal share, so any part of the tree measures the same on its own
            // as in a whole scan (Hoard re-measures single folders after Files jobs). Same rule as core.scan_dir.
            #[cfg(unix)]
            {
                use std::os::unix::fs::MetadataExt;
                if md.nlink() > 1 && sz >= DEDUPE_MIN { sz /= md.nlink(); }
            }
            let cat = cat_of(extmap, &name);
            acc.file(sh, name, sz, cl, mtime_of(&md), cat);
        }
    }
}

fn process(sh: &Shared, task: Task, extmap: &HashMap<String, u8>, exclude: &HashSet<PathBuf>) {
    let mut acc = Acc::default();
    #[cfg(windows)]
    {
        const CLOUD: u32 = 0x40_0000 | 0x1000; // RECALL_ON_DATA_ACCESS | OFFLINE
        match winlist::list(&task.path) {
            Ok(entries) => for en in entries {
                if en.attrs & 0x400 != 0 && en.tag & 0x2000_0000 != 0 { continue; }   // symlinks, junctions, mount points
                let name = en.name.to_string_lossy().into_owned();
                if en.attrs & 0x10 != 0 {
                    let path = task.path.join(&en.name);
                    if !exclude.contains(&path) { acc.dir(sh, name, path); }
                    continue;
                }
                let (sz, cl) = if en.attrs & CLOUD != 0 { (0, en.size) }
                               else if en.attrs & (0x200 | 0x800) != 0 { (en.alloc, 0) }   // sparse / compressed: what's really used
                               else { (en.size, 0) };
                if sz >= DEDUPE_MIN && !sh.first_sighting((en.id, 1)) { continue; }   // hard link to a counted file
                let cat = cat_of(extmap, &name);
                acc.file(sh, name, sz, cl, en.mtime, cat);
            },
            Err(true) => list_std(sh, &task.path, &mut acc, extmap, exclude),
            Err(false) => { sh.errors.fetch_add(1, Ordering::Relaxed); }
        }
    }
    #[cfg(not(windows))]
    list_std(sh, &task.path, &mut acc, extmap, exclude);
    let Acc { mut agg, mut kept, children } = acc;
    kept.sort_unstable_by(|a, b| b.1.cmp(&a.1));
    for k in kept.iter().skip(MAX_FILES) { agg.osize += k.1; agg.ocount += 1; }
    kept.truncate(MAX_FILES);
    {
        let mut r = sh.results.lock().unwrap();
        let rec = r[task.id].as_mut().unwrap();
        rec.children = children;
        rec.agg = agg;
        rec.kept = kept;
    }
    if let Ok(mut c) = sh.current.try_lock() { *c = task.path.to_string_lossy().into_owned(); }
    if sh.pending.fetch_sub(1, Ordering::SeqCst) == 1 {
        sh.done.store(true, Ordering::SeqCst);
        sh.cv.notify_all();
    }
}

fn worker(sh: Arc<Shared>, extmap: Arc<HashMap<String, u8>>, exclude: Arc<HashSet<PathBuf>>) {
    loop {
        let task = {
            let mut q = sh.queue.lock().unwrap();
            loop {
                if let Some(t) = q.pop_front() { break Some(t); }
                if sh.done.load(Ordering::SeqCst) { break None; }
                q = sh.cv.wait_timeout(q, std::time::Duration::from_millis(50)).unwrap().0;
            }
        };
        match task {
            Some(t) => process(&sh, t, &extmap, &exclude),
            None => return,
        }
    }
}

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 5 {
        return Err("usage: hoard-walk <root> <out.json> <progress.txt> <extmap.tsv> [--name NAME] [--exclude PATH]...".into());
    }
    let root = PathBuf::from(&args[1]);
    let (out_path, prog_path) = (args[2].clone(), args[3].clone());
    let extmap = Arc::new(load_extmap(&args[4])?);
    let mut name = root.to_string_lossy().into_owned();
    let mut exclude = HashSet::new();
    let mut i = 5;
    while i + 1 < args.len() {
        match args[i].as_str() {
            "--name" => name = args[i + 1].clone(),
            "--exclude" => { exclude.insert(PathBuf::from(&args[i + 1])); }
            _ => {}
        }
        i += 2;
    }
    let exclude = Arc::new(exclude);
    let t0 = Instant::now();
    let sh = Arc::new(Shared {
        queue: Mutex::new(VecDeque::new()), cv: Condvar::new(), pending: AtomicUsize::new(1),
        next_id: AtomicUsize::new(1), results: Mutex::new((0..1024).map(|_| None).collect()),
        files: AtomicU64::new(0), bytes: AtomicU64::new(0), errors: AtomicU64::new(0),
        current: Mutex::new(String::new()), done: AtomicBool::new(false),
        #[cfg(windows)]
        dedupe: winlist::ids_reliable(&root),
        #[cfg(not(windows))]
        dedupe: false,
        seen: Mutex::new(HashSet::new()),
    });
    sh.results.lock().unwrap()[0] = Some(DirRec { name: name.clone(), children: vec![], agg: Agg::default(), kept: vec![] });
    sh.queue.lock().unwrap().push_back(Task { id: 0, path: root.clone() });

    let threads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4).clamp(4, 16) * 2;
    let handles: Vec<_> = (0..threads).map(|_| {
        let (s, e, x) = (sh.clone(), extmap.clone(), exclude.clone());
        std::thread::Builder::new().stack_size(1 << 20).spawn(move || worker(s, e, x)).unwrap()
    }).collect();
    while !sh.done.load(Ordering::SeqCst) {
        std::thread::sleep(std::time::Duration::from_millis(200));
        let cur = sh.current.lock().map(|c| c.clone()).unwrap_or_default();
        progress(&prog_path, &cur, sh.files.load(Ordering::Relaxed), 0, sh.bytes.load(Ordering::Relaxed));
    }
    for h in handles { let _ = h.join(); }
    progress(&prog_path, "Adding up folders", sh.files.load(Ordering::Relaxed), 0, sh.bytes.load(Ordering::Relaxed));

    // fold every folder into its parent (children always have bigger ids than their parent)
    let mut recs: Vec<Option<DirRec>> = std::mem::take(&mut *sh.results.lock().unwrap());
    let n = sh.next_id.load(Ordering::SeqCst);
    recs.truncate(n);
    let mut totals: Vec<Agg> = vec![Agg::default(); n];
    for id in (0..n).rev() {
        let Some(rec) = recs[id].as_ref() else { continue };
        let mut a = rec.agg.clone();
        for &c in &rec.children { a.add_child(&totals[c]); }
        totals[id] = a;
    }

    // write the tree, iteratively so deep folders can't overflow a stack, and in chunks so a huge
    // disk never needs the whole output in memory at once
    let mut f = fs::File::create(&out_path).map_err(|e| format!("write output: {e}"))?;
    let mut out: Vec<u8> = Vec::with_capacity(8 << 20);
    enum Step { Open(usize), Close(usize), Comma }
    let mut todo = vec![Step::Open(0)];
    while let Some(step) = todo.pop() {
        if out.len() > 4 << 20 { f.write_all(&out).map_err(|e| e.to_string())?; out.clear(); }
        match step {
            Step::Comma => out.push(b','),
            Step::Open(id) => {
                let rec = recs[id].as_ref().unwrap();
                totals[id].write_head(&mut out, &rec.name, &rec.kept);
                todo.push(Step::Close(id));
                for (k, &c) in rec.children.iter().enumerate().rev() {
                    todo.push(Step::Open(c));
                    if k > 0 { todo.push(Step::Comma); }
                }
            }
            Step::Close(id) => totals[id].write_tail(&mut out),
        }
    }
    f.write_all(&out).map_err(|e| e.to_string())?;
    progress(&prog_path, &format!("done in {:.1}s, {} unreadable", t0.elapsed().as_secs_f64(), sh.errors.load(Ordering::Relaxed)),
             totals[0].nfiles, sh.errors.load(Ordering::Relaxed), totals[0].size);
    Ok(())
}

fn main() {
    if let Err(e) = run() {
        let args: Vec<String> = std::env::args().collect();
        if args.len() > 3 { let _ = fs::write(&args[3], format!("error\t{}\n", e)); }
        eprintln!("hoard-walk: {e}");
        std::process::exit(1);
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::winlist::*;
    use std::path::Path;

    fn record(name: &str, attrs: u32, eof: u64, alloc: u64, id: u64, ea: u32, next: u32) -> Vec<u8> {
        let units: Vec<u16> = name.encode_utf16().collect();
        let mut r = vec![0u8; 104 + units.len() * 2];
        r[0..4].copy_from_slice(&next.to_le_bytes());
        r[24..32].copy_from_slice(&(133_000_000_000_000_000i64).to_le_bytes());   // a 2022 FILETIME
        r[40..48].copy_from_slice(&eof.to_le_bytes());
        r[48..56].copy_from_slice(&alloc.to_le_bytes());
        r[56..60].copy_from_slice(&attrs.to_le_bytes());
        r[60..64].copy_from_slice(&((units.len() * 2) as u32).to_le_bytes());
        r[64..68].copy_from_slice(&ea.to_le_bytes());
        r[96..104].copy_from_slice(&id.to_le_bytes());
        for (i, u) in units.iter().enumerate() { r[104 + i * 2..106 + i * 2].copy_from_slice(&u.to_le_bytes()); }
        r
    }

    #[test]
    fn parses_records_and_skips_dots() {
        let mut a = record(".", 0x10, 0, 0, 1, 0, 0);
        let pad = (a.len() + 7) & !7; a.resize(pad, 0);
        let first_len = a.len() as u32;
        a[0..4].copy_from_slice(&first_len.to_le_bytes());
        let mut b = record("vm.vhdx", 0x200, 100 << 30, 5 << 30, 42, 0, 0);   // sparse: 100 GB long, 5 GB used
        let pad = (b.len() + 7) & !7; b.resize(pad, 0);
        let second_len = b.len() as u32;
        b[0..4].copy_from_slice(&second_len.to_le_bytes());
        let c = record("link", 0x410, 0, 0, 7, 0xA000_0003, 0);              // a junction
        let buf = [a, b, c].concat();
        let mut out = Vec::new();
        parse(&buf, &mut out);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].name.to_string_lossy(), "vm.vhdx");
        assert_eq!((out[0].size, out[0].alloc, out[0].id, out[0].attrs), (100 << 30, 5 << 30, 42, 0x200));
        assert!(out[0].mtime > 1_600_000_000);
        assert_eq!(out[1].tag, 0xA000_0003);
    }

    #[test]
    fn parse_never_reads_past_the_buffer() {
        let mut r = record("x.bin", 0x20, 5, 8, 3, 0, 0);
        r[60..64].copy_from_slice(&10_000u32.to_le_bytes());   // name length beyond the buffer
        let mut out = Vec::new();
        parse(&r, &mut out);
        parse(&r[..50], &mut out);
        assert!(out.is_empty());
    }

    #[test]
    fn long_paths_get_the_extended_prefix() {
        let long = format!("C:\\{}", "a".repeat(260));
        let w = wide(Path::new(&long));
        assert_eq!(String::from_utf16_lossy(&w[..4]), "\\\\?\\");
        let unc = format!("\\\\server\\share\\{}", "b".repeat(260));
        let w = wide(Path::new(&unc));
        assert_eq!(String::from_utf16_lossy(&w[..8]), "\\\\?\\UNC\\");
        assert_eq!(String::from_utf16_lossy(&wide(Path::new("C:\\x"))), "C:\\x\u{0}");
    }

    #[test]
    fn lists_a_real_folder() {
        let d = std::env::temp_dir().join(format!("hoard-walk-test-{}", std::process::id()));
        std::fs::create_dir_all(d.join("sub")).unwrap();
        std::fs::write(d.join("a.txt"), b"hello").unwrap();
        let mut names: Vec<String> = list(&d).unwrap().into_iter().map(|e| e.name.to_string_lossy().into_owned()).collect();
        names.sort();
        assert_eq!(names, ["a.txt", "sub"]);
        std::fs::remove_dir_all(&d).unwrap();
    }
}
