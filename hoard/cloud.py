"""Cloud storage through rclone: Google Drive, OneDrive, Dropbox, Box, S3, SharePoint and ~70 more.

rclone (https://rclone.org) does the sign-in with its own verified app and keeps the tokens in its
config file; Hoard only runs it. Each configured rclone remote shows up as a "cloud" volume.
"""
import calendar
import json
import os
import shutil
import subprocess
import sys
import threading
import time

import core
import plat

PRETTY = {"drive": "Google Drive", "onedrive": "OneDrive", "dropbox": "Dropbox", "box": "Box", "s3": "S3",
          "pcloud": "pCloud", "mega": "MEGA", "b2": "Backblaze B2", "sharefile": "ShareFile", "webdav": "WebDAV",
          "sftp": "SFTP", "ftp": "FTP", "gphotos": "Google Photos", "protondrive": "Proton Drive",
          "azureblob": "Azure Blob", "koofr": "Koofr", "yandex": "Yandex Disk", "jottacloud": "Jottacloud"}
# Only clouds where a delete goes to a recoverable trash / recycle bin
TRASHABLE = {"drive", "onedrive", "dropbox", "box", "pcloud", "koofr"}
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
CACHE_FILE = os.path.join(core.DATA_DIR, "cloud.json")


def exe():
    p = shutil.which("rclone")
    if p:
        return p
    L = os.environ.get("LOCALAPPDATA", "")
    cands = [os.path.join(L, "Microsoft", "WinGet", "Links", "rclone.exe"),
             os.path.join(os.environ.get("ProgramFiles", ""), "rclone", "rclone.exe"),
             "/opt/homebrew/bin/rclone", "/usr/local/bin/rclone", "/usr/bin/rclone", os.path.expanduser("~/bin/rclone")]
    if L:
        import glob
        cands += glob.glob(os.path.join(L, "Microsoft", "WinGet", "Packages", "Rclone.Rclone*", "*", "rclone.exe"))
    return next((c for c in cands if c and os.path.isfile(c)), None)


def run(args, timeout=60):
    return subprocess.run([exe()] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=NO_WINDOW, env=plat.host_env())


