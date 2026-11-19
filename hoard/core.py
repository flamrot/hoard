"""Scanning, the in-memory tree, saved snapshots, settings and the scan report log."""
import gzip
import json
import os
import time

import plat

DATA_DIR = plat.DATA_DIR
SCANS_DIR = os.path.join(DATA_DIR, "scans")

KEEP_FILE = 1 << 20      # files >= 1 MB are kept individually; smaller ones are aggregated
MAX_FILES = 300          # max individual files kept per folder
INDEX_MIN = 5 << 20      # folders >= 5 MB go into each snapshot's size index (growth charts)

# ---------------------------------------------------------------- categories
CATS = ["video", "games", "apps", "archives", "media", "code", "docs", "models", "other"]
OTHER = 8
_EXT = {
    0: "mp4 mkv mov avi wmv webm m4v flv mpg mpeg ts m2ts vob 3gp",
    1: "pak ucas utoc vpk bsa ba2 gcf ncf upk uasset umap bnk wem forge rpf big pck assets resource sav",
    2: "exe dll sys msi msix appx cab mui drv ocx cpl efi lib so pdb node dylib deb rpm pkg appimage snap flatpak",
    3: "zip 7z rar tar gz tgz bz2 xz iso img vhd vhdx wim esd zst dmg qcow2 vmdk vdi raw",
    4: "jpg jpeg png gif bmp webp tif tiff psd cr2 nef arw dng heic svg ico mp3 wav flac ogg m4a aac wma opus aiff",
    5: "js mjs cjs ts tsx jsx py pyc pyd json map css scss html c h cpp hpp cs java class jar go rs rb php lua sh ps1 bat yml yaml toml lock md wasm o obj idx pack whl rlib",
    6: "pdf doc docx xls xlsx ppt pptx txt rtf odt ods csv epub log pages numbers key",
    7: "safetensors ckpt gguf ggml pt pth onnx bin h5 tflite pb npz npy mlmodel",
}
EXT = {e: c for c, s in _EXT.items() for e in s.split()}


def cat_of(name):
    i = name.rfind(".")
    return EXT.get(name[i + 1:].lower(), OTHER) if i > 0 else OTHER


def ext_of(name):
    i = name.rfind(".")
    return name[i + 1:].lower() if 0 < i < len(name) - 1 else ""


def dominant(cats):
    best = OTHER
    for i, v in enumerate(cats):
        if v > cats[best]:
            best = i
    return best


def human(b):
    a = abs(b)
    for unit, n in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20)):
        if a >= n:
            return f"{b / n:.1f} {unit}"
    return f"{b / 1024:.0f} KB"


# ---------------------------------------------------------------- tree
class Node:
    __slots__ = ("name", "size", "nfiles", "dirs", "files", "cats", "osize", "ocount",
                 "mtime", "msum", "own", "_idx")

    def __init__(self, name):
        self.name = name
        self.size = 0
        self.nfiles = 0
        self.dirs = []
        self.files = []          # (name, size, cat, mtime)
        self.cats = [0] * 9
        self.osize = 0           # bytes in small files not kept individually
        self.ocount = 0
        self.mtime = 0           # newest modification time anywhere inside
        self.msum = 0            # sum(size * mtime): size-weighted age of the bytes inside
        self.own = 0             # newest modification time of files directly in this folder
        self._idx = None

    def child(self, name):
        if self._idx is None:
            self._idx = {d.name: d for d in self.dirs}
        c = self._idx.get(name)
        if c is None and plat.NAME != "linux":       # Windows/macOS paths are case-insensitive
            low = name.casefold()
            c = next((d for d in self.dirs if d.name.casefold() == low), None)
        return c

    def file(self, name):
        for f in self.files:
            if f[0] == name:
                return f
        return None

    def age(self):
        """Typical modification time of the bytes in here (0 = unknown)."""
        return self.msum // self.size if self.size and self.msum else self.mtime


class Scan:
    def __init__(self, vol, used):
        self.drive = vol
        self.used = used
        self.files = 0
        self.bytes = 0
        self.errors = 0
        self.current = ""
        self.started = time.time()
        self.cancel = False
        self.running = True
        self.error = None
        self.exclude = set()
        self._tick = 0


