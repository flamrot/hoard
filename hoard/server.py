"""Hoard - a local disk-space map (SpaceMonger-style) for Windows, macOS and Linux.

Runs a tiny HTTP server on 127.0.0.1 that scans volumes and serves the UI; the UI opens in a native
window (pywebview) when available, otherwise in a Chromium-style app window or the default browser.
`--scan [ids…]` runs a headless scan (used by the daily schedule) and exits.
Standard library only; OS-specific code lives in plat/.
"""
import fnmatch
import heapq
import json
import mimetypes
import os
import re
import secrets
import shutil
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # find our modules however we're launched
import plat  # noqa: E402
import core  # noqa: E402
import cleanup  # noqa: E402
import dupes  # noqa: E402
import fm  # noqa: E402
import safefs  # noqa: E402
import apps as appsmod  # noqa: E402
import cloud  # noqa: E402
import reports  # noqa: E402
from core import OTHER, Node, dominant, human, list_snaps, walk  # noqa: E402

APP_DIR = os.path.dirname(os.path.abspath(__file__))
RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)   # inside a PyInstaller bundle, data files live here
CHANGE_MIN = 50 << 20    # changes smaller than 50 MB are not listed
DAY = 86400
AGE_BUCKETS = (7, 30, 90, 365)          # days: newer than a week, month, 3 months, a year, older

sys.setrecursionlimit(20000)
threading.stack_size(128 * 1024 * 1024)

# ---------------------------------------------------------------- volumes & state
LOCK = threading.RLock()
STATE = {}          # vol id -> see drive_state()
JOB = {"scan": None, "cleanup": None, "dupes": None}
VOLS = {}           # vol id -> volume dict from plat.list_volumes()


def location_volume(path, name=None):
    """A folder or network share mapped as its own "disk"."""
    path = os.path.abspath(path) if not path.startswith(("\\\\", "//")) else path
    try:
        u = shutil.disk_usage(path)
        total, free = u.total, u.free
    except OSError:
        total = free = 0
    net = path.startswith(("\\\\", "//"))
    return {"id": plat.vol_id("dir", os.path.normcase(path)), "legacy": plat.legacy_vol_id("dir", path),
            "root": path, "name": name or os.path.basename(path.rstrip("\\/")) or path,
            "label": "Network share" if net else "Folder", "type": "network" if net else "folder",
            "total": total, "used": total - free, "free": free, "location": True}


def volumes():
    vs = plat.list_volumes()
    extra = os.environ.get("HOARD_SCAN_DIR")   # map one folder as its own "disk" (used by the smoke test)
    if extra and os.path.isdir(extra):
        v = location_volume(extra)
        v.pop("location")
        vs.append(v)
    for loc in core.load_settings().get("locations", []):
        if os.path.isdir(loc["path"]):
            vs.append(location_volume(loc["path"], loc.get("name")))
    vs += cloud.volumes()
    seen, unique = set(), []
    for v in vs:                       # ids are hashed now, but never let two places share one
        if v["id"] in seen:
            core.log_error("volumes", f"two places share the id {v['id']}; ignoring {v['root']}")
            continue
        seen.add(v["id"])
        unique.append(v)
    vs = unique
    core.migrate_ids(vs)
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


def clean_parts(parts):
    """Validate a path given as a list of names. Every action that touches the disk goes through this,
    so nothing can step outside the volume ('..'), smuggle in a separator, or address an NTFS stream (':')."""
    if not isinstance(parts, (list, tuple)):
        raise ValueError("Bad path.")
    bad = {"/", "\\", "\0"} | ({":"} if plat.NAME == "windows" else set())
    for p in parts:
        if not isinstance(p, str) or p in ("", ".", "..") or any(c in p for c in bad):
            raise ValueError("Bad path.")
    return list(parts)


def full_path(vid, parts):
    parts = clean_parts(parts)
    v = vol(vid)
    if v["type"] == "cloud":
        return cloud.remote_path(v, parts)
    return os.path.join(v["root"], *parts)


def is_cloud(vid):
    return VOLS.get(vid, {}).get("type") == "cloud"


def scan_any(v, st):
    """Cloud -> rclone; otherwise the parallel native walker when bundled, else the Python scanner."""
    if v["type"] == "cloud":
        return cloud.scan_remote(v, st)
    import fastscan
    if fastscan.walk_available():
        st.exclude = {os.path.normpath(p) for p in plat.scan_excludes(v["root"])}
        try:
            return fastscan.walk_scan(v, st)
        except Exception as ex:  # noqa: BLE001 - fall back to the Python scanner
            core.log_error("hoard-walk", str(ex))
            st.files = st.bytes = st.errors = 0
            st.current = "Scanning…"
    return core.scan_volume(v, st)


def fast_available(v):
    if plat.NAME != "windows":
        return False
    import fastscan
    return fastscan.available(v)


def fast_scan(v, st):
    """NTFS file-table scan through the elevated native helper; falls back to a normal scan if declined."""
    import fastscan
    try:
        return fastscan.scan(v, st)
    except fastscan.Declined:
        st.fast = False
        st.current = "Permission declined, so scanning the normal way…"
        return scan_any(v, st)


def scan_root_problem(v):
    """Refuse removals when the saved scan can't be tied to this volume's folder: paths in it might
    point somewhere else. Drive letters and the startup disk can't be confused, so they're exempt."""
    s = drive_state(v["id"])
    if s["cur_ts"] is None:
        return None
    root = s.get("cur_root")
    if root is None:
        if v.get("legacy"):
            return (f"Scan {v['name']} again before removing things from it. Its saved scan is from an older "
                    "version of Hoard, which can't confirm which folder it came from.")
        return None
    same = os.path.normcase(root) == os.path.normcase(v["root"]) if v["type"] != "cloud" else root == v["root"]
    return None if same else f"The saved scan of {v['name']} is of a different folder. Scan it again first."


def trash_any(vid, parts, expect=None):
    """Move one item to the Recycle Bin / Trash (or the cloud's own trash). Returns (ok, error).
    `expect` is the identity recorded when the item was marked; anything else at that path is left alone."""
    parts = clean_parts(parts)
    v = vol(vid)
    why = scan_root_problem(v)
    if why:
        return False, why
    if v["type"] == "cloud":
        if not parts:
            return False, "That's the whole cloud drive."
        node, f = walk(drive_state(vid)["cur"], parts) if list_snaps(vid) else (None, None)
        return cloud.trash(v, parts, is_dir=node is not None and f is None)
    p = os.path.join(v["root"], *parts)
    why = plat.protected(v["root"], parts, p) or plat.critical(p)
    if why:
        return False, why
    if plat.linked_ancestor(v["root"], parts):
        return False, "Part of this path is now a link to somewhere else, so Hoard won't remove through it. Scan again."
    if not os.path.lexists(p):
        return True, "gone"
    if expect is not None:
        try:
            now = plat.identity(p)
        except OSError:
            return True, "gone"
        if (len(expect) == 3 and now != list(expect)) or (len(expect) == 1 and now[2] != expect[0]):
            return False, ("This changed since you marked it: something else is at that path now. "
                           "Mark it again if you still want it removed.")
    # hold the folders from the root of the disk down to the item while the OS trashes it, and check the item
    # (identity and protection) through that held folder, so nothing can be swapped in between
    try:
        with safefs.Dir.open(os.path.dirname(p), guard=True) as d:
            name = os.path.basename(p)
            now = d.check(name, list(expect) if expect is not None and len(expect) == 3 else None, guard=True)
            if expect is not None and len(expect) == 1 and (stat.S_ISDIR(now.st_mode) and not plat.is_link(now)) != expect[0]:
                raise safefs.Unsafe(safefs.CHANGED)
            ok, err, _where = d.trash(name, safefs.ident(now))
            return ok, err
    except safefs.Unsafe as ex:
        return False, str(ex)
    except FileNotFoundError:
        return True, "gone"
    except OSError as ex:
        return False, ex.strerror or str(ex)