def remotes():
    if not exe():
        return []
    try:
        r = run(["listremotes", "--long"], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in r.stdout.splitlines():
        if ":" in line:
            name, typ = line.split(":", 1)
            out.append((name.strip(), typ.strip()))
    return out


# ---------------------------------------------------------------- volumes (cached; `rclone about` is a network call)
_cache = {"t": 0, "vols": None, "busy": False}


def _load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            _cache["vols"] = json.load(f)
    except (OSError, ValueError):
        _cache["vols"] = []


def refresh():
    rs = remotes()
    counts = {}
    for _, t in rs:
        counts[t] = counts.get(t, 0) + 1
    vols = []
    for name, typ in rs:
        pretty = PRETTY.get(typ, typ.title())
        v = {"id": "cloud-" + plat.slug(name), "root": name + ":", "remote": name, "backend": typ,
             "name": pretty if counts[typ] == 1 else f"{pretty} ({name})", "label": f"{pretty} · rclone “{name}”",
             "type": "cloud", "total": 0, "used": 0, "free": 0, "trashed": 0, "other": 0,
             "trashable": typ in TRASHABLE}
        try:
            r = run(["about", name + ":", "--json"], timeout=25)
            a = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            a = {}
        v["total"] = int(a.get("total") or 0)
        v["free"] = int(a.get("free") or 0)
        v["used"] = (v["total"] - v["free"]) if v["total"] else int(a.get("used") or 0)
        v["trashed"] = int(a.get("trashed") or 0)
        v["other"] = int(a.get("other") or 0)       # Google: Gmail + Photos share the same quota
        vols.append(v)
    _cache.update(t=time.time(), vols=vols)
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(vols, f)
    except OSError:
        pass
    return vols


def volumes(force=False):
    if _cache["vols"] is None:
        _load_cache()
    if force:
        return [dict(v) for v in refresh()]
    if time.time() - _cache["t"] > 300 and not _cache["busy"] and exe():
        def bg():
            _cache["busy"] = True
            try:
                refresh()
            finally:
                _cache["busy"] = False
        threading.Thread(target=bg, daemon=True).start()
    return [dict(v) for v in _cache["vols"] or []]


# ---------------------------------------------------------------- scanning
def _ts(s):
    """rclone ModTime like 2024-05-31T16:15:57.034468261+01:00 -> unix time."""
    try:
        t = calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
        tail = s[19:]
        if tail.endswith("Z") or not tail:
            return t
        sign = 1 if "+" in tail else -1
        hh, mm = tail[-5:].split(":")
        return t - sign * (int(hh) * 3600 + int(mm) * 60)
    except (ValueError, IndexError):
        return 0


def scan_remote(v, st):
    """List the whole remote with `rclone lsjson -R` and build the same tree the disk scanner builds."""
    proc = subprocess.Popen([exe(), "lsjson", "-R", "--fast-list", "--no-mimetype", v["root"]],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", creationflags=NO_WINDOW, env=plat.host_env())
    root = core.Node(v["name"])
    index = {"": root}
    kept = {}

    def ensure(path):
        n = index.get(path)
        if n is None:
            parent_path, _, name = path.rpartition("/")
            n = core.Node(name)
            ensure(parent_path).dirs.append(n)
            index[path] = n
        return n

    for line in proc.stdout:
        if st.cancel:
            proc.kill()
            break
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        path = o.get("Path", "")
        if o.get("IsDir"):
            ensure(path)
            continue
        parent_path, _, name = path.rpartition("/")
        parent = ensure(parent_path)
        size = max(0, int(o.get("Size") or 0))        # Google Docs report -1: they don't use quota
        mt = _ts(o.get("ModTime", ""))
        c = core.cat_of(name)
        parent.size += size
        parent.nfiles += 1
        parent.cats[c] += size
        parent.msum += size * mt
        parent.own = max(parent.own, mt)
        if size >= core.KEEP_FILE:
            kept.setdefault(id(parent), (parent, []))[1].append((size, name, c, mt))
        else:
            parent.osize += size
            parent.ocount += 1
        st.files += 1
        st.bytes += size
        if st.files % 500 == 0:
            st.current = v["root"] + path
    proc.wait()
    if not st.cancel and proc.returncode != 0:
        raise RuntimeError("rclone couldn't list it: " + (proc.stderr.read().strip().splitlines() or ["unknown error"])[-1])
    for parent, files in kept.values():
        files.sort(reverse=True)
        for f in files[core.MAX_FILES:]:
            parent.osize += f[0]
            parent.ocount += 1
        parent.files = [(n, s, c, m) for s, n, c, m in files[:core.MAX_FILES]]

    def total(n):
        if n.own > n.mtime:
            n.mtime = n.own
        for d in n.dirs:
            total(d)
            n.size += d.size
            n.nfiles += d.nfiles
            n.msum += d.msum
            n.mtime = max(n.mtime, d.mtime)
            for i, x in enumerate(d.cats):
                n.cats[i] += x
    total(root)
    return root


# ---------------------------------------------------------------- actions
def remote_path(v, parts):
    return v["root"] + "/".join(parts)


def trash(v, parts, is_dir):
    if not v.get("trashable"):
        return False, f"Hoard only removes files from clouds that keep a trash; {v['name']} deletes permanently."
    flags = ["--drive-use-trash=true"] if v["backend"] == "drive" else []
    cmd = ["purge" if is_dir else "deletefile", remote_path(v, parts)] + flags
    try:
        r = run(cmd, timeout=600)
    except subprocess.TimeoutExpired:
        return False, "rclone took too long."
    if r.returncode == 0:
        return True, None
    return False, (r.stderr.strip().splitlines() or ["rclone failed"])[-1]


def empty_trash(v):
    try:
        r = run(["cleanup", v["root"]], timeout=600)
    except subprocess.TimeoutExpired:
        return False, "rclone took too long."
    return (True, None) if r.returncode == 0 else (False, (r.stderr.strip().splitlines() or ["rclone failed"])[-1])


def web_url(v, name):
    from urllib.parse import quote
    if v["backend"] == "drive":
        return "https://drive.google.com/drive/search?q=" + quote(name)
    if v["backend"] == "onedrive":
        return "https://onedrive.live.com/?qt=search&q=" + quote(name)
    if v["backend"] == "dropbox":
        return "https://www.dropbox.com/search/personal?query=" + quote(name)
    return None


# ---------------------------------------------------------------- duplicates from the cloud's own checksums
def dupes_start(v, parts, min_size):
    """Group files by (size, checksum) using hashes the cloud already stores (Google Drive: MD5).
    Nothing is downloaded."""
    job = {"running": True, "phase": "Listing files and their checksums", "done": 0, "total": 0, "groups": None,
           "cancel": False, "cancelled": False, "error": None, "candidates": 0, "vol": v["id"]}

    def go():
        try:
            proc = subprocess.Popen([exe(), "lsjson", "-R", "--hash", "--files-only", "--fast-list", "--no-mimetype",
                                     remote_path(v, parts)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace", creationflags=NO_WINDOW, env=plat.host_env())
            by = {}
            for line in proc.stdout:
                if job["cancel"]:
                    proc.kill()
                    job["cancelled"] = True
                    break
                line = line.strip().rstrip(",")
                if not line.startswith("{"):
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                size = int(o.get("Size") or 0)
                if size < min_size:
                    continue
                h = o.get("Hashes") or {}
                digest = h.get("md5") or h.get("sha1") or h.get("sha256") or h.get("quickxor") or h.get("dropbox")
                if not digest:
                    continue
                job["candidates"] += 1
                pp = list(parts) + o["Path"].split("/")
                by.setdefault((size, digest), []).append({"p": pp, "path": remote_path(v, pp), "m": _ts(o.get("ModTime", ""))})
            proc.wait()
            if not job["cancelled"] and proc.returncode != 0:
                raise RuntimeError((proc.stderr.read().strip().splitlines() or ["rclone failed"])[-1])
            groups = [{"size": s, "wasted": s * (len(fs) - 1), "c": core.cat_of(fs[0]["p"][-1]),
                       "files": sorted(fs, key=lambda f: f["m"])} for (s, _h), fs in by.items() if len(fs) > 1]
            job["groups"] = sorted(groups, key=lambda g: -g["wasted"])
        except Exception as ex:  # noqa: BLE001
            job["error"] = str(ex)
        finally:
            job["running"] = False
    threading.Thread(target=go, daemon=True).start()
    return job


# ---------------------------------------------------------------- setup: install rclone, connect Google Drive
JOBS = {"install": None, "connect": None}


def _job(kind, cmd):
    job = {"running": True, "ok": None, "log": "", "cmd": cmd}

    def go():
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                 errors="replace", creationflags=NO_WINDOW, env=plat.host_env())
            for line in p.stdout:
                job["log"] = (job["log"] + line)[-4000:]
            p.wait()
            job["ok"] = p.returncode == 0
        except OSError as ex:
            job["ok"], job["log"] = False, str(ex)
        job["running"] = False
        _cache["t"] = 0
    threading.Thread(target=go, daemon=True).start()
    JOBS[kind] = job
    return job


def install():
    if exe():
        return {"ok": True, "already": True}
    if plat.NAME == "windows" and shutil.which("winget"):
        _job("install", ["winget", "install", "--id", "Rclone.Rclone", "-e", "--silent",
                         "--accept-package-agreements", "--accept-source-agreements"])
        return {"ok": True, "started": True}
    if plat.NAME == "macos" and shutil.which("brew"):
        _job("install", ["brew", "install", "rclone"])
        return {"ok": True, "started": True}
    return {"ok": False, "command": "sudo apt install rclone" if plat.NAME == "linux" else "brew install rclone",
            "url": "https://rclone.org/downloads/"}


def connect_drive(name, readonly):
    """Add a Google Drive remote. rclone opens the browser for Google's sign-in and waits for it."""
    if not exe():
        return {"error": "Install rclone first."}
    if any(n == name for n, _ in remotes()):
        return {"error": f"There's already an rclone remote called “{name}”."}
    _job("connect", [exe(), "config", "create", name, "drive", "scope=" + ("drive.readonly" if readonly else "drive")])
    return {"ok": True}


def status():
    def j(x):
        return None if x is None else {k: x[k] for k in ("running", "ok", "log")}
    return {"installed": bool(exe()), "install": j(JOBS["install"]), "connect": j(JOBS["connect"]),
            "remotes": [{"name": n, "type": t, "pretty": PRETTY.get(t, t)} for n, t in remotes()] if exe() else []}
