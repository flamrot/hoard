"""Hoard - a local disk-space map (SpaceMonger-style) for Windows, macOS and Linux.

Runs a tiny HTTP server on 127.0.0.1 that scans volumes and serves the UI; the UI opens in a native
window (pywebview) when available, otherwise in a Chromium-style app window or the default browser.
Standard library only; OS-specific code lives in plat/.
"""
import gzip
import heapq
import json
import os
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


# ---------------------------------------------------------------- persistence
def _enc(n):
    return [n.name, n.size, n.nfiles, n.cats, n.osize, n.ocount,
            [list(f) for f in n.files], [_enc(d) for d in n.dirs]]


def _dec(a):
    n = Node(a[0])
    n.size, n.nfiles, n.cats, n.osize, n.ocount = a[1], a[2], a[3], a[4], a[5]
    n.files = [tuple(f) for f in a[6]]
    n.dirs = [_dec(d) for d in a[7]]
    return n


def cache_path(vol, which):
    return os.path.join(DATA_DIR, f"scan_{vol}.{which}.json.gz")


def save_tree(vol, which, tree, when, partial=False):
    p = cache_path(vol, which)
    tmp = p + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=1) as f:
        json.dump({"v": 1, "time": when, "partial": partial, "root": _enc(tree)}, f, separators=(",", ":"))
    os.replace(tmp, p)


def load_meta_tree(vol, which):
    p = cache_path(vol, which)
    if not os.path.exists(p):
        return None
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            d = json.load(f)
        return {"tree": _dec(d["root"]), "time": d["time"], "partial": d.get("partial", False)}
    except Exception:  # noqa: BLE001
        return None


def cache_time(vol, which):
    """Read just the timestamp without decoding the tree (it's near the start of the file)."""
    p = cache_path(vol, which)
    if not os.path.exists(p):
        return None
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            head = f.read(200)
        i = head.index('"time":') + 7
        j = head.index(",", i)
        return float(head[i:j])
    except Exception:  # noqa: BLE001
        return os.path.getmtime(p)


# ---------------------------------------------------------------- volumes & state
LOCK = threading.RLock()
STATE = {}          # vol id -> {"cur", "cur_time", "cur_partial", "prev", "prev_time", "prev_partial", "changes"}
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
        v["scanned"] = cache_time(v["id"], "latest")
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


def drive_state(vid, load=True):
    with LOCK:
        s = STATE.get(vid)
        if s is None and load:
            s = {"cur": None, "cur_time": None, "cur_partial": False, "prev": None, "prev_time": None,
                 "prev_partial": False, "changes": None}
            c = load_meta_tree(vid, "latest")
            if c:
                s["cur"], s["cur_time"], s["cur_partial"] = c["tree"], c["time"], c["partial"]
            p = load_meta_tree(vid, "prev")
            if p:
                s["prev"], s["prev_time"], s["prev_partial"] = p["tree"], p["time"], p["partial"]
            STATE[vid] = s
        return s


def run_scan(vid):
    st = JOB["scan"]
    try:
        v = vol(vid)
        st.exclude = {os.path.normpath(p) for p in plat.scan_excludes(v["root"])}
        tree = scan_dir(v["root"], v["name"], st)
        if st.cancel:
            return
        now = time.time()
        s = drive_state(vid)
        with LOCK:
            old, old_time, old_partial = s["cur"], s["cur_time"], s["cur_partial"]
            if old is not None:
                s["prev"], s["prev_time"], s["prev_partial"] = old, old_time, old_partial
            s["cur"], s["cur_time"], s["cur_partial"] = tree, now, False
            s["changes"] = None
        st.current = "Saving…"
        if old is not None:
            try:
                os.replace(cache_path(vid, "latest"), cache_path(vid, "prev"))
            except OSError:
                save_tree(vid, "prev", old, old_time, old_partial)
        save_tree(vid, "latest", tree, now)
    except Exception as ex:  # noqa: BLE001
        st.error = str(ex)
    finally:
        st.running = False


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
                "trashName": plat.TRASH_NAME, "revealName": plat.REVEAL_NAME}

    def api_open_drive(self, b):
        s = drive_state(b["drive"])
        return {"time": s["cur_time"], "prevTime": s["prev_time"], "partial": s["cur_partial"],
                "has": s["cur"] is not None}

    def api_scan(self, b):
        vid = b["drive"]
        j = JOB["scan"]
        if j and j.running:
            return {"error": "A scan is already running."}
        v = vol(vid)
        drive_state(vid)  # make sure the previous scan is loaded so it becomes "prev"
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
        return {"view": v, "path": parts, "time": s["cur_time"], "prevTime": s["prev_time"]}

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
        return {"items": items, "prevTime": s["prev_time"]}

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
            tree, when, partial = s["cur"], s["cur_time"], s["cur_partial"]
        threading.Thread(target=save_tree, args=(vid, "latest", tree, when, partial), daemon=True).start()
        return {"ok": True}


def watchdog():
    while True:
        time.sleep(2)
        now = time.time()
        bye = LAST_SEEN["bye"]
        if (bye and now - bye > 5) or now - LAST_SEEN["t"] > 300:
            os._exit(0)


def main(open_window=True, port=0):
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


if __name__ == "__main__":
    main(open_window="--no-window" not in sys.argv,
         port=int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 0)