def _tree(s, vid, ts):
    """Load a snapshot through a small per-volume cache (current + baseline + one spare)."""
    if ts in s["trees"]:
        return s["trees"][ts]
    tree, meta = core.load_snapshot(vid, ts)
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
                              "prev_partial": False, "base_pick": None, "changes": None, "trees": {}, "cache": {}}
        snaps = list_snaps(vid)
        newest = snaps[0] if snaps else None
        if newest != s["cur_ts"]:
            s["cur_ts"] = newest
            s["cur"], s["cur_partial"] = _tree(s, vid, newest) if newest else (None, False)
            s["cur_root"] = (core.snap_meta(vid, newest) or {}).get("root") if newest else None
            s["changes"] = None
            s["cache"] = {}
        want = s["base_pick"] if s["base_pick"] in snaps[1:] else (snaps[1] if len(snaps) > 1 else None)
        if want != s["prev_ts"]:
            s["prev_ts"] = want
            s["prev"], s["prev_partial"] = _tree(s, vid, want) if want else (None, False)
            s["changes"] = None
            s["cache"] = {}
        return s


def current_trees():
    """(root, tree) for every connected volume that has a scan."""
    out = []
    for vid, v in list(VOLS.items()):
        if v["type"] != "cloud" and list_snaps(vid):
            s = drive_state(vid)
            if s["cur"] is not None:
                out.append((v["root"], s["cur"]))
    return out


def locate(path):
    """(vol id, parts, node or None) for an absolute path inside a scanned volume (longest root wins)."""
    best = None
    for vid, v in VOLS.items():
        if v["type"] == "cloud":
            continue
        parts = core.rel_parts(v["root"], path)
        if parts is not None and list_snaps(vid) and (best is None or len(v["root"]) > len(VOLS[best[0]]["root"])):
            best = (vid, parts)
    if best is None:
        return None, None, None
    s = drive_state(best[0])
    node, f = walk(s["cur"], best[1]) if s["cur"] is not None else (None, None)
    return best[0], best[1], (f if f else node)


def fm_scan_lookup(path):
    """For the Files view: (node or None, scan time) of Hoard's last scan of the folder at path."""
    if not VOLS:
        volumes()
    vid, _parts, node = locate(path)
    if vid is None:
        return None, None
    return node, drive_state(vid)["cur_ts"]


fm.HOOKS.update(scan=fm_scan_lookup, volumes=volumes)


def run_scan(vid):
    st = JOB["scan"]
    try:
        v = vol(vid)
        tree = fast_scan(v, st) if st.fast else scan_any(v, st)
        if st.cancel or tree is None:
            return
        st.current = "Saving…"
        now = core.commit_scan(vid, tree, v["root"], core.load_settings()["keep"], guard=LOCK)
        with LOCK:
            s = STATE.get(vid)
            if s is not None:
                s["trees"][now] = (tree, False)   # so the state switch below doesn't re-read it from disk
                s["base_pick"] = None              # compare the new scan with the one before it
            drive_state(vid)
    except Exception as ex:  # noqa: BLE001
        st.error = str(ex)
        import traceback
        core.log_error("scan", traceback.format_exc())
    finally:
        st.running = False


def headless_scan(ids=None):
    """Scan the given volumes (default: the ones chosen in Settings), save, prune, report, notify."""
    core.migrate_v1()
    settings = core.load_settings()
    vols = {v["id"]: v for v in volumes()}
    ids = ids or settings["schedule"]["volumes"] or ([next(iter(vols))] if vols else [])
    lock = os.path.join(core.DATA_DIR, "scan.lock")
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
            st = core.Scan(vid, 0)
            try:
                # the task runs with admin rights when "use fast scan" was approved in Settings
                if settings.get("scheduleFast") and plat.NAME == "windows" and plat._os.is_admin() and fast_available(v):
                    import fastscan
                    tree = fastscan.scan(v, st)
                else:
                    tree = scan_any(v, st)
            except Exception as ex:  # noqa: BLE001 - e.g. a cloud that's offline; keep going with the rest
                core.log_error(f"scheduled scan of {vid}", str(ex))
                continue
            before = list_snaps(vid)
            now = core.commit_scan(vid, tree, v["root"], settings["keep"])
            entry = {"vol": vid, "name": v["name"], "time": now, "total": tree.size, "files": tree.nfiles,
                     "errors": st.errors, "secs": round(time.time() - t0), "auto": True}
            prev, ch = None, []
            if before:
                prev, meta = core.load_snapshot(vid, before[0])
                if prev is not None:
                    ch = all_changes({"cur": tree, "prev": prev, "prev_partial": bool(meta.get("partial")),
                                      "changes": None})
                    entry.update(since=before[0], delta=tree.size - prev.size,
                                 top=[{"p": list(pp), "d": d} for d, pp, *_ in ch[:5]])
            core.append_report(entry)
            limit = float(settings.get("notifyGB") or 0) * (1 << 30)
            if limit and entry.get("delta", 0) >= limit:
                top = entry.get("top") or []
                body = f"Biggest change: {top[0]['p'][-1]} +{human(top[0]['d'])}" if top else "Open Hoard to see what grew."
                plat.notify(f"{v['name']} grew {human(entry['delta'])} since the last scan", body)
            for w in settings.get("watch", []):           # folder size alerts
                if w.get("vol") != vid:
                    continue
                node, _ = walk(tree, w["path"])
                old, _ = walk(prev, w["path"]) if prev is not None else (None, None)
                lim = float(w["gb"]) * (1 << 30)
                if node is not None and node.size > lim and (old is None or old.size <= lim):
                    plat.notify(f"{w['path'][-1] if w['path'] else v['name']} passed {w['gb']:g} GB",
                                f"It's now {human(node.size)}. Open Hoard to see what's inside.")
            if settings.get("reportDir"):
                try:
                    save_report(vid, v, tree, now, prev, before[0] if before else None, ch, settings["reportDir"])
                except OSError as ex:
                    core.log_error("saving report", str(ex))
        try:                                  # after the scans: auto-clean (if due) and low-space alerts
            import automation
            automation.run_auto_clean(core.load_settings())
            automation.check_low_space(list(vols.values()), core.load_settings())
        except Exception as ex:  # noqa: BLE001
            core.log_error("automation", str(ex))
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


def walk_prev(s, parts):
    pn = s["prev"]
    for p in parts:
        pn = prev_child(pn, p, s["prev_partial"])
    return pn


def is_ignored(parts, ign):
    t = tuple(parts)
    return any(t[:len(i)] == i for i in ign)


def view_json(node, pn, partial, depth, minb, parts, ign):
    out = {"n": node.name, "s": node.size, "k": "d", "f": node.nfiles, "c": dominant(node.cats), "a": node.age()}
    if node.cloud:
        out["cl"] = node.cloud
    if pn is not None:
        out["d"] = node.size - (0 if pn is NEW else pn.size)
    if depth <= 0:
        return out
    items = []
    small_s, small_n, ign_s = node.osize, node.ocount, 0
    for d in node.dirs:
        cp = parts + (d.name,)
        if ign and cp in ign:
            ign_s += d.size
            continue
        if d.size >= minb and d.size > 0:
            items.append(view_json(d, prev_child(pn, d.name, partial), partial, depth - 1, minb, cp, ign))
        else:
            small_s += d.size
            small_n += d.nfiles
    for n, sz, c, mt in node.files:
        if ign and parts + (n,) in ign:
            ign_s += sz
            continue
        if sz >= minb:
            it = {"n": n, "s": sz, "k": "f", "c": c, "a": mt}
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
        items.append({"n": f"{small_n:,} smaller item{'s' if small_n != 1 else ''}", "s": small_s, "k": "o",
                      "c": OTHER, "a": node.own})
    out["ch"] = items
    if ign_s:
        out["ign"] = ign_s
    return out