def scan_dir(path, name, st):
    node = Node(name)
    kept = []
    size_of = plat.size_of
    exclude = st.exclude
    try:
        it = os.scandir(path)
    except OSError:
        st.errors += 1
        return node
    with it:
        for e in it:
            if st.cancel:
                break
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.is_junction() or e.is_symlink() or (exclude and e.path in exclude):
                        continue
                    c = scan_dir(e.path, e.name, st)
                    node.dirs.append(c)
                    node.size += c.size
                    node.nfiles += c.nfiles
                    node.msum += c.msum
                    if c.mtime > node.mtime:
                        node.mtime = c.mtime
                    nc = node.cats
                    for i, v in enumerate(c.cats):
                        if v:
                            nc[i] += v
                elif e.is_file(follow_symlinks=False):
                    s = e.stat(follow_symlinks=False)
                    sz = size_of(s)
                    mt = int(s.st_mtime)
                    c = cat_of(e.name)
                    node.size += sz
                    node.nfiles += 1
                    node.cats[c] += sz
                    node.msum += sz * mt
                    if mt > node.own:
                        node.own = mt
                    st.files += 1
                    st.bytes += sz
                    if sz >= KEEP_FILE:
                        kept.append((sz, e.name, c, mt))
                    else:
                        node.osize += sz
                        node.ocount += 1
            except OSError:
                st.errors += 1
    if node.own > node.mtime:
        node.mtime = node.own
    if len(kept) > MAX_FILES:
        kept.sort(reverse=True)
        for f in kept[MAX_FILES:]:
            node.osize += f[0]
            node.ocount += 1
        kept = kept[:MAX_FILES]
    node.files = [(n, s, c, m) for s, n, c, m in kept]
    st._tick += 1
    if st._tick & 63 == 0:
        st.current = path
    return node


def scan_volume(v, st):
    st.exclude = {os.path.normpath(p) for p in plat.scan_excludes(v["root"])}
    return scan_dir(v["root"], v["name"], st)


def walk(node, parts):
    """Return (node, file_tuple_or_None). parts may end at a file."""
    n = node
    for i, p in enumerate(parts):
        c = n.child(p)
        if c is None:
            if i == len(parts) - 1:
                f = n.file(p)
                if f:
                    return n, f
            return None, None
        n = c
    return n, None


def rel_parts(root, path):
    """Split an absolute path into parts relative to a volume root, or None if it's elsewhere."""
    try:
        rel = os.path.relpath(os.path.abspath(path), root)
    except ValueError:      # different drive on Windows
        return None
    if rel == ".":
        return []
    if rel.startswith(".."):
        return None
    return [p for p in rel.split(os.sep) if p]


# ---------------------------------------------------------------- snapshots (scan history)
# scans/<volume id>/<unix time>.json.gz      line 1: meta JSON, line 2: the tree
# scans/<volume id>/<unix time>.idx.json.gz  folder path -> size, for growth charts
def _enc(n):
    return [n.name, n.size, n.nfiles, n.cats, n.osize, n.ocount,
            [list(f) for f in n.files], [_enc(d) for d in n.dirs], n.mtime, n.msum, n.own]


def _dec(a):
    n = Node(a[0])
    n.size, n.nfiles, n.cats, n.osize, n.ocount = a[1], a[2], a[3], a[4], a[5]
    n.files = [tuple(f) if len(f) > 3 else (f[0], f[1], f[2], 0) for f in a[6]]
    n.dirs = [_dec(d) for d in a[7]]
    if len(a) > 8:
        n.mtime, n.msum, n.own = a[8], a[9], a[10]
    return n


def vol_dir(vid):
    return os.path.join(SCANS_DIR, vid)


def snap_path(vid, ts):
    return os.path.join(vol_dir(vid), f"{int(ts)}.json.gz")


def idx_path(vid, ts):
    return os.path.join(vol_dir(vid), f"{int(ts)}.idx.json.gz")


def list_snaps(vid):
    """Snapshot timestamps for a volume, newest first."""
    try:
        names = os.listdir(vol_dir(vid))
    except OSError:
        return []
    return sorted((int(n[:-8]) for n in names if n.endswith(".json.gz") and n[:-8].isdigit()), reverse=True)


def new_ts(vid):
    """Timestamp for a new snapshot: now, but always after the newest one (two scans can finish in the same second)."""
    snaps = list_snaps(vid)
    return max(int(time.time()), (snaps[0] + 1) if snaps else 0)


def _write_gz(path, write):
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=1) as f:
        write(f)
    os.replace(tmp, path)


def save_snapshot(vid, tree, when, partial=False):
    os.makedirs(vol_dir(vid), exist_ok=True)

    def w(f):
        f.write(json.dumps({"v": 3, "time": int(when), "total": tree.size, "files": tree.nfiles,
                            "partial": partial}) + "\n")
        json.dump(_enc(tree), f, separators=(",", ":"))
    _write_gz(snap_path(vid, when), w)
    save_index(vid, when, tree)


