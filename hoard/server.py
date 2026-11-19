"""Hoard - a local disk-space map (SpaceMonger-style) for Windows, macOS and Linux.

Runs a tiny HTTP server on 127.0.0.1 that scans volumes and serves the UI; the UI opens in a native
window (pywebview) when available, otherwise in a Chromium-style app window or the default browser.
`--scan [ids…]` runs a headless scan (used by the daily schedule) and exits.
Standard library only; OS-specific code lives in plat/.
"""
import gzip
import heapq
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # find plat/ however we're launched
import plat  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)   # inside a PyInstaller bundle, data files live here
DATA_DIR = plat.DATA_DIR
SCANS_DIR = os.path.join(DATA_DIR, "scans")

KEEP_FILE = 1 << 20      # files >= 1 MB are kept individually; smaller ones are aggregated
MAX_FILES = 300          # max individual files kept per folder
CHANGE_MIN = 50 << 20    # changes smaller than 50 MB are not listed

sys.setrecursionlimit(20000)
threading.stack_size(128 * 1024 * 1024)

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
    __slots__ = ("name", "size", "nfiles", "dirs", "files", "cats", "osize", "ocount", "_idx")

    def __init__(self, name):
        self.name = name
        self.size = 0
        self.nfiles = 0
        self.dirs = []
        self.files = []          # (name, size, cat)
        self.cats = [0] * 9
        self.osize = 0           # bytes in small files not kept individually
        self.ocount = 0
        self._idx = None

    def child(self, name):
        if self._idx is None:
            self._idx = {d.name: d for d in self.dirs}
        return self._idx.get(name)

    def file(self, name):
        for f in self.files:
            if f[0] == name:
                return f
        return None


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
                    nc = node.cats
                    for i, v in enumerate(c.cats):
                        if v:
                            nc[i] += v
                elif e.is_file(follow_symlinks=False):
                    sz = size_of(e.stat(follow_symlinks=False))
                    c = cat_of(e.name)
                    node.size += sz
                    node.nfiles += 1
                    node.cats[c] += sz
                    st.files += 1
                    st.bytes += sz
                    if sz >= KEEP_FILE:
                        kept.append((sz, e.name, c))
                    else:
                        node.osize += sz
                        node.ocount += 1
            except OSError:
                st.errors += 1
    if len(kept) > MAX_FILES:
        kept.sort(reverse=True)
        for f in kept[MAX_FILES:]:
            node.osize += f[0]
            node.ocount += 1
        kept = kept[:MAX_FILES]
    node.files = [(n, s, c) for s, n, c in kept]
    st._tick += 1
    if st._tick & 63 == 0:
        st.current = path
    return node


def scan_volume(v, st):
    st.exclude = {os.path.normpath(p) for p in plat.scan_excludes(v["root"])}
    return scan_dir(v["root"], v["name"], st)


# ---------------------------------------------------------------- snapshots (scan history)
# scans/<volume id>/<unix time>.json.gz  —  line 1: meta JSON, line 2: the tree
def _enc(n):
    return [n.name, n.size, n.nfiles, n.cats, n.osize, n.ocount,
            [list(f) for f in n.files], [_enc(d) for d in n.dirs]]


def _dec(a):
    n = Node(a[0])
    n.size, n.nfiles, n.cats, n.osize, n.ocount = a[1], a[2], a[3], a[4], a[5]
    n.files = [tuple(f) for f in a[6]]
    n.dirs = [_dec(d) for d in a[7]]
    return n


def vol_dir(vid):
    return os.path.join(SCANS_DIR, vid)


def snap_path(vid, ts):
    return os.path.join(vol_dir(vid), f"{int(ts)}.json.gz")


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


def save_snapshot(vid, tree, when, partial=False):
    os.makedirs(vol_dir(vid), exist_ok=True)
    p = snap_path(vid, when)
    tmp = p + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=1) as f:
        f.write(json.dumps({"v": 2, "time": int(when), "total": tree.size, "files": tree.nfiles,
                            "partial": partial}) + "\n")
        json.dump(_enc(tree), f, separators=(",", ":"))
    os.replace(tmp, p)


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