def iter_files(node, parts, ign):
    """Every individually kept file under node: (size, parts, cat, mtime)."""
    stack = [(node, tuple(parts))]
    while stack:
        n, pp = stack.pop()
        for f in n.files:
            if not (ign and pp + (f[0],) in ign):
                yield f[1], pp + (f[0],), f[2], f[3]
        for d in n.dirs:
            cp = pp + (d.name,)
            if not (ign and cp in ign):
                stack.append((d, cp))


def top_files(node, parts, s, ign, limit=100):
    best = heapq.nlargest(limit, iter_files(node, parts, ign), key=lambda x: x[0])
    out = []
    for sz, pp, c, mt in best:
        it = {"p": list(pp), "s": sz, "c": c, "k": "f", "a": mt}
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
        for fn, sz, cat, _mt in n.files:
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


def age_histogram(node, parts, ign):
    """Bytes by last-modified age: [<1 week, <1 month, <3 months, <1 year, older, unknown]."""
    now = time.time()
    edges = [now - d * DAY for d in AGE_BUCKETS]
    hist = [0] * 6

    def add(size, mt):
        if not mt:
            hist[5] += size
            return
        for i, e in enumerate(edges):
            if mt >= e:
                hist[i] += size
                return
        hist[4] += size
    stack = [(node, tuple(parts))]
    while stack:
        n, pp = stack.pop()
        for f in n.files:
            if not (ign and pp + (f[0],) in ign):
                add(f[1], f[3])
        add(n.osize, n.own)
        for d in n.dirs:
            cp = pp + (d.name,)
            if not (ign and cp in ign):
                stack.append((d, cp))
    return hist


def resave(vid):
    """Write the current scan back after removals, in the background, under the volume lock."""
    s = drive_state(vid)
    if s["cur"] is None:
        return
    v = VOLS.get(vid) or {}
    threading.Thread(target=core.save_snapshot, daemon=True,
                     args=(vid, s["cur"], s["cur_ts"], s["cur_partial"], s.get("cur_root") or v.get("root"), LOCK)).start()


def remove_from_tree(s, parts):
    tree = s["cur"]
    if tree is None:
        return
    chain = [tree]
    n = tree
    for p in parts[:-1]:
        n = n.child(p)
        if n is None:
            return
        chain.append(n)
    last = parts[-1]
    d = n.child(last)
    if d is not None and d.name == last:
        size, nf, cats, msum = d.size, d.nfiles, d.cats, d.msum
        n.dirs.remove(d)
        n._idx = None
    else:
        f = n.file(last)
        if f is None:
            return
        size, nf, msum = f[1], 1, f[1] * f[3]
        cats = [0] * 9
        cats[f[2]] = f[1]
        n.files.remove(f)
    for a in chain:
        a.size -= size
        a.nfiles -= nf
        a.msum -= msum
        for i, v in enumerate(cats):
            a.cats[i] -= v
    s["changes"] = None
    s["cache"] = {}


def replace_in_tree(s, parts, new):
    """Put a fresh measurement of the folder at parts in place and carry the difference up to the root."""
    chain = [s["cur"]]
    for p in parts:
        c = chain[-1].child(p)
        if c is None:
            return
        chain.append(c)
    old = chain[-1]
    dsize, dn, dm, dcl = new.size - old.size, new.nfiles - old.nfiles, new.msum - old.msum, new.cloud - old.cloud
    dcats = [a - b for a, b in zip(new.cats, old.cats)]
    for slot in Node.__slots__:
        if slot != "name":
            setattr(old, slot, getattr(new, slot))
    old._idx = None
    for a in chain[:-1]:
        a.size += dsize
        a.nfiles += dn
        a.msum += dm
        a.cloud += dcl
        a.cats = [x + d for x, d in zip(a.cats, dcats)]
        a.mtime = max(a.mtime, new.mtime)
    s["changes"] = None
    s["cache"] = {}


def refresh_paths(paths):
    """Bring the saved scans up to date after files or folders changed outside a scan (Files jobs, uninstallers).
    Each path's folder is listed again and the item itself measured again; the rest of the scan is reused."""
    if not VOLS:
        volumes()
    fold = (lambda x: x) if plat.NAME == "linux" else str.casefold
    todo = {}                                 # (vol id, folder parts) -> names in it to measure again
    scanned = [(vid, v["root"]) for vid, v in list(VOLS.items()) if v["type"] != "cloud" and list_snaps(vid)]
    for p in paths:
        for vid, root in scanned:             # every scan it's in: an added folder can sit inside a scanned drive
            parts = core.rel_parts(root, p)
            if not parts or drive_state(vid)["cur"] is None:
                continue                      # elsewhere, or the location itself (it needs a full scan)
            with LOCK:
                n, depth = drive_state(vid)["cur"], 0
                while depth < len(parts) - 1 and (c := n.child(parts[depth])) is not None:   # deepest folder in the scan
                    n, depth = c, depth + 1
            todo.setdefault((vid, tuple(parts[:depth])), set()).add(fold(parts[depth]))
    deep = {(vid, tuple(fold(x) for x in f) + (n,)) for (vid, f), names in todo.items() for n in names}
    touched = set()
    for (vid, f), names in todo.items():
        ff = tuple(fold(x) for x in f)
        if any((vid, ff[:i]) in deep for i in range(1, len(ff) + 1)):
            continue                          # inside something measured again anyway
        s, v = drive_state(vid), VOLS[vid]
        path = full_path(vid, list(f))
        if not os.path.isdir(path):
            continue
        with LOCK:
            n, _f = walk(s["cur"], list(f))
            if n is None:
                continue
            keep = {d.name: d for d in n.dirs if fold(d.name) not in names}
        st = core.Scan(vid, 0)
        st.exclude = {os.path.normpath(x) for x in plat.scan_excludes(v["root"])}
        new = core.scan_dir(path, n.name, st, keep)     # outside the lock: the UI stays responsive meanwhile
        if st.errors and not new.dirs and not new.nfiles:
            continue                          # couldn't read the folder: keep what the scan had
        with LOCK:
            replace_in_tree(s, list(f), new)
        touched.add(vid)
    for vid in touched:
        resave(vid)
    if touched:
        APPS_CACHE["t"] = 0                   # app sizes come from the scan
    return bool(touched)


fm.HOOKS["changed"] = refresh_paths


