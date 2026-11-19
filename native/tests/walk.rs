//! Runs the real hoard-walk binary on a temporary folder tree and checks the output.

use std::fs;
use std::path::PathBuf;
use std::process::Command;

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("hoard-walk-test-{}-{}", name, std::process::id()));
    let _ = fs::remove_dir_all(&d);
    fs::create_dir_all(&d).unwrap();
    d
}

#[test]
fn walks_a_tree_like_the_python_scanner() {
    let root = tmp("tree");
    fs::create_dir_all(root.join("a/b")).unwrap();
    fs::create_dir_all(root.join("skip/inner")).unwrap();
    fs::write(root.join("a/big.mp4"), vec![1u8; 3 << 20]).unwrap();
    fs::write(root.join("a/b/small.txt"), b"hello").unwrap();
    fs::write(root.join("skip/inner/x.bin"), vec![0u8; 1 << 20]).unwrap();
    fs::write(root.join("name with \"quotes\" and \\.txt"), b"q").unwrap_or(()); // illegal on Windows: ignored
    let work = tmp("out");
    let (out, prog, ext) = (work.join("t.json"), work.join("p.txt"), work.join("e.tsv"));
    fs::write(&ext, "mp4\t0\ntxt\t6\n").unwrap();
    let st = Command::new(env!("CARGO_BIN_EXE_hoard-walk"))
        .arg(&root).arg(&out).arg(&prog).arg(&ext)
        .arg("--name").arg("Root").arg("--exclude").arg(root.join("skip"))
        .status().unwrap();
    assert!(st.success());
    let json = fs::read_to_string(&out).unwrap();
    assert!(json.starts_with("[\"Root\","), "root name: {}", &json[..40.min(json.len())]);
    assert!(json.contains("\"big.mp4\""), "big file kept individually");
    assert!(!json.contains("\"skip\""), "excluded folder left out");
    assert!(!json.contains("x.bin"));
    let progress = fs::read_to_string(&prog).unwrap();
    assert!(progress.starts_with("done"), "progress: {progress}");
    // file count: big.mp4 + small.txt (+ the quotes file where the OS allows it)
    let files: u64 = progress.split('\t').nth(1).unwrap().parse().unwrap();
    assert!(files == 2 || files == 3, "files = {files}");
    let _ = fs::remove_dir_all(&root);
    let _ = fs::remove_dir_all(&work);
}

#[test]
fn a_missing_root_still_writes_a_tree() {
    let work = tmp("missing");
    let (out, prog, ext) = (work.join("t.json"), work.join("p.txt"), work.join("e.tsv"));
    fs::write(&ext, "").unwrap();
    let st = Command::new(env!("CARGO_BIN_EXE_hoard-walk"))
        .arg(work.join("does-not-exist")).arg(&out).arg(&prog).arg(&ext).status().unwrap();
    assert!(st.success());
    assert!(fs::read_to_string(&out).unwrap().starts_with('['));
    let _ = fs::remove_dir_all(&work);
}
