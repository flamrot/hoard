//! Shared helpers for Hoard's native scanners: extension categories, JSON output, progress files.

use std::collections::HashMap;

pub const KEEP_FILE: u64 = 1 << 20; // files >= 1 MB are listed individually
pub const MAX_FILES: usize = 300; // per folder
pub const OTHER: u8 = 8;

/// ext -> category, from the tab-separated file Hoard writes (so categories live in one place: core.py).
pub fn load_extmap(path: &str) -> Result<HashMap<String, u8>, String> {
    let mut m = HashMap::new();
    for line in std::fs::read_to_string(path).map_err(|e| format!("extmap: {e}"))?.lines() {
        if let Some((e, c)) = line.split_once('\t') {
            m.insert(e.to_string(), c.parse().unwrap_or(OTHER));
        }
    }
    Ok(m)
}

pub fn cat_of(extmap: &HashMap<String, u8>, name: &str) -> u8 {
    match name.rfind('.') {
        Some(i) if i > 0 && i + 1 < name.len() => *extmap.get(&name[i + 1..].to_lowercase()).unwrap_or(&OTHER),
        _ => OTHER,
    }
}

pub fn json_str(out: &mut Vec<u8>, s: &str) {
    out.push(b'"');
    for ch in s.chars() {
        match ch {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            c if (c as u32) < 0x20 => out.extend_from_slice(format!("\\u{:04x}", c as u32).as_bytes()),
            c => {
                let mut b = [0u8; 4];
                out.extend_from_slice(c.encode_utf8(&mut b).as_bytes());
            }
        }
    }
    out.push(b'"');
}

pub fn progress(path: &str, phase: &str, done: u64, total: u64, bytes: u64) {
    let _ = std::fs::write(path, format!("{}\t{}\t{}\t{}\n", phase.replace(['\t', '\n'], " "), done, total, bytes));
}

/// Totals for one folder, before and after its subfolders are folded in.
#[derive(Default, Clone)]
pub struct Agg {
    pub size: u64,
    pub nfiles: u64,
    pub cats: [u64; 9],
    pub osize: u64,
    pub ocount: u64,
    pub msum: f64,
    pub mtime: i64,
    pub own: i64,
    pub cloud: u64,
}

impl Agg {
    pub fn add_file(&mut self, size: u64, cloud: u64, mtime: i64, cat: u8, kept: bool) {
        self.size += size;
        self.nfiles += 1;
        self.cats[cat as usize] += size;
        self.cloud += cloud;
        self.msum += size as f64 * mtime as f64;
        self.own = self.own.max(mtime);
        if !kept {
            self.osize += size;
            self.ocount += 1;
        }
    }
    pub fn add_child(&mut self, c: &Agg) {
        self.size += c.size;
        self.nfiles += c.nfiles;
        self.msum += c.msum;
        self.cloud += c.cloud;
        self.mtime = self.mtime.max(c.mtime);
        for i in 0..9 {
            self.cats[i] += c.cats[i];
        }
    }
    /// Opening part of the node array, up to (and including) the "[" of the child-folder list.
    pub fn write_head(&self, out: &mut Vec<u8>, name: &str, files: &[(String, u64, u8, i64)]) {
        use std::io::Write;
        out.push(b'[');
        json_str(out, name);
        let cats: Vec<String> = self.cats.iter().map(|x| x.to_string()).collect();
        write!(out, ",{},{},[{}],{},{},[", self.size, self.nfiles, cats.join(","), self.osize, self.ocount).unwrap();
        for (i, (n, s, c, m)) in files.iter().enumerate() {
            if i > 0 {
                out.push(b',');
            }
            out.push(b'[');
            json_str(out, n);
            write!(out, ",{},{},{}]", s, c, m).unwrap();
        }
        out.extend_from_slice(b"],[");
    }
    pub fn write_tail(&self, out: &mut Vec<u8>) {
        use std::io::Write;
        let mtime = self.mtime.max(self.own);
        write!(out, "],{},{:.0},{},{}]", mtime, self.msum, self.own, self.cloud).unwrap();
    }
}