def save_report(vid, v, tree, now, prev, prev_ts, changes, folder, parts=()):
    node, _ = walk(tree, list(parts))
    sep = os.sep
    root = v["root"].rstrip(sep)
    joinp = lambda pp: root + sep + sep.join(pp) if pp else v["root"]   # noqa: E731
    ch = [(d, joinp(pp), sz) for d, pp, _k, sz, _c in changes if tuple(pp[:len(parts)]) == tuple(parts)][:30]
    folders = sorted(((joinp(tuple(parts) + (d.name,)), d.size) for d in node.dirs), key=lambda x: -x[1])[:30]
    files = [(joinp(pp), sz) for sz, pp, _c, _m in heapq.nlargest(30, iter_files(node, parts, set()), key=lambda x: x[0])]
    pnode = walk(prev, list(parts))[0] if prev is not None else None
    html_text = reports.report_html(f"Hoard report: {node.name}", v["name"], now, node.size,
                                    prev_ts, node.size - pnode.size if pnode is not None else None, ch, folders, files)
    path = reports.unique_path(folder, f"Hoard report {v['name'].strip(':')} {time.strftime('%Y-%m-%d %H%M', time.localtime(now))}", "html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_text)
    return path


# ---------------------------------------------------------------- staging area
def staged_add(items):
    cur = core.read_staged()
    added = 0
    for it in items:
        vid = it["vol"]
        try:
            parts = clean_parts(it["path"])
        except ValueError:
            continue
        if not parts:
            continue
        if any(x["vol"] == vid and x["path"] == parts[:len(x["path"])] for x in cur):
            continue          # already staged, or inside something staged
        entry = {"vol": vid, "path": parts, "name": it.get("name") or parts[-1], "size": int(it.get("size") or 0),
                 "kind": it.get("kind") or "f", "c": int(it.get("c") or OTHER)}
        if not is_cloud(vid):        # remember exactly which file or folder this is, to check before removing
            try:
                entry["id"] = plat.identity(full_path(vid, parts))
                entry["kind"] = "d" if entry["id"][2] else "f"
            except (OSError, ValueError):
                continue             # not there (anymore): nothing to mark
        cur = [x for x in cur if not (x["vol"] == vid and x["path"][:len(parts)] == parts)]   # drop children
        cur.append(entry)
        added += 1
    core.write_staged(cur)
    return added


def staged_trash():
    items = core.read_staged()
    done, failed, freed, touched = [], [], 0, set()
    for it in items:
        try:
            expect = it.get("id") or ([it.get("kind") == "d"] if not is_cloud(it["vol"]) else None)
            ok, err = trash_any(it["vol"], it["path"], expect)
            if not ok:
                raise ValueError(err)
            done.append(it)
            if err != "gone":         # already gone: nothing freed, but the scan still shouldn't show it
                freed += it["size"]
            if list_snaps(it["vol"]):
                with LOCK:
                    remove_from_tree(drive_state(it["vol"]), it["path"])
                touched.add(it["vol"])
        except Exception as ex:  # noqa: BLE001
            failed.append(dict(it, error=str(ex)))
    core.write_staged(failed)
    for vid in touched:
        resave(vid)
    APPS_CACHE["t"] = 0               # an app may have gone (macOS uninstall), and app sizes come from the scan
    return {"done": len(done), "failed": failed, "size": freed}


def stage_from_abs(paths):
    """Turn absolute paths (from cleanup suggestions / apps) into staging items on their volumes."""
    volumes()
    items = []
    for c in paths:
        best = None
        for vid, v in VOLS.items():
            if v["type"] == "cloud":
                continue
            parts = core.rel_parts(v["root"], c["path"])
            if parts and (best is None or len(v["root"]) > len(VOLS[best[0]]["root"])):
                best = (vid, parts)
        if best:
            items.append({"vol": best[0], "path": best[1], "name": c.get("name"), "size": c.get("size"),
                          "kind": c.get("kind"), "c": c.get("c", OTHER)})
    return staged_add(items)


# ---------------------------------------------------------------- http
TOKEN = secrets.token_urlsafe(24)
LAST_SEEN = {"t": time.time(), "bye": None}
APPS_CACHE = {"t": 0, "items": []}


class _Keep:
    """Use an already opened file in a with-block without closing it there (its owner closes it)."""
    def __init__(self, f):
        self.f = f

    def __enter__(self):
        return self.f

    def __exit__(self, *exc):
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "Hoard/3"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        h = (self.headers.get("Host") or "").split(":")[0]
        return h in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(RES_DIR, "ui.html"), "rb") as f:
                html_bytes = f.read().replace(b"__TOKEN__", TOKEN.encode())
            return self._send(200, html_bytes, "text/html; charset=utf-8")
        if u.path == "/file":
            return self._file(parse_qs(u.query))
        if u.path == "/fmfile":
            return self._fmfile(parse_qs(u.query))
        if u.path.startswith("/assets/"):
            return self._asset(u.path.removeprefix("/assets/"))
        self._send(404, {"error": "not found"})

    ASSET_TYPES = {"startup.json": "application/json", "loading.json": "application/json",
                   "scanning.json": "application/json", "done.json": "application/json",
                   "dotlottie.js": "text/javascript; charset=utf-8", "dotlottie-player.wasm": "application/wasm",
                   "fm.js": "text/javascript; charset=utf-8", "fm.css": "text/css; charset=utf-8"}

    def _asset(self, name):
        """The animation player and animations bundled with the app: a fixed list, nothing else."""
        ctype = self.ASSET_TYPES.get(name)
        if not ctype:
            return self._send(404, {"error": "not found"})
        try:
            with open(os.path.join(RES_DIR, "assets", name), "rb") as f:
                body = f.read()
        except OSError:
            return self._send(404, {"error": "not found"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, q):
        """Stream a file for the preview (images, video, audio, PDF, text). Range requests supported."""
        if (q.get("t") or [""])[0] != TOKEN:
            return self._send(403, {"error": "bad token"})
        try:
            if is_cloud(q["v"][0]):
                raise ValueError("cloud")
            parts = clean_parts(json.loads(q["p"][0]))
            path = full_path(q["v"][0], parts)
            # never read through a link: it could lead outside the scanned folder
            if plat.linked_ancestor(vol(q["v"][0])["root"], parts) or plat.is_link(os.lstat(path)):
                raise ValueError("link")
            size = os.path.getsize(path)
        except (KeyError, ValueError, OSError):
            return self._send(404, {"error": "not found"})
        if not os.path.isfile(path):
            return self._send(404, {"error": "not a file"})
        self._stream(path, size)

    def _fmfile(self, q):
        """The Files view's viewer: stream any regular file by absolute path (never a link, folder or device)."""
        if (q.get("t") or [""])[0] != TOKEN:
            return self._send(403, {"error": "bad token"})
        try:
            f, st = fm.open_for_read(q["p"][0])      # no links anywhere on the way; verified after opening
        except (KeyError, fm.FmError):
            return self._send(404, {"error": "not found"})
        with f:
            self._stream(q["p"][0], st.st_size, f)

    def _stream(self, path, size, opened=None):
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype in ("text/html", "application/xhtml+xml", "text/xml", "application/xml", "text/javascript"):
            ctype = "text/plain"            # show markup as text; never run it in the app's page
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and size:
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            code = 206
        if start > end and size:
            return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "sandbox")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with (open(path, "rb") if opened is None else _Keep(opened)) as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    b = f.read(min(1 << 20, left))
                    if not b:
                        break
                    self.wfile.write(b)
                    left -= len(b)
        except (OSError, ConnectionError):
            pass

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        name = self.path.split("?")[0].removeprefix("/api/")
        if name == "bye":                # sent by navigator.sendBeacon, which can't set headers: token in the body
            n = int(self.headers.get("Content-Length") or 0)
            if self.rfile.read(min(n, 200)).decode("utf-8", "replace").strip() == TOKEN:
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
        except ConnectionError:
            pass                # the window closed mid-reply; nothing to report
        except Exception as ex:  # noqa: BLE001
            import traceback
            core.log_error(f"api {name}", traceback.format_exc())
            self._send(500, {"error": str(ex)})

    # ---------------------------------------------------------- disks & scanning
    def api_ping(self, b):
        return {}

    def api_drives(self, b):
        st = core.load_settings()
        return {"drives": volumes(), "os": plat.NAME, "sep": os.sep,
                "trashName": plat.TRASH_NAME, "revealName": plat.REVEAL_NAME, "theme": st["theme"],
                "ignore": st["ignore"], "watch": st["watch"], "hiddenNote": plat.HIDDEN_NOTE,
                "fastScan": st.get("fastScan", False),
                "fastVols": [v["id"] for v in VOLS.values() if fast_available(v)]}

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
        if v["type"] == "cloud":
            used = v.get("used") or 0
        job = core.Scan(vid, used)
        job.fast = bool(b.get("fast")) and fast_available(v)
        JOB["scan"] = job
        threading.Thread(target=run_scan, args=(vid,), daemon=True).start()
        return {"ok": True, "fast": job.fast}

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
                "cancelled": j.cancel, "error": j.error, "fast": j.fast, "cloud": j.cloud}

    # ---------------------------------------------------------- map and lists
    def api_view(self, b):
        vid = b["drive"]
        s = drive_state(vid)
        if s["cur"] is None:
            return {"none": True}
        parts = b.get("path") or []
        ign = core.ignored(core.load_settings(), vid)
        with LOCK:
            node, f = walk(s["cur"], parts)
            if node is None or f is not None:
                return {"missing": True}
            pn = walk_prev(s, parts)
            frac = min(0.05, max(0.0001, float(b.get("minFrac", 0.0004))))     # the map's detail level
            v = view_json(node, pn, s["prev_partial"], max(1, min(8, int(b.get("depth", 8)))), max(1, node.size * frac),
                          tuple(parts), ign)
            v["cats"] = list(node.cats)
            if not parts and not s["cur_partial"] and VOLS.get(vid, {}).get("type") in ("fixed", "removable"):
                hidden = VOLS[vid]["used"] - node.size
                if hidden > (1 << 30):
                    v["hidden"] = hidden
        return {"view": v, "path": parts, "time": s["cur_ts"], "prevTime": s["prev_ts"], "partial": s["cur_partial"]}

    def api_files(self, b):
        s = drive_state(b["drive"])
        if s["cur"] is None:
            return {"items": []}
        parts = b.get("path") or []
        node, f = walk(s["cur"], parts)
        if node is None:
            return {"items": []}
        with LOCK:
            return {"items": top_files(node, parts, s, core.ignored(core.load_settings(), b["drive"]))}

    def api_changes(self, b):
        s = drive_state(b["drive"])
        if s["prev"] is None:
            return {"items": [], "noPrev": True}
        parts = tuple(b.get("path") or [])
        ign = core.ignored(core.load_settings(), b["drive"])
        with LOCK:
            ch = all_changes(s)
        k = len(parts)
        items = [{"p": list(pp), "d": d, "k": kind, "s": sz, "c": c}
                 for d, pp, kind, sz, c in ch if pp[:k] == parts and len(pp) > k and not is_ignored(pp, ign)][:100]
        return {"items": items, "prevTime": s["prev_ts"]}

    def api_ages(self, b):
        vid = b["drive"]
        s = drive_state(vid)
        parts = tuple(b.get("path") or [])
        key = ("ages", parts)
        if key not in s["cache"]:
            node, _ = walk(s["cur"], list(parts))
            if node is None:
                return {"hist": [0] * 6}
            s["cache"][key] = age_histogram(node, parts, core.ignored(core.load_settings(), vid))
        return {"hist": s["cache"][key], "buckets": AGE_BUCKETS}

    def api_types(self, b):
        vid = b["drive"]
        s = drive_state(vid)
        parts = b.get("path") or []
        node, _ = walk(s["cur"], parts)
        if node is None:
            return {"items": []}
        tot = {}
        for sz, _pp, _c, _m in iter_files(node, parts, core.ignored(core.load_settings(), vid)):
            name = _pp[-1]
            e = core.ext_of(name) or "(no extension)"
            t = tot.setdefault(e, [0, 0])
            t[0] += sz
            t[1] += 1
        items = sorted(({"ext": e, "s": v[0], "n": v[1], "c": core.cat_of("x." + e)} for e, v in tot.items()),
                       key=lambda x: -x["s"])[:150]
        return {"items": items, "tracked": sum(i["s"] for i in items), "total": node.size}

    def api_search(self, b):
        vid = b["drive"]
        s = drive_state(vid)
        parts = b.get("path") or []
        node, _ = walk(s["cur"], parts)
        if node is None:
            return {"items": []}
        q = (b.get("q") or "").strip().lower()
        wild = any(ch in q for ch in "*?[")
        min_size = int(b.get("minSize") or 0)
        cat = b.get("cat")
        cat = int(cat) if cat not in (None, "", -1, "-1") else None
        older = time.time() - float(b["olderDays"]) * DAY if b.get("olderDays") else None
        kind = b.get("kind") or "any"
        ext = (b.get("ext") or "").lower().lstrip(".")
        ign = core.ignored(core.load_settings(), vid)
        # "personal": only the user's own files (what Simple mode suggests). Skips system and program folders,
        # app data, game libraries, the Recycle Bin and hidden folders, and placeholder dates (before 1990).
        personal = bool(b.get("personal")) and not is_cloud(vid)
        root = vol(vid)["root"] if personal else None

        def name_ok(n):
            n = n.lower()
            if ext and not n.endswith("." + ext):
                return False
            if not q:
                return True
            return fnmatch.fnmatch(n, q) if wild else q in n

        hits = []
        stack = [(node, tuple(parts))]
        while stack:
            n, pp = stack.pop()
            if kind != "file":
                for d in n.dirs:
                    cp = pp + (d.name,)
                    if ign and cp in ign:
                        continue
                    if d.size >= min_size and name_ok(d.name) and (cat is None or dominant(d.cats) == cat) \
                            and (older is None or (d.mtime and d.mtime < older)) and not ext \
                            and not (personal and plat.managed(root, list(cp) + ["_"])):
                        hits.append((d.size, cp, "d", dominant(d.cats), d.age()))
            if kind != "dir":
                for fn, sz, c, mt in n.files:
                    if sz >= min_size and name_ok(fn) and (cat is None or c == cat) \
                            and (older is None or (mt and mt < older)) and not (ign and pp + (fn,) in ign) \
                            and not (personal and (mt < plat.BOGUS_BEFORE or plat.managed(root, list(pp) + [fn]))):
                        hits.append((sz, pp + (fn,), "f", c, mt))
            for d in n.dirs:
                cp = pp + (d.name,)
                if ign and cp in ign:
                    continue
                if personal and plat.managed(root, list(cp) + ["_"]):
                    continue             # don't even look inside system / app-managed folders
                stack.append((d, cp))
        total = sum(h[0] for h in hits if h[2] == "f") if kind == "file" else None
        best = heapq.nlargest(300, hits, key=lambda h: h[0])
        return {"items": [{"p": list(pp), "s": sz, "k": k, "c": c, "a": a} for sz, pp, k, c, a in best],
                "count": len(hits), "total": total}

    def api_trend(self, b):
        vid = b["drive"]
        key = "/".join(b.get("path") or [])
        pts = []
        for ts in reversed(list_snaps(vid)):
            idx = core.load_index(vid, ts)
            pts.append({"ts": ts, "s": idx.get(key)})
        return {"points": pts}

    def api_history(self, b):
        vid = b["drive"]
        out = []
        for ts in list_snaps(vid):
            m = core.snap_meta(vid, ts) or {}
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

    # ---------------------------------------------------------- acting on items
    def api_reveal(self, b):
        if "drive" in b and is_cloud(b["drive"]):
            parts = b.get("path") or []
            url = cloud.web_url(vol(b["drive"]), parts[-1] if parts else "")
            if not url:
                return {"error": "There's no web page to open for this cloud."}
            import webbrowser
            webbrowser.open(url)
            return {"ok": True}
        p = full_path(b["drive"], b.get("path") or []) if "drive" in b else b.get("abs", "")
        if not p or not os.path.lexists(p):
            return {"error": "That item no longer exists."}
        plat.reveal(p)
        return {"ok": True}

    def api_open(self, b):
        if is_cloud(b["drive"]):
            return {"error": "Open cloud files from the cloud's website or app."}
        p = full_path(b["drive"], b.get("path") or [])
        if not os.path.lexists(p):
            return {"error": "That item no longer exists."}
        plat.open_default(p)
        return {"ok": True}

    def api_recycle(self, b):
        vid, parts = b["drive"], b.get("path") or []
        expect = None
        if not is_cloud(vid) and list_snaps(vid):   # what the scan saw there: a folder or a file
            node, f = walk(drive_state(vid)["cur"], clean_parts(parts))
            if node is not None or f is not None:
                expect = [f is None]
        ok, err = trash_any(vid, parts, expect)
        if not ok:
            return {"error": err}
        s = drive_state(vid)
        with LOCK:
            remove_from_tree(s, parts)
        resave(vid)
        APPS_CACHE["t"] = 0
        if err == "gone":
            return {"error": "That item no longer exists."}
        return {"ok": True}

    def api_info(self, b):
        """Owner, attributes and permissions, plus who owns the space in a folder."""
        vid, parts = b["drive"], b.get("path") or []
        if is_cloud(vid):
            return {"cloud": True}
        p = full_path(vid, parts)
        if not os.path.lexists(p):
            return {"error": "That item no longer exists."}
        info = plat.file_info(p)
        owners = None
        s = drive_state(vid)
        node, f = walk(s["cur"], parts) if s["cur"] is not None else (None, None)
        if isinstance(node, Node) and f is None and node.dirs:
            by = {}
            items = sorted([(d.size, d.name) for d in node.dirs] + [(x[1], x[0]) for x in node.files], reverse=True)[:150]
            for size, name in items:
                o = plat.owner_of(os.path.join(p, name))
                by[o] = by.get(o, 0) + size
            owners = sorted(({"owner": o, "s": sz} for o, sz in by.items() if sz > 0), key=lambda x: -x["s"])
        return {"info": info, "owners": owners}

    def api_add_location(self, b):
        path = (b.get("path") or "").strip().strip('"')
        if not path:
            return {"error": "Type or paste a folder path."}
        path = os.path.expandvars(os.path.expanduser(path))
        if not os.path.isdir(path):
            return {"error": "That folder doesn't exist, or Hoard can't open it."}
        st = core.load_settings()
        if any(os.path.normcase(l["path"]) == os.path.normcase(path) for l in st["locations"]):
            return {"ok": True, "id": location_volume(path)["id"], "existing": True}
        st["locations"].append({"path": path, "name": (b.get("name") or "").strip() or None})
        core.save_settings(st)
        volumes()
        return {"ok": True, "id": location_volume(path)["id"]}

    def api_locate(self, b):
        """Where is this folder or file? (for 'Open in Hoard' from the file manager)"""
        path = os.path.abspath(os.path.expanduser((b.get("abs") or "").strip().strip('"')))
        if not os.path.exists(path):
            return {"error": "That folder doesn't exist anymore."}
        volumes()
        best = None
        for vid, v in VOLS.items():
            if v["type"] == "cloud":
                continue
            parts = core.rel_parts(v["root"], path)
            if parts is not None and (best is None or len(v["root"]) > len(VOLS[best[0]]["root"])):
                best = (vid, parts)
        if best is None:
            return {"none": True, "path": path}
        vid, parts = best
        node = None
        if list_snaps(vid):
            n, f = walk(drive_state(vid)["cur"], parts)
            node = f or n
        return {"drive": vid, "parts": parts, "scanned": node is not None, "kind": "d" if os.path.isdir(path) else "f",
                "path": path}

    def api_context_menu(self, b):
        if b.get("on"):
            ok, err = plat.context_menu_install(plat.self_command())
        else:
            ok, err = plat.context_menu_remove()
        st = core.load_settings()
        st["contextMenu"] = bool(b.get("on")) and ok
        core.save_settings(st)
        return {"ok": ok, "error": err, "on": plat.context_menu_status()}

    def api_tray(self, b):
        if plat.NAME not in ("windows", "macos"):
            return {"error": "The tray icon is on Windows and macOS only; low-space alerts still come with the daily check."}
        if plat.NAME == "macos":
            import menubar as tray
        else:
            import tray
        ok, err = tray.set_autostart(bool(b.get("on")))
        st = core.load_settings()
        st["tray"] = bool(b.get("on")) and ok
        core.save_settings(st)
        return {"ok": ok, "error": err}

    def api_remove_location(self, b):
        st = core.load_settings()
        v = VOLS.get(b["drive"])
        if not v or not v.get("location"):
            return {"error": "That isn't a location you added."}
        st["locations"] = [l for l in st["locations"] if os.path.normcase(l["path"]) != os.path.normcase(v["root"])]
        core.save_settings(st)
        volumes()
        return {"ok": True}

    # ---------------------------------------------------------- cloud (rclone)
    def api_cloud_status(self, b):
        r = cloud.status()
        r["fastAvailable"] = plat.NAME == "windows"
        return r

    def api_cloud_install(self, b):
        return cloud.install()

    def api_cloud_connect(self, b):
        name = re.sub(r"[^\w-]", "", b.get("name") or "gdrive") or "gdrive"
        return cloud.connect_drive(name, bool(b.get("readonly", True)))

    def api_cloud_refresh(self, b):
        cloud.volumes(force=True)
        return {"drives": volumes()}

    def api_ignore(self, b):
        vid, parts, on = b["drive"], b.get("path") or [], bool(b.get("on", True))
        st = core.load_settings()
        lst = st["ignore"].setdefault(vid, [])
        key = "/".join(parts)
        if on and key and key not in lst:
            lst.append(key)
        if not on and key in lst:
            lst.remove(key)
        if not lst:
            st["ignore"].pop(vid, None)
        core.save_settings(st)
        drive_state(vid)["cache"] = {}
        return {"ignore": st["ignore"]}

    def api_watch(self, b):
        vid, parts, gb = b["drive"], b.get("path") or [], float(b.get("gb") or 0)
        st = core.load_settings()
        st["watch"] = [w for w in st["watch"] if not (w["vol"] == vid and w["path"] == parts)]
        if gb > 0:
            st["watch"].append({"vol": vid, "path": parts, "gb": gb, "name": parts[-1] if parts else vol(vid)["name"]})
        core.save_settings(st)
        return {"watch": st["watch"]}

    # ---------------------------------------------------------- staging area
    def api_stage_list(self, b):
        items = core.read_staged()
        return {"items": items, "total": sum(i["size"] for i in items)}

    def api_stage_add(self, b):
        if b.get("abs"):
            added = stage_from_abs(b["abs"])
        else:
            added = staged_add([dict(it, vol=b.get("drive") or it.get("vol")) for it in b.get("items", [])])
        return dict(self.api_stage_list({}), added=added)

    def api_stage_remove(self, b):
        items = [x for x in core.read_staged() if not (x["vol"] == b["vol"] and x["path"] == b["path"])]
        core.write_staged(items)
        return self.api_stage_list({})

    def api_stage_clear(self, b):
        core.write_staged([])
        return self.api_stage_list({})

    def api_stage_trash(self, b):
        r = staged_trash()
        return dict(r, **self.api_stage_list({}))

    # ---------------------------------------------------------- cleanup suggestions
    def api_cleanup_start(self, b):
        j = JOB["cleanup"]
        if j and j["running"]:
            return {"ok": True}
        volumes()
        JOB["cleanup"] = cleanup.start(current_trees())
        return {"ok": True}

    def api_cleanup_status(self, b):
        j = JOB["cleanup"]
        if not j:
            return {"running": False, "items": []}
        items = list(j["items"])
        for v in VOLS.values():          # cloud trash (Google Drive keeps deleted files and counts them)
            if v["type"] == "cloud" and v.get("backend") == "drive":
                items.append({"id": "cloudtrash-" + v["id"], "group": "Cloud", "title": f"{v['name']} trash",
                              "desc": "Files you deleted from Google Drive still count against your storage until the trash is emptied.",
                              "action": "cloud_empty", "size": v.get("trashed") or 0, "pending": False})
        return {"running": j["running"], "items": items}

    def api_cleanup_run(self, b):
        if b["id"].startswith("cloudtrash-"):
            v = vol(b["id"][len("cloudtrash-"):])
            before = v.get("trashed") or 0
            ok, err = cloud.empty_trash(v)
            cloud.volumes(force=True)
            volumes()
            return {"ok": ok, "error": err, "freed": before if ok else 0}
        r = cleanup.run(b["id"])
        j = JOB["cleanup"]
        if j and r.get("ok") and not r.get("launched"):
            for i, it in enumerate(j["items"]):
                if it["id"] == b["id"]:
                    j["items"][i] = dict(it, size=max(0, (it.get("size") or 0) - r.get("freed", 0)), done=True)
        return r

    # ---------------------------------------------------------- duplicates
    def api_dupes_start(self, b):
        vid = b["drive"]
        j = JOB["dupes"]
        if j and j["running"]:
            j["cancel"] = True
        if is_cloud(vid):
            JOB["dupes"] = cloud.dupes_start(vol(vid), clean_parts(b.get("path") or []),
                                             int(float(b.get("minMB") or 10) * (1 << 20)))
            return {"ok": True, "candidates": 0, "cloud": True}
        s = drive_state(vid)
        parts = b.get("path") or []
        node, _ = walk(s["cur"], parts)
        if node is None:
            return {"error": "Scan this disk first."}
        min_size = int(float(b.get("minMB") or 10) * (1 << 20))
        root = vol(vid)["root"]
        files = [{"path": os.path.join(root, *pp), "p": list(pp), "size": sz, "m": mt, "c": c}
                 for sz, pp, c, mt in iter_files(node, parts, core.ignored(core.load_settings(), vid)) if sz >= min_size]
        JOB["dupes"] = dupes.start(files)
        JOB["dupes"]["vol"] = vid
        return {"ok": True, "candidates": len(files)}

    def api_dupes_status(self, b):
        j = JOB["dupes"]
        if not j:
            return {"running": False, "groups": None}
        return {k: j[k] for k in ("running", "phase", "done", "total", "groups", "cancelled", "error", "candidates", "vol")}

    def api_dupes_cancel(self, b):
        if JOB["dupes"]:
            JOB["dupes"]["cancel"] = True
        return {}

    # ---------------------------------------------------------- apps
    def _apps(self):
        if time.time() - APPS_CACHE["t"] > 120:
            volumes()
            items = plat.installed_apps()
            now = {a.get("location") for a in items}
            gone = [a["location"] for a in APPS_CACHE["items"]           # uninstalled since the last listing:
                    if a.get("location") and a["location"] not in now]  # measure what's left of its folder
            if gone:
                refresh_paths(gone)
            for a in items:
                size = None
                if a.get("location"):
                    _vid, _parts, node = locate(a["location"])
                    if isinstance(node, Node):
                        size = node.size
                    elif isinstance(node, tuple):
                        size = node[1]
                a["size"] = size if size is not None else (a.get("estimate") or None)
                a["sizeFrom"] = "scan" if size is not None else ("estimate" if a.get("estimate") else "")
            items.sort(key=lambda a: -(a["size"] or 0))
            APPS_CACHE.update(t=time.time(), items=items)
        return APPS_CACHE["items"]

    def api_apps(self, b):
        if b.get("refresh"):
            APPS_CACHE["t"] = 0
        return {"items": [{k: v for k, v in a.items() if k != "cmd" or a["uninstall"] == "command"} for a in self._apps()],
                "os": plat.NAME}

    def api_app_related(self, b):
        app = next((a for a in self._apps() if a["id"] == b["id"]), None)
        if app is None:
            return {"items": []}

        def children(path):
            _vid, _parts, node = locate(path)
            if isinstance(node, Node):
                out = [(d.name, d.size, os.path.join(path, d.name), True) for d in node.dirs]
                out += [(f[0], f[1], os.path.join(path, f[0]), False) for f in node.files]
                return out
            try:      # not in a scan: list it live (sizes measured below for matches only)
                return [(e.name, 0, e.path, e.is_dir()) for e in os.scandir(path) if not e.is_symlink()]
            except OSError:
                return []
        items = appsmod.related(app, children)
        for it in items:
            if not it["size"]:
                it["size"] = cleanup.du(it["path"])[0]
        return {"items": items}

    def api_app_uninstall(self, b):
        app = next((a for a in self._apps() if a["id"] == b["id"]), None)
        if app is None:
            return {"error": "That app isn't installed anymore."}
        if app["uninstall"] == "trash":
            added = stage_from_abs([{"path": app["location"], "name": app["name"] + ".app",
                                     "size": app.get("size") or 0, "kind": "d", "c": 2}])
            return {"ok": True, "staged": added}
        ok, err = plat.run_uninstall(app)
        APPS_CACHE["t"] = 0
        return {"ok": ok, "error": err}

    # ---------------------------------------------------------- export
    def api_export_csv(self, b):
        folder = os.environ.get("HOARD_EXPORT_DIR") or plat.downloads_dir()
        base = re.sub(r"[^\w .-]", "", b.get("name") or "Hoard export")[:80]
        path = reports.write_csv(reports.unique_path(folder, f"{base} {time.strftime('%Y-%m-%d %H%M')}", "csv"),
                                 b["columns"], b["rows"])
        if not os.environ.get("HOARD_EXPORT_DIR"):
            plat.reveal(path)
        return {"file": path}

    def api_export_report(self, b):
        vid = b["drive"]
        s = drive_state(vid)
        if s["cur"] is None:
            return {"error": "Scan this disk first."}
        with LOCK:
            ch = all_changes(s)
        path = save_report(vid, vol(vid), s["cur"], s["cur_ts"], s["prev"], s["prev_ts"], ch,
                           os.environ.get("HOARD_EXPORT_DIR") or plat.downloads_dir(), tuple(b.get("path") or []))
        if not os.environ.get("HOARD_EXPORT_DIR"):
            plat.reveal(path)
        return {"file": path}

    def _export_dir(self):
        return os.environ.get("HOARD_EXPORT_DIR") or plat.downloads_dir()

    def _saved(self, path):
        if not os.environ.get("HOARD_EXPORT_DIR"):
            plat.reveal(path)
        return {"file": path}

    def api_export_image(self, b):
        """Save the map the user is looking at (a PNG data URL from the canvas)."""
        import base64
        data = b.get("data") or ""
        if not data.startswith("data:image/png;base64,"):
            return {"error": "That isn't a map image."}
        raw = base64.b64decode(data.split(",", 1)[1])
        base = re.sub(r"[^\w .-]", "", b.get("name") or "Hoard map")[:80]
        path = reports.unique_path(self._export_dir(), f"{base} {time.strftime('%Y-%m-%d %H%M')}", "png")
        with open(path, "wb") as f:
            f.write(raw)
        return self._saved(path)

    def api_export_pdf(self, b):
        """A one-file PDF: the map (as shown) plus what grew, largest folders and largest files."""
        import base64
        vid, parts = b["drive"], tuple(clean_parts(b.get("path") or []))
        s = drive_state(vid)
        if s["cur"] is None:
            return {"error": "Scan this disk first."}
        v = vol(vid)
        jpeg = None
        if (b.get("image") or "").startswith("data:image/jpeg;base64,"):
            jpeg = base64.b64decode(b["image"].split(",", 1)[1])
        with LOCK:
            node, _ = walk(s["cur"], list(parts))
            ch = all_changes(s)
        joinp = lambda pp: full_path(vid, list(pp))   # noqa: E731
        grew = [(joinp(pp), "+" + human(d)) for d, pp, _k, _sz, _c in ch if tuple(pp[:len(parts)]) == parts][:25]
        folders = [(joinp(parts + (d.name,)), human(d.size)) for d in sorted(node.dirs, key=lambda d: -d.size)[:25]]
        files = [(joinp(pp), human(sz)) for sz, pp, _c, _m in heapq.nlargest(25, iter_files(node, parts, set()), key=lambda x: x[0])]
        when = time.strftime("%d %b %Y %H:%M", time.localtime(s["cur_ts"]))
        sub = f"{human(node.size)} in {full_path(vid, list(parts))} · scanned {when}"
        if v.get("total"):
            sub += f" · {human(v['free'])} free of {human(v['total'])}"
        pdf = reports.report_pdf(f"Hoard: {node.name}", sub,
                                 [("What grew" + (f" since {time.strftime('%d %b', time.localtime(s['prev_ts']))}" if s["prev_ts"] else ""), grew),
                                  ("Largest folders", folders), ("Largest files", files)], jpeg)
        path = reports.unique_path(self._export_dir(), f"Hoard report {v['name'].strip(':')} {time.strftime('%Y-%m-%d %H%M')}", "pdf")
        with open(path, "wb") as f:
            f.write(pdf)
        return self._saved(path)

    # ---------------------------------------------------------- settings
    def api_settings(self, b):
        st = core.load_settings()
        import automation
        return {"settings": st, "schedule": plat.schedule_status(),
                "cleanChoices": [{"id": s["id"], "title": s["title"]} for s in automation.auto_clean_choices()],
                "contextMenu": plat.context_menu_status(), "contextMenuSupported": plat.NAME != "macos",
                "scheduleNote": plat.SCHEDULE_NOTE, "storage": core.storage_bytes(),
                "report": core.read_report()[-12:][::-1],
                "defaultReportDir": os.path.join(plat.documents_dir(), "Hoard reports")}

    def api_save_settings(self, b):
        cur = core.load_settings()
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
        if "fastScan" in new:
            cur["fastScan"] = bool(new["fastScan"])
        if "updateCheck" in new:
            cur["updateCheck"] = bool(new["updateCheck"])
        if isinstance(new.get("lowSpace"), dict):
            ls = new["lowSpace"]
            cur["lowSpace"] = {"enabled": bool(ls.get("enabled")), "percent": max(0.0, min(50.0, float(ls.get("percent") or 0))),
                               "gb": max(0.0, min(10000.0, float(ls.get("gb") or 0)))}
        if isinstance(new.get("autoClean"), dict):
            import automation
            ac, allowed = new["autoClean"], {s["id"] for s in automation.auto_clean_choices()}
            cur["autoClean"] = {"enabled": bool(ac.get("enabled")), "every": "day" if ac.get("every") == "day" else "week",
                                "ids": [i for i in ac.get("ids", []) if i in allowed],
                                "last": cur.get("autoClean", {}).get("last", 0)}
        before = core.load_settings()
        cur["scheduleFast"] = bool(new.get("scheduleFast", cur.get("scheduleFast"))) and plat.NAME == "windows" \
            and any(fast_available(v) for v in VOLS.values())
        if "ignore" in new and isinstance(new["ignore"], dict):
            cur["ignore"] = {k: [str(p) for p in v] for k, v in new["ignore"].items() if v}
        if "watch" in new and isinstance(new["watch"], list):
            cur["watch"] = [w for w in new["watch"] if isinstance(w, dict) and float(w.get("gb", 0)) > 0]
        if "reportDir" in new:
            rd = str(new["reportDir"] or "")
            if rd:
                try:
                    os.makedirs(rd, exist_ok=True)
                except OSError as ex:
                    return {"error": f"Can't use that report folder: {ex.strerror or ex}"}
            cur["reportDir"] = rd
        if cur["schedule"]["enabled"] and not cur["schedule"]["volumes"]:
            return {"error": "Choose at least one disk to scan."}
        err, ok = None, True
        h, m = (int(x) for x in t.split(":"))
        status = plat.schedule_status()
        if cur["schedule"]["enabled"]:
            # re-register only when something changed (an admin-level task means a permission prompt)
            changed = (not status["installed"] or before["schedule"]["time"] != t
                       or bool(status.get("elevated")) != cur["scheduleFast"])
            if changed:
                ok, err = plat.schedule_install(h, m, plat.self_command() + ["--scan"], elevated=cur["scheduleFast"])
                if not ok and cur["scheduleFast"]:
                    cur["scheduleFast"] = False          # declined: keep a normal daily scan instead
                    ok2, _ = plat.schedule_install(h, m, plat.self_command() + ["--scan"])
                    err = (err or "") + (" The daily scan will use the normal scanner." if ok2 else "")
        else:
            ok, err = plat.schedule_remove()
        core.save_settings(cur)
        for vid in os.listdir(core.SCANS_DIR) if os.path.isdir(core.SCANS_DIR) else []:
            core.prune(vid, cur["keep"])
        for s in STATE.values():
            s["cache"] = {}
        return {"ok": ok, "error": err, "settings": cur, "schedule": plat.schedule_status()}

    # ---------------------------------------------------------- updates & install
    def api_about(self, b):
        import updates
        return {"version": core.VERSION, "frozen": bool(getattr(sys, "frozen", False)), "os": plat.NAME,
                "installed": updates.is_installed(), "canInstall": updates.can_install(),
                "update": updates.cached(), "exe": sys.executable if getattr(sys, "frozen", False) else None}

    def api_update_check(self, b):
        import updates
        return updates.check(force=bool(b.get("force")))

    def api_update_apply(self, b):
        import updates
        return updates.apply()

    def api_install_self(self, b):
        import updates
        return updates.install(desktop=bool(b.get("desktop")))

    # ---------------------------------------------------------- Files view (logic in fm.py)
    def api_fm_roots(self, b):
        volumes()
        return fm.fm_roots(b)

    def api_fm_list(self, b):
        return fm.fm_list(b)

    def api_fm_check(self, b):
        return fm.fm_check(b)

    def api_fm_plan(self, b):
        return fm.fm_plan(b)

    def api_fm_start(self, b):
        return fm.fm_start(b)

    def api_fm_jobs(self, b):
        return fm.fm_jobs(b)

    def api_fm_control(self, b):
        return fm.fm_control(b)

    def api_fm_decide(self, b):
        return fm.fm_decide(b)

    def api_fm_undo(self, b):
        return fm.fm_undo(b)

    def api_fm_sizes(self, b):
        return fm.fm_sizes(b)

    def api_fm_find(self, b):
        return fm.fm_find(b)

    def api_fm_read(self, b):
        return fm.fm_read(b)

    def api_fm_prefs(self, b):
        return fm.fm_prefs(b)

    def api_fm_compare(self, b):
        return fm.fm_compare(b)

    def api_fm_results(self, b):
        return fm.fm_results(b)

    def api_fm_sync_plan(self, b):
        return fm.fm_sync_plan(b)

    def api_fm_archive_open(self, b):
        return fm.fm_archive_open(b)

    def api_fm_archive_list(self, b):
        return fm.fm_archive_list(b)

    def api_fm_archive_read(self, b):
        return fm.fm_archive_read(b)

    def api_fm_checksums(self, b):
        return fm.fm_checksums(b)

    def api_fm_edit_open(self, b):
        return fm.fm_edit_open(b)

    def api_open_folder(self, b):
        p = b.get("which") == "reports" and core.load_settings().get("reportDir")
        if p and os.path.isdir(p):
            plat.open_default(p)
            return {"ok": True}
        return {"error": "That folder doesn't exist yet."}