def snap_meta(vid, ts):
    try:
        with gzip.open(snap_path(vid, ts), "rt", encoding="utf-8") as f:
            return json.loads(f.readline())
    except Exception:  # noqa: BLE001
        return None


def load_snapshot(vid, ts):
    try:
        with gzip.open(snap_path(vid, ts), "rt", encoding="utf-8") as f:
            meta = json.loads(f.readline())
            tree = _dec(json.loads(f.read()))
        return tree, meta
    except Exception:  # noqa: BLE001
        return None, None


def build_index(tree):
    idx = {"": tree.size}

    def rec(n, prefix):
        for d in n.dirs:
            if d.size >= INDEX_MIN:
                k = prefix + "/" + d.name if prefix else d.name
                idx[k] = d.size
                rec(d, k)
    rec(tree, "")
    return idx


def save_index(vid, ts, tree):
    idx = build_index(tree)
    _write_gz(idx_path(vid, ts), lambda f: json.dump(idx, f, separators=(",", ":")))
    _IDX[(vid, int(ts))] = idx
    return idx


_IDX = {}


def load_index(vid, ts):
    key = (vid, int(ts))
    if key in _IDX:
        return _IDX[key]
    try:
        with gzip.open(idx_path(vid, ts), "rt", encoding="utf-8") as f:
            _IDX[key] = json.load(f)
            return _IDX[key]
    except (OSError, ValueError):
        tree, _ = load_snapshot(vid, ts)     # older snapshot without an index: build it once
        return save_index(vid, ts, tree) if tree is not None else {}


def warm_indexes():
    """Build missing growth-chart indexes for older snapshots in the background."""
    try:
        vids = os.listdir(SCANS_DIR)
    except OSError:
        return
    for vid in vids:
        for ts in list_snaps(vid):
            if not os.path.exists(idx_path(vid, ts)):
                load_index(vid, ts)


def prune(vid, keep):
    for ts in list_snaps(vid)[max(2, keep):]:
        for p in (snap_path(vid, ts), idx_path(vid, ts)):
            try:
                os.remove(p)
            except OSError:
                pass
        _IDX.pop((vid, ts), None)


def migrate_v1():
    """Move scans saved by the first version (scan_<id>.latest/prev.json.gz) into scans/<id>/."""
    import re
    try:
        names = os.listdir(DATA_DIR)
    except OSError:
        return
    for n in names:
        m = re.fullmatch(r"scan_(.+)\.(latest|prev)\.json\.gz", n)
        if not m:
            continue
        p = os.path.join(DATA_DIR, n)
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                d = json.load(f)
            save_snapshot(m.group(1), _dec(d["root"]), d["time"], d.get("partial", False))
            os.remove(p)
        except Exception:  # noqa: BLE001 - leave the old file in place and note why
            import traceback
            log_error(f"migrating {n}", traceback.format_exc())


def storage_bytes():
    total = 0
    for root, _dirs, files in os.walk(SCANS_DIR):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def log_error(what, detail):
    try:
        with open(os.path.join(DATA_DIR, "error.log"), "a", encoding="utf-8") as log:
            log.write(f"{time.ctime()} {what}:\n{detail}\n")
    except OSError:
        pass


# ---------------------------------------------------------------- settings, report log, staging list
DEFAULTS = {"schedule": {"enabled": False, "time": "07:00", "volumes": []},
            "keep": 30, "notifyGB": 1.0, "theme": "system",
            "ignore": {},          # volume id -> ["folder/sub", ...]  hidden from the map and lists
            "watch": [],           # [{"vol", "path": [...], "gb"}]  alert when a folder passes a size
            "reportDir": ""}       # save an HTML report here after each daily scan


def _read_json(name, default):
    try:
        with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(name, data):
    p = os.path.join(DATA_DIR, name)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(p + ".tmp", p)


def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    d = _read_json("settings.json", {})
    s.update({k: v for k, v in d.items() if k in s and k != "schedule"})
    s["schedule"].update({k: v for k, v in d.get("schedule", {}).items() if k in s["schedule"]})
    return s


def save_settings(s):
    _write_json("settings.json", s)


def ignored(settings, vid):
    return {tuple(p.split("/")) for p in settings.get("ignore", {}).get(vid, [])}


def read_report():
    return _read_json("report.json", [])


def append_report(entry):
    _write_json("report.json", (read_report() + [entry])[-100:])


def read_staged():
    return _read_json("staged.json", [])


def write_staged(items):
    _write_json("staged.json", items)