def prune(vid, keep):
    for ts in list_snaps(vid)[max(2, keep):]:
        try:
            os.remove(snap_path(vid, ts))
        except OSError:
            pass


def migrate_v1():
    """Move scans saved by the first version (scan_<id>.latest/prev.json.gz) into scans/<id>/."""
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
            with open(os.path.join(DATA_DIR, "error.log"), "a", encoding="utf-8") as log:
                log.write(f"{time.ctime()} migrating {n}:\n{traceback.format_exc()}\n")


def storage_bytes():
    total = 0
    for root, _dirs, files in os.walk(SCANS_DIR):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ---------------------------------------------------------------- settings & report
DEFAULTS = {"schedule": {"enabled": False, "time": "07:00", "volumes": []},
            "keep": 30, "notifyGB": 1.0, "theme": "system"}


def load_settings():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        with open(os.path.join(DATA_DIR, "settings.json"), encoding="utf-8") as f:
            d = json.load(f)
        s.update({k: v for k, v in d.items() if k in s and k != "schedule"})
        s["schedule"].update({k: v for k, v in d.get("schedule", {}).items() if k in s["schedule"]})
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    p = os.path.join(DATA_DIR, "settings.json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(p + ".tmp", p)


def read_report():
    try:
        with open(os.path.join(DATA_DIR, "report.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def append_report(entry):
    r = (read_report() + [entry])[-100:]
    p = os.path.join(DATA_DIR, "report.json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(r, f)
    os.replace(p + ".tmp", p)


# ---------------------------------------------------------------- volumes & state
LOCK = threading.RLock()
STATE = {}          # vol id -> see drive_state()
JOB = {"scan": None}
VOLS = {}           # vol id -> volume dict from plat.list_volumes()


def volumes():
    vs = plat.list_volumes()
    extra = os.environ.get("HOARD_SCAN_DIR")   # map one folder as its own "disk" (used by the smoke test)
    if extra and os.path.isdir(extra):
        extra = os.path.abspath(extra)
        u = shutil.disk_usage(extra)
        vs.append({"id": "dir-" + plat.slug(extra), "root": extra, "name": os.path.basename(extra) or extra,
                   "label": "Folder", "type": "folder", "total": u.total, "used": u.total - u.free, "free": u.free})
    with LOCK:
        VOLS.clear()
        VOLS.update({v["id"]: v for v in vs})
    for v in vs:
        snaps = list_snaps(v["id"])
        v["scanned"] = snaps[0] if snaps else None
        v["scans"] = len(snaps)
    return vs


def vol(vid):
    if vid not in VOLS:
        volumes()
    v = VOLS.get(vid)
    if v is None:
        raise ValueError("That disk isn't connected anymore.")
    return v


def full_path(vid, parts):
    return os.path.join(vol(vid)["root"], *parts)


def _tree(s, vid, ts):
    """Load a snapshot through a small per-volume cache (current + baseline + one spare)."""
    if ts in s["trees"]:
        return s["trees"][ts]
    tree, meta = load_snapshot(vid, ts)
    if tree is None:
        return None, False
    s["trees"][ts] = (tree, bool(meta.get("partial")))
    while len(s["trees"]) > 3:
        drop = min(t for t in s["trees"] if t not in (s["cur_ts"], ts))
        del s["trees"][drop]
    return s["trees"][ts]


def drive_state(vid):
    """Current scan = newest snapshot; baseline = the one the user picked, else the scan before it.
    Picks up snapshots written by the background (scheduled) scan automatically."""
    with LOCK:
        s = STATE.get(vid)
        if s is None:
            s = STATE[vid] = {"cur": None, "cur_ts": None, "cur_partial": False, "prev": None, "prev_ts": None,
                              "prev_partial": False, "base_pick": None, "changes": None, "trees": {}}
        snaps = list_snaps(vid)
        newest = snaps[0] if snaps else None
        if newest != s["cur_ts"]:
            s["cur_ts"] = newest
            s["cur"], s["cur_partial"] = _tree(s, vid, newest) if newest else (None, False)
            s["changes"] = None
        want = s["base_pick"] if s["base_pick"] in snaps[1:] else (snaps[1] if len(snaps) > 1 else None)
        if want != s["prev_ts"]:
            s["prev_ts"] = want
            s["prev"], s["prev_partial"] = _tree(s, vid, want) if want else (None, False)
            s["changes"] = None
        return s


def run_scan(vid):
    st = JOB["scan"]
    try:
        v = vol(vid)
        tree = scan_volume(v, st)
        if st.cancel:
            return
        st.current = "Saving…"
        now = new_ts(vid)
        with LOCK:
            s = drive_state(vid)
            s["trees"][now] = (tree, False)   # so the state switch below doesn't re-read it from disk
        save_snapshot(vid, tree, now)
        prune(vid, load_settings()["keep"])
        with LOCK:
            s["base_pick"] = None              # compare the new scan with the one before it
            drive_state(vid)
    except Exception as ex:  # noqa: BLE001
        st.error = str(ex)
    finally:
        st.running = False


def headless_scan(ids=None):
    """Scan the given volumes (default: the ones chosen in Settings), save, prune, report, notify."""
    migrate_v1()
    settings = load_settings()
    vols = {v["id"]: v for v in volumes()}
    ids = ids or settings["schedule"]["volumes"] or [next(iter(vols))] if vols else []
    lock = os.path.join(DATA_DIR, "scan.lock")
    try:
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        if time.time() - os.path.getmtime(lock) < 6 * 3600:
            return 0          # another scheduled scan is still running
        os.remove(lock)
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    try:
        for vid in ids:
            v = vols.get(vid)
            if v is None:
                continue
            t0 = time.time()
            st = Scan(vid, 0)
            tree = scan_volume(v, st)
            now = new_ts(vid)
            before = list_snaps(vid)
            save_snapshot(vid, tree, now)
            prune(vid, settings["keep"])
            entry = {"vol": vid, "name": v["name"], "time": now, "total": tree.size, "files": tree.nfiles,
                     "errors": st.errors, "secs": round(time.time() - t0), "auto": True}
            if before:
                prev, meta = load_snapshot(vid, before[0])
                if prev is not None:
                    ch = all_changes({"cur": tree, "prev": prev, "prev_partial": bool(meta.get("partial")),
                                      "changes": None})
                    entry.update(since=before[0], delta=tree.size - prev.size,
                                 top=[{"p": list(pp), "d": d} for d, pp, *_ in ch[:5]])
            append_report(entry)
            limit = float(settings.get("notifyGB") or 0) * (1 << 30)
            if limit and entry.get("delta", 0) >= limit:
                top = entry.get("top") or []
                body = f"Biggest change: {top[0]['p'][-1]} +{human(top[0]['d'])}" if top else "Open Hoard to see what grew."
                plat.notify(f"{v['name']} grew {human(entry['delta'])} since the last scan", body)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass
    return 0


# ---------------------------------------------------------------- views
NEW = object()   # marker: the item did not exist in the previous scan


def prev_child(pn, name, partial):
    if pn is None:
        return None
    if pn is NEW:
        return NEW
    c = pn.child(name)
    if c is not None:
        return c
    return None if partial else NEW


def prev_file(pn, name, partial):
    if pn is None:
        return None
    if pn is NEW:
        return 0
    f = pn.file(name)
    if f is not None:
        return f[1]
    return None if partial else 0


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


def walk_prev(s, parts):
    pn = s["prev"]
    for p in parts:
        pn = prev_child(pn, p, s["prev_partial"])
    return pn


def view_json(node, pn, partial, depth, minb):
    out = {"n": node.name, "s": node.size, "k": "d", "f": node.nfiles, "c": dominant(node.cats)}
    if pn is not None:
        out["d"] = node.size - (0 if pn is NEW else pn.size)
    if depth <= 0:
        return out
    items = []
    small_s, small_n = node.osize, node.ocount
    for d in node.dirs:
        if d.size >= minb and d.size > 0:
            items.append(view_json(d, prev_child(pn, d.name, partial), partial, depth - 1, minb))
        else:
            small_s += d.size
            small_n += d.nfiles
    for n, sz, c in node.files:
        if sz >= minb:
            it = {"n": n, "s": sz, "k": "f", "c": c}
            ps = prev_file(pn, n, partial)
            if ps is not None:
                it["d"] = sz - ps
            items.append(it)
        else:
            small_s += sz
            small_n += 1
    items.sort(key=lambda x: -x["s"])
    if len(items) > 160:
        for it in items[160:]:
            small_s += it["s"]
            small_n += it.get("f", 1)
        items = items[:160]
    if small_s > 0:
        items.append({"n": f"{small_n:,} smaller item{'s' if small_n != 1 else ''}", "s": small_s, "k": "o", "c": OTHER})
    out["ch"] = items
    return out


def top_files(node, parts, s, limit=100):
    def gen(n, pp):
        for f in n.files:
            yield f[1], pp + (f[0],), f[2]
        for d in n.dirs:
            yield from gen(d, pp + (d.name,))
    best = heapq.nlargest(limit, gen(node, tuple(parts)), key=lambda x: x[0])
    out = []
    for sz, pp, c in best:
        it = {"p": list(pp), "s": sz, "c": c, "k": "f"}
        if s["prev"] is not None:
            pn = walk_prev(s, pp[:-1])
            ps = prev_file(pn, pp[-1], s["prev_partial"])
            if ps is not None:
                it["d"] = sz - ps
        out.append(it)
    return out


def all_changes(s):
    if s["changes"] is not None:
        return s["changes"]
    out = []
    partial = s["prev_partial"]

    def rec(n, pn, pp):
        if pn is None:
            return None
        d = n.size - (0 if pn is NEW else pn.size)
        biggest = 0
        for c in n.dirs:
            cd = rec(c, prev_child(pn, c.name, partial), pp + (c.name,))
            if cd is not None and cd > biggest:
                biggest = cd
        for fn, sz, cat in n.files:
            ps = prev_file(pn, fn, partial)
            if ps is None:
                continue
            fd = sz - ps
            if fd > biggest:
                biggest = fd
            if fd >= CHANGE_MIN:
                out.append((fd, pp + (fn,), "f", sz, cat))
        # list a folder only when its growth isn't mostly explained by one child
        if d >= CHANGE_MIN and biggest < 0.6 * d and pp:
            out.append((d, pp, "d", n.size, dominant(n.cats)))
        return d

    if s["prev"] is not None and s["cur"] is not None:
        rec(s["cur"], s["prev"], ())
    out.sort(key=lambda x: -x[0])
    s["changes"] = out
    return out


def remove_from_tree(s, parts):
    tree = s["cur"]
    chain = [tree]
    n = tree
    for p in parts[:-1]:
        n = n.child(p)
        if n is None:
            return
        chain.append(n)
    last = parts[-1]
    d = n.child(last)
    if d is not None:
        size, nf, cats = d.size, d.nfiles, d.cats
        n.dirs.remove(d)
        n._idx = None
    else:
        f = n.file(last)
        if f is None:
            return
        size, nf = f[1], 1
        cats = [0] * 9
        cats[f[2]] = f[1]
        n.files.remove(f)
    for a in chain:
        a.size -= size
        a.nfiles -= nf
        for i, v in enumerate(cats):
            a.cats[i] -= v
    s["changes"] = None


# ---------------------------------------------------------------- http
TOKEN = secrets.token_urlsafe(24)
LAST_SEEN = {"t": time.time(), "bye": None}


class Handler(BaseHTTPRequestHandler):
    server_version = "Hoard/2"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        h = (self.headers.get("Host") or "").split(":")[0]
        return h in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if self.path in ("/", "/index.html") or self.path.startswith("/?"):
            with open(os.path.join(RES_DIR, "ui.html"), "rb") as f:
                html = f.read().replace(b"__TOKEN__", TOKEN.encode())
            return self._send(200, html, "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        name = self.path.split("?")[0].removeprefix("/api/")
        if name == "bye":
            LAST_SEEN["bye"] = time.time()
            return self._send(200, {})
        if self.headers.get("X-Token") != TOKEN:
            return self._send(403, {"error": "bad token"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        LAST_SEEN["t"] = time.time()
        LAST_SEEN["bye"] = None
        fn = getattr(self, "api_" + name, None)
        if fn is None:
            return self._send(404, {"error": "unknown"})
        try:
            self._send(200, fn(body))
        except Exception as ex:  # noqa: BLE001
            self._send(500, {"error": str(ex)})

    # --- endpoints
    def api_ping(self, b):
        return {}

    def api_drives(self, b):
        return {"drives": volumes(), "os": plat.NAME, "sep": os.sep,
                "trashName": plat.TRASH_NAME, "revealName": plat.REVEAL_NAME,
                "theme": load_settings()["theme"]}

    def api_open_drive(self, b):
        s = drive_state(b["drive"])
        return {"time": s["cur_ts"], "prevTime": s["prev_ts"], "partial": s["cur_partial"],
                "has": s["cur"] is not None}

    def api_scan(self, b):
        vid = b["drive"]
        j = JOB["scan"]
        if j and j.running:
            return {"error": "A scan is already running."}
        v = vol(vid)
        try:
            u = shutil.disk_usage(v["root"])
            used = u.total - u.free
        except OSError:
            used = 0
        JOB["scan"] = Scan(vid, used)
        threading.Thread(target=run_scan, args=(vid,), daemon=True).start()
        return {"ok": True}

    def api_cancel(self, b):
        j = JOB["scan"]
        if j:
            j.cancel = True
        return {}

    def api_progress(self, b):
        j = JOB["scan"]
        if not j:
            return {"running": False}
        return {"running": j.running, "drive": j.drive, "files": j.files, "bytes": j.bytes, "used": j.used,
                "errors": j.errors, "current": j.current, "elapsed": time.time() - j.started,
                "cancelled": j.cancel, "error": j.error}

    def api_view(self, b):
        s = drive_state(b["drive"])
        if s["cur"] is None:
            return {"none": True}
        parts = b.get("path") or []
        with LOCK:
            node, f = walk(s["cur"], parts)
            if node is None or f is not None:
                return {"missing": True}
            pn = walk_prev(s, parts)
            v = view_json(node, pn, s["prev_partial"], int(b.get("depth", 8)), max(1, node.size * 0.0004))
            v["cats"] = list(node.cats)
        return {"view": v, "path": parts, "time": s["cur_ts"], "prevTime": s["prev_ts"],
                "partial": s["cur_partial"]}

    def api_files(self, b):
        s = drive_state(b["drive"])
        if s["cur"] is None:
            return {"items": []}
        node, f = walk(s["cur"], b.get("path") or [])
        if node is None:
            return {"items": []}
        with LOCK:
            return {"items": top_files(node, b.get("path") or [], s)}

    def api_changes(self, b):
        s = drive_state(b["drive"])
        if s["prev"] is None:
            return {"items": [], "noPrev": True}
        parts = tuple(b.get("path") or [])
        with LOCK:
            ch = all_changes(s)
        k = len(parts)
        items = [{"p": list(pp), "d": d, "k": kind, "s": sz, "c": c}
                 for d, pp, kind, sz, c in ch if pp[:k] == parts and len(pp) > k][:100]
        return {"items": items, "prevTime": s["prev_ts"]}

    def api_history(self, b):
        vid = b["drive"]
        out = []
        for ts in list_snaps(vid):
            m = snap_meta(vid, ts) or {}
            out.append({"ts": ts, "total": m.get("total"), "files": m.get("files"), "partial": m.get("partial", False)})
        s = drive_state(vid)
        return {"items": out, "cur": s["cur_ts"], "base": s["prev_ts"]}

    def api_set_baseline(self, b):
        vid = b["drive"]
        with LOCK:
            s = drive_state(vid)
            s["base_pick"] = int(b["ts"]) if b.get("ts") else None
            s = drive_state(vid)
        return {"prevTime": s["prev_ts"]}

    def api_reveal(self, b):
        p = full_path(b["drive"], b.get("path") or [])
        if not os.path.lexists(p):
            return {"error": "That item no longer exists."}
        plat.reveal(p)
        return {"ok": True}

    def api_recycle(self, b):
        vid, parts = b["drive"], b.get("path") or []
        p = full_path(vid, parts)
        why = plat.protected(vol(vid)["root"], parts, p)
        if why:
            return {"error": why}
        if not os.path.lexists(p):
            return {"error": "That item no longer exists."}
        ok, err = plat.trash(p)
        if not ok:
            return {"error": err}
        s = drive_state(vid)
        with LOCK:
            remove_from_tree(s, parts)
            tree, when, partial = s["cur"], s["cur_ts"], s["cur_partial"]
        threading.Thread(target=save_snapshot, args=(vid, tree, when, partial), daemon=True).start()
        return {"ok": True}

    def api_settings(self, b):
        return {"settings": load_settings(), "schedule": plat.schedule_status(),
                "scheduleNote": plat.SCHEDULE_NOTE, "storage": storage_bytes(),
                "report": read_report()[-12:][::-1]}

    def api_save_settings(self, b):
        cur = load_settings()
        new = b.get("settings") or {}
        sch = new.get("schedule") or {}
        t = str(sch.get("time", cur["schedule"]["time"]))
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            return {"error": "Pick a time like 07:00."}
        cur["schedule"] = {"enabled": bool(sch.get("enabled")), "time": t,
                           "volumes": [str(x) for x in sch.get("volumes", [])][:26]}
        cur["keep"] = max(2, min(365, int(new.get("keep", cur["keep"]))))
        cur["notifyGB"] = max(0.0, float(new.get("notifyGB", cur["notifyGB"])))
        cur["theme"] = new.get("theme") if new.get("theme") in ("system", "light", "dark") else cur["theme"]
        if cur["schedule"]["enabled"] and not cur["schedule"]["volumes"]:
            return {"error": "Choose at least one disk to scan."}
        save_settings(cur)
        err = None
        h, m = (int(x) for x in t.split(":"))
        if cur["schedule"]["enabled"]:
            ok, err = plat.schedule_install(h, m, plat.self_command() + ["--scan"])
        else:
            ok, err = plat.schedule_remove()
        for vid in os.listdir(SCANS_DIR) if os.path.isdir(SCANS_DIR) else []:
            prune(vid, cur["keep"])
        return {"ok": ok, "error": err, "settings": cur, "schedule": plat.schedule_status()}


def watchdog():
    while True:
        time.sleep(2)
        now = time.time()
        bye = LAST_SEEN["bye"]
        if (bye and now - bye > 5) or now - LAST_SEEN["t"] > 300:
            os._exit(0)


def main(open_window=True, port=0):
    migrate_v1()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    if sys.stdout:
        print(url, flush=True)
    if not open_window:
        srv.serve_forever()
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if plat.open_ui(url):      # native window: it has closed, so we're done
        os._exit(0)
    watchdog()                 # browser window: exit once the page says goodbye or goes silent


def cli(argv):
    if "--scan" in argv:
        i = argv.index("--scan")
        ids = [a for a in argv[i + 1:] if not a.startswith("--")]
        sys.exit(headless_scan(ids))
    main(open_window="--no-window" not in argv,
         port=int(argv[argv.index("--port") + 1]) if "--port" in argv else 0)


if __name__ == "__main__":
    cli(sys.argv)
