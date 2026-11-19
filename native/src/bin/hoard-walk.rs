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

fn process(sh: &Shared, task: Task, extmap: &HashMap<String, u8>, exclude: &HashSet<PathBuf>) {
    let mut agg = Agg::default();
    let mut kept: Vec<(String, u64, u8, i64)> = Vec::new();
    let mut children = Vec::new();
    match fs::read_dir(&task.path) {
        Err(_) => { sh.errors.fetch_add(1, Ordering::Relaxed); }
        Ok(rd) => {
            for entry in rd {
                let Ok(e) = entry else { sh.errors.fetch_add(1, Ordering::Relaxed); continue; };
                let Ok(ft) = e.file_type() else { sh.errors.fetch_add(1, Ordering::Relaxed); continue; };
                if ft.is_symlink() { continue; } // includes Windows junctions and mount points
                let name = e.file_name().to_string_lossy().into_owned();
                if ft.is_dir() {
                    let path = e.path();
                    if exclude.contains(&path) { continue; }
                    let id = sh.next_id.fetch_add(1, Ordering::Relaxed);
                    children.push(id);
                    {
                        let mut r = sh.results.lock().unwrap();
                        if r.len() <= id { r.resize_with(id + 1024, || None); }
                        r[id] = Some(DirRec { name, children: vec![], agg: Agg::default(), kept: vec![] });
                    }
                    sh.pending.fetch_add(1, Ordering::SeqCst);
                    sh.queue.lock().unwrap().push_back(Task { id, path });
                    sh.cv.notify_one();
                } else if ft.is_file() {
                    let Ok(md) = e.metadata() else { sh.errors.fetch_add(1, Ordering::Relaxed); continue; };
                    let (sz, cl) = sizes(&md);
                    let mt = mtime_of(&md);
                    let cat = cat_of(extmap, &name);
                    let keep = sz >= KEEP_FILE;
                    agg.add_file(sz, cl, mt, cat, keep);
                    if keep { kept.push((name, sz, cat, mt)); }
                    sh.files.fetch_add(1, Ordering::Relaxed);
                    sh.bytes.fetch_add(sz, Ordering::Relaxed);
                }
            }
        }
    }
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

    // write the tree, iteratively so deep folders can't overflow a stack
    let mut out: Vec<u8> = Vec::with_capacity(64 << 20);
    enum Step { Open(usize), Close(usize), Comma }
    let mut todo = vec![Step::Open(0)];
    while let Some(step) = todo.pop() {
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
    let mut f = fs::File::create(&out_path).map_err(|e| format!("write output: {e}"))?;
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