def watchdog():
    while True:
        time.sleep(2)
        now = time.time()
        bye = LAST_SEEN["bye"]
        if ((bye and now - bye > 5) or now - LAST_SEEN["t"] > 300) and not fm.busy():
            os._exit(0)                  # but never in the middle of a file job


def main(open_window=True, port=0, open_path=None):
    core.migrate_v1()
    try:
        import updates
        updates.cleanup_old()            # remove the previous exe left behind by an update
    except Exception:  # noqa: BLE001
        pass
    threading.Thread(target=core.warm_indexes, daemon=True).start()
    threading.Thread(target=fm.recover, daemon=True).start()      # clean up after a Files job cut short last time
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    if open_path:                              # "Open in Hoard" from the file manager
        from urllib.parse import quote
        url += "?open=" + quote(open_path)
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
    if "--uninstall" in argv:
        import updates
        sys.exit(updates.uninstall_interactive())
    if "--tray" in argv:
        if plat.NAME == "macos":
            import menubar as tray
        else:
            import tray
        sys.exit(tray.run())
    if "--scan" in argv:
        i = argv.index("--scan")
        ids = [a for a in argv[i + 1:] if not a.startswith("--")]
        sys.exit(headless_scan(ids))
    open_path = argv[argv.index("--open") + 1] if "--open" in argv and argv.index("--open") + 1 < len(argv) else None
    main(open_window="--no-window" not in argv,
         port=int(argv[argv.index("--port") + 1]) if "--port" in argv else 0, open_path=open_path)


if __name__ == "__main__":
    cli(sys.argv)
