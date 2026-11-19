"""Cleanup suggestions: measure the OS-specific targets from plat.cleanup_specs() and clear them.

Actions
  empty_bin       empty the Recycle Bin / Trash
  clear           permanently delete the *contents* of known cache folders (optionally only old files)
  stage_children  list each child of a folder (e.g. downloaded models) so the user can stage the ones to remove
  stage_files     list candidates found by a finder ("downloads", "node_modules") for staging
  command         needs admin or a package manager: show the command to run
  tool            open the OS's own cleanup tool
Only paths from the built-in specs are ever cleared; nothing the UI sends is used as a path.
"""
import glob
import os
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import core
import plat

INSTALLER_EXT = {"exe", "msi", "msix", "zip", "7z", "rar", "iso", "img", "dmg", "pkg", "deb", "rpm",
                 "appimage", "tar", "gz", "tgz", "xz", "bz2", "zst"}
DAY = 86400


def expand(paths):
    found = []
    for p in paths or []:
        p = os.path.normpath(os.path.expandvars(os.path.expanduser(p)))
        hits = glob.glob(p) if any(ch in p for ch in "*?[") else ([p] if os.path.lexists(p) else [])
        for h in hits:
            if (os.path.isdir(h) or os.path.isfile(h)) and not os.path.islink(h):
                found.append(h)
    found.sort(key=len)
    out = []
    for h in found:     # drop paths already inside another one
        if not any(h.startswith(o.rstrip(os.sep) + os.sep) for o in out):
            out.append(h)
    return out


def _too_broad(path):
    p = os.path.normcase(os.path.abspath(path)).rstrip("\\/")
    home = os.path.normcase(os.path.expanduser("~")).rstrip("\\/")
    bad = {home, os.path.normcase(os.path.dirname(home)).rstrip("\\/")}
    for var in ("LOCALAPPDATA", "APPDATA", "ProgramData", "SystemRoot", "ProgramFiles"):
        if os.environ.get(var):
            bad.add(os.path.normcase(os.environ[var]).rstrip("\\/"))
    return p in bad or os.path.dirname(p) == p or len(p.replace("\\", "/").strip("/").split("/")) < 2


def du(path, older=None):
    """(bytes, files) under path; with `older`, only files last modified before that time."""
    size_of = plat.size_of
    if os.path.isfile(path):
        try:
            s = os.lstat(path)
            return (size_of(s), 1) if older is None or s.st_mtime < older else (0, 0)
        except OSError:
            return 0, 0
    total = n = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink() or e.is_junction():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        s = e.stat(follow_symlinks=False)
                        if older is None or s.st_mtime < older:
                            total += size_of(s)
                            n += 1
                except OSError:
                    pass
    return total, n


def file_size(path):
    """Size of one file, even a locked system file like hiberfil.sys (read via its folder listing)."""
    d, name = os.path.split(path)
    try:
        with os.scandir(d) as it:
            for e in it:
                if e.name.lower() == name.lower():
                    return e.stat(follow_symlinks=False).st_size
    except OSError:
        pass
    return 0


_FD_OK = os.name != "nt" and {os.open, os.unlink, os.rmdir} <= os.supports_dir_fd and os.scandir in os.supports_fd
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _clear_fd(dfd, older, stats):
    """POSIX: work through open directory handles, so a folder swapped for a link mid-way is never followed:
    each child is opened relative to its parent with O_NOFOLLOW and must be the same folder we listed."""
    try:
        entries = list(os.scandir(dfd))
    except OSError:
        stats["skipped"] += 1
        return
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                continue
            if stat.S_ISDIR(st.st_mode):
                try:
                    cfd = os.open(e.name, _DIR_FLAGS, dir_fd=dfd)
                except OSError:
                    stats["skipped"] += 1
                    continue
                try:
                    now = os.fstat(cfd)
                    if (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
                        stats["skipped"] += 1       # replaced since we listed it
                        continue
                    _clear_fd(cfd, older, stats)
                finally:
                    os.close(cfd)
                try:
                    os.rmdir(e.name, dir_fd=dfd)    # only succeeds when empty
                except OSError:
                    pass
                continue
            if older is not None and st.st_mtime >= older:
                continue
            os.unlink(e.name, dir_fd=dfd)           # removes the entry itself; never follows anything
            stats["freed"] += plat.size_of(st)
            stats["files"] += 1
        except OSError:
            stats["skipped"] += 1


def _clear_dir(d, older, stats, keep_root, expect=None):
    """Windows (and any POSIX without dir_fd): hold each folder open, without delete sharing, while clearing it,
    so neither it nor any folder above it (up to the cache folder) can be renamed or swapped for a junction."""
    pin = plat.pin_dir(d, expect)
    if pin is None:
        stats["skipped"] += 1
        return
    try:
        try:
            entries = list(os.scandir(d))
        except OSError:
            stats["skipped"] += 1
            return
        for e in entries:
            try:
                if e.is_symlink() or e.is_junction():
                    continue                       # never follow (or delete through) links
                if e.is_dir(follow_symlinks=False):
                    _clear_dir(e.path, older, stats, keep_root=False, expect=e.inode())
                    continue
                s = e.stat(follow_symlinks=False)
                if plat.is_link(s):
                    continue
                if older is not None and s.st_mtime >= older:
                    continue
                size = plat.size_of(s)
                try:
                    os.remove(e.path)
                except PermissionError:
                    os.chmod(e.path, stat.S_IWRITE)     # read-only files on Windows
                    os.remove(e.path)
                stats["freed"] += size
                stats["files"] += 1
            except OSError:
                stats["skipped"] += 1
    finally:
        plat.unpin_dir(pin)
    if not keep_root:
        try:
            os.rmdir(d)          # only succeeds when empty
        except OSError:
            pass


def _clear_tree(d, older, stats):
    if _FD_OK:
        try:
            top = os.open(d, _DIR_FLAGS)
        except OSError:
            stats["skipped"] += 1
            return
        try:
            _clear_fd(top, older, stats)
        finally:
            os.close(top)
    else:
        _clear_dir(d, older, stats, keep_root=True)


def clear(paths, older=None):
    stats = {"freed": 0, "files": 0, "skipped": 0}
    for p in paths:
        if _too_broad(p):
            stats["skipped"] += 1
            continue
        if os.path.isfile(p):
            try:
                s = os.lstat(p)
                if older is None or s.st_mtime < older:
                    os.remove(p)
                    stats["freed"] += plat.size_of(s)
                    stats["files"] += 1
            except OSError:
                stats["skipped"] += 1
        else:
            _clear_tree(p, older, stats)
    return stats


# ---------------------------------------------------------------- finders (things to review and stage)
def find_downloads():
    d = plat.downloads_dir()
    cutoff = time.time() - 30 * DAY
    out = []
    try:
        entries = list(os.scandir(d))
    except OSError:
        return out
    for e in entries:
        try:
            if not e.is_file(follow_symlinks=False) or core.ext_of(e.name) not in INSTALLER_EXT:
                continue
            s = e.stat(follow_symlinks=False)
            if s.st_mtime < cutoff and s.st_size >= 5 << 20:
                out.append({"path": e.path, "name": e.name, "size": plat.size_of(s), "kind": "f",
                            "c": core.cat_of(e.name), "age": int(s.st_mtime)})
        except OSError:
            pass
    return out


def find_node_modules(trees):
    """node_modules folders (>= 20 MB) in projects whose own files haven't changed for 90 days."""
    cutoff = time.time() - 90 * DAY
    out, needs_rescan = [], False
    for root, tree in trees:
        stack = [(tree, [])]
        while stack:
            n, parts = stack.pop()
            nm = n.child("node_modules") if n.dirs else None
            if nm is not None and nm.name == "node_modules":
                activity = max([n.own] + [d.mtime for d in n.dirs if d is not nm])
                if activity == 0:
                    needs_rescan = True
                elif activity < cutoff and nm.size >= 20 << 20:
                    out.append({"path": os.path.join(root, *parts, "node_modules"), "name": f"{n.name}/node_modules",
                                "size": nm.size, "kind": "d", "c": 5, "age": activity})
            for d in n.dirs:
                if d.name != "node_modules" and not d.name.startswith("."):
                    stack.append((d, parts + [d.name]))
    return out, needs_rescan


# ---------------------------------------------------------------- analysis job
def analyze(job, trees):
    specs = plat.cleanup_specs()
    job["items"] = [dict(s, size=None, pending=True) for s in specs]

    def measure(i, spec):
        item = dict(spec, pending=False)
        try:
            act = spec["action"]
            older = time.time() - spec["olderDays"] * DAY if spec.get("olderDays") else None
            paths = expand(spec.get("paths"))
            item["where"] = paths[:6]
            if act == "empty_bin":
                item["size"] = plat.bin_size()
            elif act in ("clear", "command", "tool"):
                if spec.get("file"):
                    item["size"] = file_size(spec["file"])
                else:
                    sizes = [du(p, older) for p in paths]
                    item["size"] = sum(s for s, _ in sizes)
                    item["files"] = sum(n for _, n in sizes)
            elif act == "stage_children":
                cands = []
                for p in paths:
                    try:
                        for e in os.scandir(p):
                            if e.is_symlink():
                                continue
                            size, _ = du(e.path)
                            if size >= 1 << 20:
                                cands.append({"path": e.path, "name": e.name, "size": size,
                                              "kind": "d" if e.is_dir() else "f", "c": core.cat_of(e.name)})
                    except OSError:
                        pass
                item["candidates"] = sorted(cands, key=lambda c: -c["size"])
                item["size"] = sum(c["size"] for c in cands)
            elif act == "stage_files":
                if spec.get("finder") == "downloads":
                    cands = find_downloads()
                else:
                    cands, item["needsRescan"] = find_node_modules(trees)
                item["candidates"] = sorted(cands, key=lambda c: -c["size"])
                item["size"] = sum(c["size"] for c in cands)
        except Exception as ex:  # noqa: BLE001
            item["error"] = str(ex)
            item["size"] = 0
        job["items"][i] = item

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda a: measure(*a), enumerate(specs)))
    job["running"] = False


def start(trees):
    job = {"running": True, "items": [], "started": time.time()}
    threading.Thread(target=analyze, args=(job, trees), daemon=True).start()
    return job


def run(spec_id):
    spec = next((s for s in plat.cleanup_specs() if s["id"] == spec_id), None)
    if spec is None:
        return {"error": "Unknown cleanup item."}
    act = spec["action"]
    if act == "empty_bin":
        before = plat.bin_size() or 0
        ok, err = plat.empty_bin()
        return {"ok": ok, "error": err, "freed": before if ok else 0}
    if act == "clear":
        older = time.time() - spec["olderDays"] * DAY if spec.get("olderDays") else None
        st = clear(expand(spec.get("paths")), older)
        return {"ok": True, **st}
    if act == "tool":
        try:
            subprocess.Popen(spec["tool"])
            return {"ok": True, "launched": True}
        except OSError as ex:
            return {"error": str(ex)}
    return {"error": "This one needs to be run by you (see the command)."}
