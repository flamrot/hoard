"""Files: the two-pane file manager view (Midnight Commander style). Binding contract: review/fm-api.md.

Listing, reading and finding never change anything. Everything that changes the disk is previewed first (fm_plan)
and then runs as a job (fm_start) on one worker thread, one job at a time; size and find jobs run beside it.
System-critical items (plat.critical: path rules and file identities) and every item's identity are checked when
planning AND again right before each action, through held folder handles (safefs). Links and junctions are never
followed for a write: operations act on the link itself. Undo is checked exactly the same way.
"""
import base64
import collections
import errno
import fnmatch
import hashlib
import io
import itertools
import json
import lzma
import os
import re
import stat
import tarfile
import tempfile
import threading
import time
import traceback
import types
import uuid
import zipfile
import zlib

import core
import plat
import safefs

# Set by server.py (avoids a circular import):
#   scan(path) -> (node or None, scan time or None)   Hoard's last scan of the folder at path
#   volumes() -> [volume dicts]                         connected disks and added folders
#   changed(paths)                                      bring that scan up to date after a job changed paths
HOOKS = {"scan": None, "volumes": None, "changed": None}

MAX_ROWS, MAX_BRANCH, MAX_FIND = 200_000, 50_000, 20_000
MAX_READ = 1 << 20
CHUNK = 1 << 20
PLAN_TTL = 600
JOURNAL, PREFS, PENDING = "fm-journal.json", "fm.json", "fm-pending.json"
RUNDIR = os.path.join(core.DATA_DIR, "fm-running")       # journals of jobs still running, one line per action
NOTICES = []                                             # what startup recovery did, for the user
CHANGED = "This changed since you chose it, so Hoard left it alone."
UNDO_CHANGED = "This file changed after the operation. Hoard will not replace it."
GONE = "It's no longer there."


class FmError(Exception):
    """A plain-language refusal, returned to the client as {"error": message} (plus "code" when the client can
    offer a way out, e.g. "needs_utf8")."""
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class Stop(Exception):
    """The job was cancelled, or the user chose Abort."""


class Changed(OSError):
    """The item at a path is no longer the one that was chosen."""


def api(fn):
    def wrapper(b):
        try:
            return fn(b if isinstance(b, dict) else {})
        except FmError as ex:
            out = {"error": str(ex) or "Bad request."}
            if ex.code:
                out["code"] = ex.code
            return out
        except ValueError as ex:
            return {"error": str(ex) or "Bad request."}
        except (TypeError, KeyError, AttributeError):
            return {"error": "Bad request."}
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ---------------------------------------------------------------- paths and names
_WIN_BAD = set('<>:"/\\|?*') | {chr(i) for i in range(32)}
_WIN_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)}


def clean_abs(p):
    """An absolute path from the client, normalised. Refuses relative paths, NUL, Windows device paths and
    NTFS streams ('file:stream')."""
    if not isinstance(p, str) or not p or "\0" in p:
        raise FmError("Bad path.")
    p = plat.strip_long(p)
    if plat.NAME == "windows":
        p = p.replace("/", "\\")
        if p.startswith("\\\\.\\") or p.startswith("\\\\?\\"):
            raise FmError("Bad path.")
        drive, rest = os.path.splitdrive(p)
        if not drive or ":" in rest or not os.path.isabs(p):
            raise FmError("Bad path.")
    elif not os.path.isabs(p):
        raise FmError("Bad path.")
    return os.path.normpath(p)


def clean_name(n):
    """A single file or folder name typed by the user."""
    if not isinstance(n, str) or not n.strip() or n in (".", "..") or "\0" in n or "/" in n or os.sep in n:
        raise FmError("That name can't be used. Names can't be empty or contain / or \\.")
    if len(n.encode("utf-8", "surrogatepass")) > 255:
        raise FmError("That name is too long.")
    if plat.NAME == "windows":
        if any(c in _WIN_BAD for c in n):
            raise FmError('Names can\'t contain any of these: < > : " / \\ | ? *')
        if n.endswith((" ", ".")):
            raise FmError("Names can't end with a space or a dot.")
        if n.split(".")[0].strip().lower() in _WIN_RESERVED:
            raise FmError("Windows reserves that name. Choose another.")
    return n


def _key(p):
    return plat.path_key(p)


def _kind(st):
    if plat.is_link(st):
        return "l"
    return "d" if stat.S_ISDIR(st.st_mode) else "f"


def _same_entry(a, b):
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino) and a.st_ino != 0


def _hidden(name, st):
    return name.startswith(".") or bool(getattr(st, "st_file_attributes", 0) & 0x2)


def _plain(ex):
    """An OS error as a short sentence."""
    win = getattr(ex, "winerror", None)
    if isinstance(ex, safefs.Unsafe):
        return str(ex)
    if isinstance(ex, Changed):
        return CHANGED
    if isinstance(ex, FileNotFoundError):
        return GONE
    if isinstance(ex, FileExistsError):
        return "Something with that name is already there."
    if win == 32:
        return "Another app is using it."
    if isinstance(ex, PermissionError):
        return "Hoard isn't allowed to change this (access denied, read-only, or in use by another app)."
    if getattr(ex, "errno", None) == errno.ENOSPC or win == 112:
        return "The destination disk is full."
    if getattr(ex, "errno", None) == errno.ENOTEMPTY or win == 145:
        return "The folder isn't empty."
    return getattr(ex, "strerror", None) or str(ex) or "Something went wrong."


# ---------------------------------------------------------------- listing
def _folder_entries(folder):
    """[(name, lstat, dev, file id, allocated or None)] for one folder; never follows links.
    Windows: ids and allocation come from one directory query (os.scandir leaves ids out there)."""
    with os.scandir(folder) as it:
        entries = list(it)
    info = plat.dir_info(folder) if plat.dir_info else None
    dev = os.stat(folder).st_dev
    out = []
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if info is not None:
            fid, alloc = info.get(e.name, (0, None))
        else:
            fid, alloc = (e.inode() if plat.NAME != "windows" else 0), None
        out.append((e.name, st, st.st_dev or dev, fid, alloc))
    return out


def _row(name, st, dev, fid, alloc, why, node=None):
    k = _kind(st)
    if k == "f":
        s, a, c = st.st_size, (alloc if alloc is not None else plat.size_of(st)), core.cat_of(name)
    elif k == "d" and node is not None:
        s = a = node.size
        c = core.dominant(node.cats)
    else:
        s = a = None
        c = core.OTHER
    if plat.NAME == "windows":
        ro = bool(getattr(st, "st_file_attributes", 0) & 0x1)
    else:
        ro = not st.st_mode & stat.S_IWUSR
    return {"n": name, "k": k, "s": s, "a": a, "m": int(st.st_mtime), "c": c, "h": _hidden(os.path.basename(name), st),
            "ro": ro, "p": why, "id": [dev, fid]}


def _child_why(canon_folder, name):
    return plat.critical_key(_key(os.path.join(canon_folder, name)))


def _iter_tree(root, canon_root, job=None, hidden=True):
    """(path, relative path, lstat, dev, file id, allocated, protection reason) for everything below root.
    Never enters links. Unreadable folders are skipped."""
    stack = [(root, canon_root, "")]
    while stack:
        d, cd, rel = stack.pop()
        try:
            entries = _folder_entries(d)
        except OSError:
            continue
        for name, st, dev, fid, alloc in entries:
            if job is not None:
                job.checkpoint()
            if not hidden and _hidden(name, st):
                continue
            path, cpath = os.path.join(d, name), os.path.join(cd, name)
            yield path, rel + name, st, dev, fid, alloc, cpath
            if _kind(st) == "d":
                stack.append((path, cpath, rel + name + os.sep))


def _sort_rows(rows, sort, desc, folders_first):
    def name_key(r):
        return (r["n"].casefold(), r["n"])
    keys = {"size": lambda r: (r["s"], name_key(r)), "mtime": lambda r: (r["m"], name_key(r)),
            "ext": lambda r: (core.ext_of(r["n"]) if r["k"] != "d" else "", name_key(r))}
    keyf = keys.get(sort, name_key)
    unknown = [r for r in rows if sort == "size" and r["s"] is None]       # unknown sizes always go last
    known = [r for r in rows if not (sort == "size" and r["s"] is None)]
    known.sort(key=keyf, reverse=bool(desc))
    unknown.sort(key=name_key)
    rows = known + unknown
    if folders_first:
        rows = [r for r in rows if r["k"] == "d"] + [r for r in rows if r["k"] != "d"]
    return rows


def _crumbs(path):
    drive, rest = os.path.splitdrive(path)
    if plat.NAME == "windows":
        root = drive + "\\"
        out = [{"n": drive, "path": root}]
    else:
        root = "/"
        out = [{"n": "/", "path": "/"}]
    cur = root
    for part in [p for p in rest.split(os.sep) if p]:
        cur = os.path.join(cur, part)
        out.append({"n": part, "path": cur})
    return out


def _scan_lookup(path):
    if HOOKS["scan"] is None:
        return None, None
    try:
        node, ts = HOOKS["scan"](path)
    except Exception:  # noqa: BLE001 - sizes are a bonus; never fail a listing over them
        return None, None
    return (node if hasattr(node, "child") else None), ts


@api
def fm_roots(b):
    vols = HOOKS["volumes"]() if HOOKS["volumes"] else []
    roots = [{"path": v["root"], "name": v["name"], "label": v.get("label", ""), "free": v.get("free"),
              "total": v.get("total"), "type": v.get("type")} for v in vols if v.get("type") != "cloud"]
    return {"roots": roots, "home": os.path.expanduser("~"), "sep": os.sep, "trashName": plat.TRASH_NAME,
            "scanTime": {v["root"]: v.get("scanned") for v in vols if v.get("type") != "cloud"}}


@api
def fm_list(b):
    path = clean_abs(b.get("path"))
    hidden, branch = bool(b.get("hidden")), bool(b.get("branch"))
    folder = plat.strip_long(os.path.realpath(path))
    parent = os.path.dirname(folder)
    out = {"path": folder, "parent": None if parent == folder else parent, "crumbs": _crumbs(folder), "rows": [],
           "total": 0, "scanned": None, "truncated": False}
    if not os.path.isdir(folder):
        drive = os.path.splitdrive(folder)[0] + os.sep if plat.NAME == "windows" else None
        if drive and not os.path.exists(drive):
            out["error"] = "Location disconnected. Reconnect, then retry."
        elif os.path.isfile(folder):
            out["error"] = "That's a file, not a folder."
        else:
            out["error"] = "This folder doesn't exist anymore."
        return out
    node, ts = _scan_lookup(folder)
    out["scanned"] = ts
    rows, limit = [], MAX_BRANCH if branch else MAX_ROWS
    try:
        if branch:
            seen = set()
            for _p, rel, st, dev, fid, alloc, cpath in _iter_tree(folder, folder, hidden=hidden):
                if _kind(st) == "d":
                    continue
                if fid and (dev, fid) in seen:              # hard links to one file: list it once
                    continue
                seen.add((dev, fid))
                out["total"] += 1
                if len(rows) < limit:
                    rows.append(_row(rel, st, dev, fid, alloc, plat.critical_key(_key(cpath))))
        else:
            for name, st, dev, fid, alloc in _folder_entries(folder):
                if not hidden and _hidden(name, st):
                    continue
                out["total"] += 1
                if len(rows) < limit:
                    child = node.child(name) if node is not None and stat.S_ISDIR(st.st_mode) else None
                    rows.append(_row(name, st, dev, fid, alloc, _child_why(folder, name), child))
    except PermissionError:
        out["error"] = "Hoard cannot read this folder."
        return out
    except OSError:
        out["error"] = "Hoard cannot read this folder."
        return out
    out["truncated"] = out["total"] > len(rows)
    out["rows"] = _sort_rows(rows, b.get("sort") or "name", b.get("desc"), b.get("foldersFirst", True))
    return out


@api
def fm_check(b):
    items = []
    for p in (b.get("paths") or [])[:5000]:
        try:
            items.append({"path": p, "p": plat.critical(clean_abs(p))})
        except FmError as ex:
            items.append({"path": p, "p": str(ex)})
    return {"items": items}


# ---------------------------------------------------------------- jobs
JOBS = {}
HISTORY = collections.deque(maxlen=100)
_QUEUE = collections.deque()
_QLOCK = threading.RLock()
_WORKER = {"t": None, "seeded": False}
_TMPN = itertools.count(1)


def busy():
    """A copy/move/delete is still working: the app must not quit under it (it would leave the job half done)."""
    return any(j.mutating and j.state in ("queued", "running", "paused", "waiting") for j in list(JOBS.values()))


class Job:
    def __init__(self, op, title, mutating=True):
        self.id = uuid.uuid4().hex[:12]
        self.op, self.title, self.mutating = op, title, mutating
        self.state = "queued"
        self.done_n = self.done_b = self.total_n = self.total_b = 0
        self.current = ""
        self.question = None
        self.result = None
        self.journal = None
        self.summary = ""
        self.errors = []
        self.counts = {"done": 0, "skipped": 0, "failed": 0}
        self.total_items = 0                                # top-level items chosen, for the summary
        self.cancel = False
        self.mode, self.rule, self.skip_all, self.preserve = "ask", None, set(), True
        self.force = None
        self.created, self.finished = time.time(), None
        self.entry = None                                   # undo journal entry, once something changed
        self.touched = []                                   # paths it may change, to update Hoard's scan after
        self._go = threading.Event()
        self._go.set()
        self._answered = threading.Event()
        self._answer = None
        self._samples = collections.deque()
        self._saved = 0.0
        self._groups = 0
        self._log = None
        _history()                                          # load past history before this job writes any

    # ---- progress
    def add_bytes(self, n):
        self.done_b += n
        now = time.time()
        if not self._samples or now - self._samples[-1][0] > 0.25:
            self._samples.append((now, self.done_b))
            while self._samples and now - self._samples[0][0] > 5:
                self._samples.popleft()

    def tick(self, n=1):
        self.done_n += n

    def speed(self):
        if len(self._samples) < 2:
            return 0
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        return max(0, int((b1 - b0) / (t1 - t0))) if t1 > t0 else 0

    # ---- pause, cancel, questions
    def checkpoint(self):
        """Called between chunks and items: honours pause and cancel."""
        if self.cancel:
            raise Stop
        if not self._go.is_set():
            self.state = "paused"
            while not self._go.wait(0.2):
                if self.cancel:
                    raise Stop
            self.state = "running"
        if self.cancel:
            raise Stop

    def ask(self, kind, src, message, choices, **extra):
        """Wait for the user (fm_decide). Returns (answer, apply to all)."""
        self._answered.clear()
        self.question = dict(kind=kind, src=src, message=message, choices=list(choices), **extra)
        self.state = "waiting"
        try:
            while not self._answered.wait(0.2):
                if self.cancel:
                    raise Stop
        finally:
            self.question = None
            self.state = "running" if self._go.is_set() else "paused"
        return self._answer

    # ---- results
    def error(self, path, message):
        self.errors.append({"path": path, "message": message})

    def new_group(self):
        self._groups += 1
        return self._groups

    def record(self, action):
        """Add an undo step. It's appended to this job's log on disk at once (before, e.g., a moved file's original
        is removed), so an interrupted job can still be undone; the log becomes a journal entry at the end."""
        if self.entry is None:
            self.entry = {"id": self.id, "op": self.op, "time": int(time.time()), "summary": self.title,
                          "actions": [], "undone": False}
            os.makedirs(RUNDIR, exist_ok=True)
            self._log = open(os.path.join(RUNDIR, self.id + ".jsonl"), "a", encoding="utf-8")
            self._log.write(json.dumps({k: v for k, v in self.entry.items() if k != "actions"}) + "\n")
        self.entry["actions"].append(action)
        self._log.write(json.dumps(action) + "\n")
        self._log.flush()

    def close_log(self):
        if self._log is not None:
            self._log.close()
            self._log = None
            try:
                os.remove(os.path.join(RUNDIR, self.id + ".jsonl"))
            except OSError:
                pass

    def view(self):
        sp = self.speed()
        left = self.total_b - self.done_b
        q = self.question
        return {"id": self.id, "op": self.op, "state": self.state, "title": self.title,
                "done": {"n": min(self.done_n, self.total_n) if self.total_n else self.done_n, "bytes": self.done_b},
                "total": {"n": self.total_n, "bytes": self.total_b}, "speed": sp,
                "eta": int(left / sp) if sp > 0 and left > 0 and self.state == "running" else None,
                "current": self.current, "question": dict(q) if q else None,
                "result": _copy_result(self.result), "journal": self.journal, "summary": self.summary,
                "errors": list(self.errors[-200:]), "counts": dict(self.counts)}


def _copy_result(r):
    if not isinstance(r, dict):
        return r
    return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in r.items()}


def _submit(job, fn):
    """Queue a mutating job (they run one at a time, in order)."""
    with _QLOCK:
        JOBS[job.id] = job
        _QUEUE.append((job, fn))
        t = _WORKER["t"]
        if t is None or not t.is_alive():
            t = _WORKER["t"] = threading.Thread(target=_work, daemon=True)
            t.start()
    return job


def _work():
    while True:
        with _QLOCK:
            if not _QUEUE:
                _WORKER["t"] = None
                return
            job, fn = _QUEUE.popleft()
        _run(job, fn)


def _side(job, fn):
    """Sizes and find run beside the queue, each on its own thread."""
    with _QLOCK:
        JOBS[job.id] = job
    threading.Thread(target=_run, args=(job, fn), daemon=True).start()
    return job


def _run(job, fn):
    final = "cancelled"
    if not job.cancel:
        job.state = "running"
        try:
            fn(job)
            final = "cancelled" if job.cancel else "done"
        except Stop:
            final = "cancelled"
        except JobFailed:
            final = "failed"
        except Exception as ex:  # noqa: BLE001
            core.log_error(f"files job {job.op}", traceback.format_exc())
            job.error(job.current, _plain(ex) if isinstance(ex, OSError) else str(ex))
            final = "failed"
    job.question = None
    if job.mutating and job.touched and HOOKS["changed"]:      # even when cancelled: part of it may be done
        job.current = "Updating Hoard's scan"
        try:
            HOOKS["changed"](job.touched)
        except Exception:  # noqa: BLE001
            core.log_error("scan update after a files job", traceback.format_exc())
    job.current = ""
    if job.mutating:                          # mutation result: counts plus whatever the operation added
        job.result = dict({"result": None}, **(job.result or {}), completed=job.counts["done"],
                          skipped=job.counts["skipped"], failed=job.counts["failed"])
    job.summary = _summary(job, final)
    if job.entry is not None and job.entry["actions"]:
        job.entry["summary"] = job.summary
        if job.op not in ("delete", "undo"):
            _journal_put(job.entry)
            job.journal = job.entry["id"]
    job.close_log()                         # only after the journal entry is saved
    if job.mutating:
        hist = _history()
        for h in [h for h in hist if h["id"] == job.id]:
            hist.remove(h)
        hist.append({"id": job.id, "op": job.op, "summary": job.summary, "time": int(time.time()),
                     "undo": job.journal})
    job.finished = time.time()
    job.state = final                       # last: once a client sees the end state, the journal is saved
    _prune_jobs()


def _prune_jobs():
    """Keep finished jobs visible for ten minutes (the client reads results and summaries from them)."""
    now = time.time()
    with _QLOCK:
        for jid in [j.id for j in JOBS.values() if j.finished and now - j.finished > 600]:
            JOBS.pop(jid, None)


_DONE_WORDS = {"copy": "Copied", "move": "Moved", "rename": "Renamed", "rename_many": "Renamed", "mkdir": "Created",
               "mkfile": "Created", "delete": "Deleted", "undo": "Undid", "sizes": "Measured", "find": "Searched",
               "extract": "Extracted", "pack": "Packed", "checksum_save": "Saved", "edit_save": "Saved",
               "sync": "Synced", "compare": "Compared", "checksums": "Checked", "archive": "Read"}


def _items(n):
    return f"{n} item{'s' if n != 1 else ''}"


def _summary(job, final):
    c = job.counts
    if final == "cancelled":
        unchanged = max(0, job.total_items - c["done"])
        return f"Stopped. {_items(c['done'])} completed; {unchanged} unchanged."
    if job.op == "find":
        n = len((job.result or {}).get("rows", []))
        return f"Found {_items(n)}."
    if job.op == "trash":
        s = f"Moved {_items(c['done'])} to the {plat.TRASH_NAME}."
    elif job.op == "delete":
        s = f"Deleted {_items(c['done'])} for good."
    elif job.op == "undo":
        s = f"Undid {_items(c['done'])}."
    else:
        s = f"{_DONE_WORDS.get(job.op, 'Finished')} {_items(c['done'])}."
    bad = c["failed"] + c["skipped"]
    if final == "failed":
        s = "Stopped by an error. " + s
    if bad:
        s += f" {bad} {'was' if bad == 1 else 'were'} left unchanged."
    return s


def _history():
    """Finished jobs: this session's, seeded once from the saved undo journal."""
    if not _WORKER["seeded"]:
        _WORKER["seeded"] = True
        for e in _journal_read():
            if not e.get("undone"):
                HISTORY.append({"id": e["id"], "op": e.get("op"), "summary": e.get("summary", ""),
                                "time": e.get("time", 0), "undo": e["id"]})
    return HISTORY


@api
def fm_jobs(b):
    with _QLOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j.created)
    return {"jobs": [j.view() for j in jobs], "history": list(_history())[::-1], "notices": list(NOTICES)}


@api
def fm_control(b):
    job = JOBS.get(b.get("job"))
    if job is None:
        raise FmError("That job isn't running anymore.")
    action = b.get("action")
    if action == "pause":
        job._go.clear()
        if job.state == "running":
            job.state = "paused"
    elif action == "resume":
        job._go.set()
        if job.state == "paused":
            job.state = "running"
    elif action == "cancel":
        job.cancel = True
        job._go.set()
        with _QLOCK:
            queued = [x for x in _QUEUE if x[0] is job]
            for x in queued:
                _QUEUE.remove(x)
        if queued:
            job.state = "cancelled"
            job.finished = time.time()
            job.summary = "Cancelled before it started."
    else:
        raise FmError("Hoard doesn't know that action.")
    return {"ok": True}


@api
def fm_decide(b):
    job = JOBS.get(b.get("job"))
    q = job.question if job else None
    if q is None:
        raise FmError("There's no question waiting.")
    answer = b.get("answer")
    if answer not in q["choices"]:
        raise FmError("That answer isn't one of the choices.")
    job._answer = (answer, bool(b.get("all")))
    job._answered.set()
    return {"ok": True}


# ---------------------------------------------------------------- undo journal
_JLOCK = threading.Lock()


def _journal_read():
    data = core._read_json(JOURNAL, [])
    return data if isinstance(data, list) else []


def _journal_put(entry):
    with _JLOCK:
        items = [e for e in _journal_read() if e.get("id") != entry["id"]]
        items.append(entry)
        core._write_json(JOURNAL, items[-100:])


# ---------------------------------------------------------------- unfinished work: temp names, and recovery
def _pending_read():
    data = core._read_json(PENDING, [])
    return data if isinstance(data, list) else []


def _pending_add(rec):
    """Remember, on disk and before it happens, where a job may leave temporary names (.part copies,
    .renaming staged renames), so a crash can be cleaned up at the next start."""
    with _JLOCK:
        core._write_json(PENDING, _pending_read() + [rec])


def _pending_drop_token(token):
    with _JLOCK:
        core._write_json(PENDING, [r for r in _pending_read() if r.get("token") != token])


def _pending_drop(job_id):
    with _JLOCK:
        core._write_json(PENDING, [r for r in _pending_read() if r.get("job") != job_id])


def _clean_parts(d, job_id, job):
    """Remove this job's leftover '.hoard-<job>-N.part' files anywhere under folder d (never through links)."""
    n = 0
    for name, st in _children(job, d):
        if _kind(st) == "d":
            try:
                with d.sub(name, safefs.ident(st)) as sub:
                    n += _clean_parts(sub, job_id, job)
            except (Unsafe, OSError):
                pass
        elif _kind(st) == "f" and name.startswith(f".hoard-{job_id}-") and name.endswith(".part"):
            try:
                d.unlink(name, safefs.ident(st))
                n += 1
            except (Unsafe, OSError):
                pass
    return n


def _prune_backups():
    """Previous versions of edited files are kept only while their journal entry exists."""
    if not os.path.isdir(BACKUPS):
        return
    used = {a.get("backup") for e in _journal_read() for a in e.get("actions") or [] if a.get("t") == "edit"}
    used = {os.path.normcase(os.path.abspath(u)) for u in used if u}
    for fn in os.listdir(BACKUPS):
        p = os.path.join(BACKUPS, fn)
        if os.path.normcase(p) not in used and time.time() - os.path.getmtime(p) > 3600:
            try:
                os.remove(p)
            except OSError:
                pass


def recover():
    """At startup: turn journals of interrupted jobs into undoable history entries, remove their unfinished
    copies, and give staged renames their names back. Everything done is reported in Jobs -> History."""
    notes, job = [], Job("recover", "Cleaning up after an interrupted operation", mutating=False)
    if os.path.isdir(RUNDIR):
        for fn in sorted(os.listdir(RUNDIR)):
            jid = fn[:-6]
            if not fn.endswith(".jsonl") or jid in JOBS:
                continue
            path = os.path.join(RUNDIR, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    lines = [json.loads(x) for x in f if x.strip()]
            except (OSError, ValueError):
                lines = []
            if len(lines) > 1:
                entry = dict(lines[0], actions=lines[1:], undone=False,
                             summary="Interrupted: " + str(lines[0].get("summary", "")))
                _journal_put(entry)
                notes.append((f"Hoard was closed during “{lines[0].get('summary', '')}”. What it finished can be "
                              "undone from here.", entry["id"]))
            try:
                os.remove(path)
            except OSError:
                pass
    keep = []
    for rec in _pending_read():
        j = JOBS.get(rec.get("job"))
        if j is not None and not j.finished:          # still working on it in this session
            keep.append(rec)
            continue
        if rec.get("kind") == "parts":
            n = 0
            for folder in rec.get("dirs") or []:
                try:
                    with safefs.Dir.open(folder, guard=True) as d:
                        n += _clean_parts(d, rec["job"], job)
                except (Unsafe, OSError):
                    pass
            if n:
                notes.append((f"Removed {_items(n)} left half-copied by an interrupted operation.", None))
        elif rec.get("kind") in ("edit", "swap"):  # a replacement cut short: keep whichever version is whole
            try:
                with safefs.Dir.open(rec["dir"], guard=True) as d:
                    if d.exists(rec["tmp"]):
                        d.unlink(rec["tmp"], None)
                    if d.exists(rec["prev"]):
                        if not d.exists(rec["name"]):
                            d.rename_to(rec["prev"], d, rec["name"], guard_src=False)
                            notes.append((f"Put ‘{rec['name']}’ back after a save was interrupted.", None))
                        else:
                            notes.append((f"A save of ‘{rec['name']}’ was interrupted; the earlier version is kept "
                                          f"as ‘{rec['prev']}’ in {rec['dir']}.", None))
            except (Unsafe, OSError):
                keep.append(rec)
        elif rec.get("kind") == "rename":
            left = []
            for folder, name, tmp, new in rec.get("items") or []:
                try:
                    with safefs.Dir.open(folder, guard=True) as d:
                        if not d.exists(tmp):
                            continue
                        if d.exists(name):
                            raise FileExistsError(errno.EEXIST, "taken")
                        d.rename_to(tmp, d, name, guard_src=False)
                        notes.append((f"Gave ‘{name}’ its name back after an interrupted rename.", None))
                except (Unsafe, OSError):
                    left.append([folder, name, tmp, new])
                    notes.append((f"‘{tmp}’ in {folder} couldn't get its old name ‘{name}’ back. Rename it by hand.",
                                  None))
            if left:
                keep.append(dict(rec, items=left))
    with _JLOCK:
        core._write_json(PENDING, keep)
    _prune_backups()
    for text, jid in notes:
        _history().append({"id": uuid.uuid4().hex[:12], "op": "recover", "summary": text, "time": int(time.time()),
                           "undo": jid})
        NOTICES.append(text)
    return [t for t, _ in notes]


# ---------------------------------------------------------------- planning
OPS = ("copy", "move", "trash", "delete", "rename", "rename_many", "mkdir", "mkfile", "extract", "pack",
       "checksum_save", "edit_save")
PLANS = {}


def _expire_plans():
    now = time.time()
    for pid in [k for k, p in PLANS.items() if now - p["time"] > PLAN_TTL]:
        PLANS.pop(pid, None)


def _block(plan, path, reason):
    plan["blocked"].append({"path": path, "reason": reason})


def _sources(plan, raw, guard):
    """[(path, lstat)] for the chosen items. guard: refuse system-critical ones (they'd be removed or renamed).
    Items inside another chosen folder are dropped (the folder covers them)."""
    if not isinstance(raw, list) or not raw:
        raise FmError("Choose at least one item.")
    got, keys = [], []
    for r in raw:
        try:
            p = clean_abs(r)
        except FmError as ex:
            _block(plan, str(r), str(ex))
            continue
        try:
            st = os.lstat(p)
        except OSError:
            _block(plan, p, GONE)
            continue
        why = plat.critical(p) if guard else None
        if why:
            _block(plan, p, why)
            continue
        k = _key(plat.canonical(p))
        if k in keys:
            continue
        got.append((p, st, k))
        keys.append(k)
    return [(p, st) for p, st, k in got if not any(k.startswith(o + os.sep) for o in keys if o != k)]


def _dest(plan, raw):
    p = clean_abs(raw)
    real = plat.strip_long(os.path.realpath(p))
    if not os.path.isdir(real):
        raise FmError("The destination folder doesn't exist.")
    plan["dest"], plan["destId"] = real, _fp(real)
    return real


def _estimate(path, st, deadline):
    """Bytes for the preview: files exactly; folders from the last scan, else a quick count (None if slow)."""
    k = _kind(st)
    if k == "f":
        return st.st_size
    if k == "l":
        return 0
    node, _ts = _scan_lookup(path)
    if node is not None:
        return node.size
    total = 0
    for _p, _r, s, *_rest in _iter_tree(path, path):
        if time.time() > deadline:
            return None
        if _kind(s) == "f":
            total += s.st_size
    return total


def _namer(b, n):
    name, pattern = b.get("name"), b.get("pattern")
    if name:
        if n != 1:
            raise FmError("A new name works for one item at a time. Use a pattern with * for several.")
        clean_name(name)
        return lambda base: name
    if pattern and pattern != "*":
        if not isinstance(pattern, str) or pattern.count("*") > 1:
            raise FmError("Use at most one * in the name pattern.")
        if "*" not in pattern and n > 1:
            raise FmError("With several items, the name pattern needs a * (for example backup-* or *.bak).")
        return lambda base: clean_name(pattern.replace("*", base))
    return lambda base: base


def _plan_transfer(plan, b):
    move = plan["op"] == "move"
    dest = _dest(plan, b.get("dest"))
    dkey = _key(dest)
    srcs = _sources(plan, b.get("sources"), guard=move)
    namer = _namer(b, len(srcs))
    deadline = time.time() + 2
    verb = "moved" if move else "copied"
    for p, st in srcs:
        kind = _kind(st)
        skey = _key(plat.canonical(p))
        if kind == "d" and (dkey == skey or dkey.startswith(skey + os.sep)):
            _block(plan, p, f"A folder can't be {verb} into itself.")
            continue
        try:
            dst = os.path.join(dest, namer(os.path.basename(p)))
        except FmError as ex:
            _block(plan, p, str(ex))
            continue
        why = plat.critical(dst)                     # never write over, or into, a system-critical item
        if why:
            _block(plan, p, why)
            continue
        item = {"src": p, "dst": dst, "kind": kind, "size": _estimate(p, st, deadline), "id": _fp(p)}
        if _key(dst) == skey:
            if move:
                _block(plan, p, "It's already in that folder.")
                continue
            item["dup"] = True                       # copied next to itself: becomes "name (2)"
        elif os.path.lexists(dst):
            dst_st = os.lstat(dst)
            if kind == "d" and _kind(dst_st) == "d":
                item["merge"] = True                 # folder into an existing folder: contents are merged
            elif not _same_entry(st, dst_st):
                plan["conflicts"].append({"src": p, "dst": dst, "srcSize": st.st_size if kind == "f" else None,
                                          "dstSize": dst_st.st_size if _kind(dst_st) == "f" else None,
                                          "srcTime": int(st.st_mtime), "dstTime": int(dst_st.st_mtime)})
        plan["items"].append(item)


def _plan_remove(plan, b):
    deadline = time.time() + 2
    for p, st in _sources(plan, b.get("sources"), guard=True):
        plan["items"].append({"src": p, "kind": _kind(st), "size": _estimate(p, st, deadline), "id": _fp(p)})


def _plan_rename(plan, b):
    if plan["op"] == "rename":
        srcs = b.get("sources") or []
        if len(srcs) != 1:
            raise FmError("Rename one item at a time, or use multi-rename.")
        pairs = [[srcs[0], b.get("name")]]
    else:
        pairs = b.get("renames")
        if not isinstance(pairs, list) or not pairs:
            raise FmError("There's nothing to rename.")
    chosen = []
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise FmError("Bad request.")
        got = _sources(plan, [pair[0]], guard=True)
        if not got:
            continue
        p, st = got[0]
        try:
            new = clean_name(pair[1])
        except FmError as ex:
            if plan["op"] == "rename":
                raise                                    # a single typed name: say what's wrong with it
            _block(plan, p, str(ex))
            continue
        if new == os.path.basename(p):
            continue                                       # unchanged
        chosen.append((p, st, os.path.join(os.path.dirname(p), new)))
    src_keys = {_key(p) for p, _st, _d in chosen}
    seen = {}
    for p, st, dst in chosen:
        k, name = _key(dst), os.path.basename(dst)
        why = plat.critical(dst)
        if why:
            _block(plan, p, why)
        elif k in seen:
            _block(plan, p, f"Two items would be named ‘{name}’.")
        elif os.path.lexists(dst) and k not in src_keys and not _same_entry(st, os.lstat(dst)):
            _block(plan, p, f"‘{name}’ already exists in this folder.")
        else:
            seen[k] = p
            plan["items"].append({"src": p, "dst": dst, "kind": _kind(st), "id": _fp(p)})


def _plan_create(plan, b):
    dest = _dest(plan, b.get("dest"))
    dst = os.path.join(dest, clean_name(b.get("name")))
    why = plat.critical(dst)
    if why:
        _block(plan, dst, why)
    elif os.path.lexists(dst):
        _block(plan, dst, f"‘{os.path.basename(dst)}’ already exists in this folder.")
    else:
        plan["items"].append({"src": None, "dst": dst, "kind": "d" if plan["op"] == "mkdir" else "f", "size": 0})


_PLANNERS = {"copy": _plan_transfer, "move": _plan_transfer, "trash": _plan_remove, "delete": _plan_remove,
             "rename": _plan_rename, "rename_many": _plan_rename, "mkdir": _plan_create, "mkfile": _plan_create,
             "extract": lambda plan, b: _plan_extract(plan, b), "pack": lambda plan, b: _plan_pack(plan, b),
             "checksum_save": lambda plan, b: _plan_checksum_save(plan, b),
             "edit_save": lambda plan, b: _plan_edit_save(plan, b)}


@api
def fm_plan(b):
    op = b.get("op")
    if op not in OPS:
        raise FmError("Hoard doesn't know that operation.")
    _expire_plans()
    opts = b.get("options") if isinstance(b.get("options"), dict) else {}
    plan = {"id": uuid.uuid4().hex[:12], "op": op, "time": time.time(), "items": [], "blocked": [], "conflicts": [],
            "dest": None, "destId": None, "preserveTimes": bool(opts.get("preserveTimes", True))}
    plan["result"] = b.get("result")
    _PLANNERS[op](plan, b)
    PLANS[plan["id"]] = plan
    return _plan_view(plan)


def _plan_view(plan):
    items = [{k: v for k, v in it.items() if k in ("src", "dst", "size", "kind", "merge", "dup", "action", "replace")
              and v is not None} for it in plan["items"]]
    return {"plan": plan["id"], "op": plan["op"], "items": items, "blocked": plan["blocked"],
            "conflicts": plan["conflicts"], "total": {"n": len(items), "bytes": sum(it.get("size") or 0 for it in plan["items"])},
            "undoable": plan["op"] != "delete"}


def _title(plan, n):
    dest = plan.get("dest")
    first = os.path.basename((plan["items"][0].get("src") or plan["items"][0]["dst"])) if plan["items"] else ""
    return {"copy": f"Copying {_items(n)} to {dest}", "move": f"Moving {_items(n)} to {dest}",
            "trash": f"Moving {_items(n)} to the {plat.TRASH_NAME}", "delete": f"Deleting {_items(n)} for good",
            "rename": f"Renaming ‘{first}’", "rename_many": f"Renaming {_items(n)}",
            "mkdir": f"Creating folder ‘{first}’", "mkfile": f"Creating file ‘{first}’",
            "extract": f"Extracting {_items(n)} to {dest}", "pack": f"Creating ZIP archive {plan.get('packName')}",
            "checksum_save": f"Saving {_items(n)} checksum files", "edit_save": f"Saving ‘{first}’",
            "sync": f"Syncing {_items(n)}"}[plan["op"]]


@api
def fm_start(b):
    plan = PLANS.get(b.get("plan"))
    if plan is None or time.time() - plan["time"] > PLAN_TTL:
        raise FmError("This preview has expired. Review the operation again.")
    exclude = set()
    for x in b.get("exclude") or []:
        try:
            exclude.add(_key(clean_abs(x)))
        except FmError:
            pass
    if any(_key(bl["path"]) not in exclude for bl in plan["blocked"]):
        raise FmError("Some items are protected or can't be changed. Leave them out to continue with the rest.")
    mode = b.get("conflict") or "ask"
    if mode not in ("ask", "replace", "skip", "keep", "newer", "larger", "differs"):
        raise FmError("Hoard doesn't know that conflict rule.")
    if plan["op"] == "delete" and b.get("confirm") is not True:
        raise FmError("Deleting for good needs your confirmation.")
    items = [it for it in plan["items"] if _key(it.get("key") or it.get("src") or it["dst"]) not in exclude]
    if not items:
        raise FmError("There's nothing left to do.")
    PLANS.pop(plan["id"], None)                              # a plan authorises one run
    job = Job(plan["op"], _title(plan, len(items)))
    job.mode, job.preserve, job.total_items = mode, plan["preserveTimes"], len(items)
    job.total_n = len(items)
    job.total_b = sum(it.get("size") or 0 for it in items)
    job.touched = [it["dst"] for it in items if it.get("dst")]
    if plan["op"] in ("move", "trash", "delete", "rename", "rename_many", "sync"):     # these change the source too
        job.touched += [it["src"] for it in items if it.get("src")]
    _submit(job, lambda j: _EXEC[plan["op"]](j, plan, items))
    return {"job": job.id}


# ---------------------------------------------------------------- running operations
# Every change goes through safefs.Dir: the folder is opened through a verified chain and held, and each item is
# re-checked (identity, protection, same disk, not a link when entered) right before it is touched.
Unsafe = safefs.Unsafe
MERGE = object()


def _idl(st):
    return list(safefs.ident(st))


def _try(job, path, fn):
    """Run fn(); on an OS error ask Retry / Skip / Skip all / Abort ('Skip all' covers that kind of error).
    Safety refusals (Unsafe) are reported and skipped, never offered for retry. Returns True on success."""
    while True:
        job.checkpoint()
        try:
            fn()
            return True
        except Unsafe as ex:
            job.error(path, str(ex))
            return False
        except OSError as ex:
            msg = _plain(ex)
            cls = getattr(ex, "winerror", None) or ex.errno or type(ex).__name__
            if cls in job.skip_all:
                job.error(path, msg)
                return False
            ans, every = job.ask("error", path, msg, ["retry", "skip", "skipall", "abort"])
            if ans == "retry":
                continue
            if ans == "abort":
                raise Stop
            if ans == "skipall" or every:
                job.skip_all.add(cls)
            job.error(path, msg)
            return False


def _guarded(job, path, fn):
    """Run one top-level step; refusals and errors from opening/checking are reported. Returns fn()'s result
    (False on a refusal)."""
    try:
        return fn()
    except Unsafe as ex:
        job.error(path, str(ex))
    except FileNotFoundError:
        job.error(path, GONE)
    except OSError as ex:
        job.error(path, _plain(ex))
    return False


def _refuse_critical(*paths):
    """The full check (path rules, identities of the item and every folder above it, shares) at action time."""
    for p in paths:
        why = plat.critical(p)
        if why:
            raise Unsafe(why)


def _free_in(d, name):
    """'name (2).ext', 'name (3).ext', … : the first that doesn't exist in folder d."""
    stem, ext = os.path.splitext(name)
    if not stem:
        stem, ext = name, ""
    for i in itertools.count(2):
        cand = f"{stem} ({i}){ext}"
        if not d.exists(cand):
            return cand


def _conflict(job, src, dst, sst, dst_st):
    """'replace', 'skip' or 'keep' for an existing destination, by the job's rule or by asking."""
    rule = job.force or job.rule or job.mode           # force: a reviewed sync replacement
    if rule == "newer":
        return "replace" if sst.st_mtime > dst_st.st_mtime else "skip"
    if rule == "larger":
        return "replace" if sst.st_size > dst_st.st_size else "skip"
    if rule == "differs":
        return "replace" if sst.st_size != dst_st.st_size else "skip"
    if rule in ("replace", "skip", "keep"):
        return rule
    folder, name = os.path.split(dst)
    ans, every = job.ask("conflict", src, f"‘{name}’ already exists in {folder}.", ["replace", "skip", "keep", "abort"],
                         dst=dst, srcSize=sst.st_size if _kind(sst) == "f" else None,
                         dstSize=dst_st.st_size if _kind(dst_st) == "f" else None,
                         srcTime=int(sst.st_mtime), dstTime=int(dst_st.st_mtime))
    if ans == "abort":
        raise Stop
    if every:
        job.rule = ans
    return ans


def _backup(job, dd, name, dst_st):
    """Before replacing: send the existing item to the trash (recoverable). Returns 'ok', 'skip' or 'keep'."""
    path = dd.child(name)
    try:
        ok, err, where = dd.trash(name, safefs.ident(dst_st))
    except (Unsafe, OSError) as ex:
        job.error(path, str(ex) if isinstance(ex, Unsafe) else _plain(ex))
        return "skip"
    if ok:
        job.record({"t": "trash", "path": path, "where": where, "id": _idl(dst_st)})
        return "ok"
    ans, _every = job.ask("error", path, f"Hoard couldn't move the existing ‘{name}’ to the {plat.TRASH_NAME}, so it "
                          f"won't replace it. {err or ''}".strip(), ["skip", "keep", "abort"])
    if ans == "abort":
        raise Stop
    return ans


def _resolve_dst(job, src, sst, dd, name, move, defer=False):
    """Deal with something already at dd/name. Returns the name to write, MERGE, None (skip), or - with defer -
    a _Replace to commit after the new output is fully written (nothing is removed up front)."""
    try:
        dst_st = dd.lstat(name)
    except FileNotFoundError:
        dd.guard_new(name)
        return name
    if _kind(sst) == "d" and _kind(dst_st) == "d":
        dd.check(name, None, guard=True)               # merging into it: it must not be protected
        return MERGE
    if _same_entry(sst, dst_st):                       # the same item (only the case differs, or a hard link)
        return name if move else _free_in(dd, name)
    choice = _conflict(job, src, dd.child(name), sst, dst_st)
    if choice == "replace" and defer:
        if _kind(sst) != _kind(dst_st):
            job.error(dd.child(name), "Something of another kind is in the way (a folder where a file would go, "
                                      "or the other way round). Choose Keep both or Skip.")
            return None
        dd.check(name, safefs.ident(dst_st), guard=True)
        return _Replace(name, dst_st)
    if choice == "replace":
        choice = _backup(job, dd, name, dst_st)
    if choice == "skip":
        return None
    return _free_in(dd, name) if choice == "keep" else name


def _copy_file(job, sd, sname, sst, dd, dname):
    """Chunked copy into a hidden temp file next to the destination, then a no-replace rename: a cancelled or
    failed copy never leaves a partial file under the real name, and never overwrites anything."""
    tmp = f".hoard-{job.id}-{next(_TMPN)}.part"
    written = [0]

    def attempt():
        written[0] = 0
        made = False
        try:
            fi, _fst = sd.open_read(sname, safefs.ident(sst))
            with fi:
                fo = dd.create(tmp)
                made = True
                with fo:
                    while True:
                        job.checkpoint()
                        chunk = fi.read(CHUNK)
                        if not chunk:
                            break
                        fo.write(chunk)
                        written[0] += len(chunk)
                        job.add_bytes(len(chunk))
            if job.preserve:
                try:
                    dd.utime(tmp, (sst.st_atime_ns, sst.st_mtime_ns))
                except OSError:
                    pass
            dd.rename_to(tmp, dd, dname, guard_src=False)
        except BaseException:
            job.add_bytes(-written[0])
            if made:
                try:
                    dd.unlink(tmp, None)
                except (OSError, Unsafe):
                    pass
            raise
    return _try(job, sd.child(sname), attempt)


def _rmdir_left(job, d, name, st, why):
    """Remove a folder emptied by a move or delete; if things stayed inside, say so (no question)."""
    try:
        d.rmdir(name, safefs.ident(st))
    except Unsafe as ex:
        job.error(d.child(name), str(ex))
    except OSError:
        if d.exists(name):
            job.error(d.child(name), why)


def _children(job, d):
    """(name, lstat) of a held folder's contents; unreadable folders are reported."""
    try:
        names = d.names()
    except OSError as ex:
        job.error(d.path, _plain(ex))
        return []
    out = []
    for n in names:
        try:
            out.append((n, d.lstat(n)))
        except OSError:
            pass
    return out


def _transfer(job, sd, sname, sexp, dd, dname, move, group=None, record=True):
    """Copy (or move) sd/sname to dd/dname. Moves rename on the same disk, otherwise copy each file, journal it,
    then remove its source. Copies journal what they create (folders as a group, for a careful undo).
    Returns True when something was written."""
    job.checkpoint()
    src = sd.child(sname)
    job.current = src
    sst = sd.check(sname, sexp, guard=move)
    kind = _kind(sst)
    target = _resolve_dst(job, src, sst, dd, dname, move)
    if target is None:
        job.tick()
        return False
    if target is MERGE:
        with sd.sub(sname, safefs.ident(sst), guard=move) as ss, dd.sub(dname, guard=True) as ds:
            for c, cst in _children(job, ss):
                _guarded(job, ss.child(c),
                         lambda c=c, cst=cst: _transfer(job, ss, c, safefs.ident(cst), ds, c, move, None, record))
        if move:
            _rmdir_left(job, sd, sname, sst, "Some items couldn't be moved, so they're still in the original folder.")
        return True
    dname = target
    if move and _same_disk_dirs(sd, dd):
        other_disk = []

        def rename():
            try:
                sd.rename_to(sname, dd, dname, safefs.ident(sst), guard_src=True)
            except OSError as ex:
                if ex.errno != errno.EXDEV and getattr(ex, "winerror", None) != 17:
                    raise
                other_disk.append(True)              # e.g. a bind mount: copy instead
        if not _try(job, src, rename):
            job.tick()
            return False
        if not other_disk:
            if record:
                job.record({"t": "move", "from": src, "to": dd.child(dname), "id": _idl(dd.lstat(dname))})
            job.tick()
            job.add_bytes(sst.st_size if kind == "f" else 0)
            return True
    if kind == "d":
        if not _try(job, dd.child(dname), lambda: dd.mkdir(dname)):
            return False
        nst = dd.lstat(dname)
        if record:
            act = {"t": "created", "path": dd.child(dname), "id": _idl(nst), "kind": "d"}
            if not move:
                if group is None:
                    group = act["g"] = job.new_group()
                    act["top"] = True
                else:
                    act["g"] = group
            job.record(act)
        with sd.sub(sname, safefs.ident(sst), guard=move) as ss, dd.sub(dname, safefs.ident(nst), guard=True) as ds:
            for c, cst in _children(job, ss):
                _guarded(job, ss.child(c),
                         lambda c=c, cst=cst: _transfer(job, ss, c, safefs.ident(cst), ds, c, move, group, record))
        if job.preserve:
            try:
                dd.utime(dname, (sst.st_atime_ns, sst.st_mtime_ns))
            except OSError:
                pass
        if move:
            _rmdir_left(job, sd, sname, sst, "Some items couldn't be moved, so they're still in the original folder.")
        return True
    if kind == "f":
        ok = _copy_file(job, sd, sname, sst, dd, dname)
    else:
        ok = _try(job, src, lambda: dd.make_link(sd.readlink(sname), dname, sst))
    job.tick()
    if not ok:
        return False
    nst = dd.lstat(dname)
    if record:
        if move:                                      # journaled (on disk) before the original is removed
            job.record({"t": "move", "from": src, "to": dd.child(dname), "id": _idl(nst)})
        else:
            act = {"t": "created", "path": dd.child(dname), "id": _idl(nst), "kind": kind, "size": nst.st_size,
                   "mtime": nst.st_mtime_ns}
            if group is not None:
                act["g"] = group
            job.record(act)
    if move:
        now = sd.lstat(sname)
        if not safefs.same_id(now, safefs.ident(sst)) or (now.st_size, now.st_mtime_ns) != (sst.st_size, sst.st_mtime_ns):
            job.error(src, "It changed while being moved, so the original was kept.")
        else:
            _try(job, src, lambda: sd.unlink(sname, safefs.ident(sst)))
    return True


def _same_disk_dirs(a, b):
    """Can a rename move things between these held folders? (Tests replace this to take the copy path.)"""
    return a.st.st_dev == b.st.st_dev


def _exec_transfer(job, plan, items):
    move = plan["op"] == "move"
    job.current = "Counting…"
    total_n = total_b = 0
    for it in items:
        try:
            st = os.lstat(it["src"])
        except OSError:
            continue
        if move and _same_disk(it["src"], plan["dest"]):
            n, size = 1, (it.get("size") or 0)
        else:
            n, size = _measure(job, it["src"], st)
        total_n, total_b = total_n + n, total_b + size
    job.total_n, job.total_b = total_n, total_b
    _pending_add({"job": job.id, "kind": "parts", "dirs": [plan["dest"]]})
    try:
        try:
            dd = safefs.Dir.open(plan["dest"], guard=True, expect=plan["destId"])
        except (Unsafe, OSError) as ex:
            job.error(plan["dest"], "The destination folder changed since you chose it, so Hoard stopped."
                      if not isinstance(ex, Unsafe) or str(ex) == safefs.CHANGED else str(ex))
            job.counts["failed"] += len(items)
            return
        with dd:
            for it in items:
                job.checkpoint()
                ok = _guarded(job, it["src"], lambda it=it: _transfer_top(job, it, dd, move))
                job.counts["done" if ok else "skipped"] += 1
    finally:
        _pending_drop(job.id)


def _transfer_top(job, it, dd, move):
    if move:
        _refuse_critical(it["src"])
    sd, sname = safefs.open_parent(it["src"], guard=move)
    with sd:
        dname = os.path.basename(it["dst"])
        if it.get("dup"):
            dname = _free_in(dd, dname)
        _refuse_critical(dd.child(dname))
        return _transfer(job, sd, sname, it["id"], dd, dname, move)


def _count(path, st):
    """(files and links) under a folder; 1 for anything else."""
    if _kind(st) != "d":
        return 1
    return sum(1 for _p, _r, s, *_x in _iter_tree(path, path) if _kind(s) != "d")


def _measure(job, path, st):
    """(items, bytes) a copy will process."""
    if _kind(st) != "d":
        return 1, (st.st_size if _kind(st) == "f" else 0)
    n = size = 0
    for _p, _r, s, *_x in _iter_tree(path, path, job):
        if _kind(s) != "d":
            n += 1
            size += s.st_size if _kind(s) == "f" else 0
    return n, size


def _same_disk(src, dest_folder):
    try:
        return os.lstat(src).st_dev == os.stat(dest_folder).st_dev
    except OSError:
        return False


def _exec_trash(job, plan, items):
    for it in items:
        job.checkpoint()
        job.current = it["src"]
        ok = _guarded(job, it["src"], lambda it=it: _trash_top(job, it))
        job.counts["done" if ok else "skipped"] += 1
        job.tick()


def _trash_top(job, it):
    _refuse_critical(it["src"])
    d, name = safefs.open_parent(it["src"])
    with d:
        got = {}

        def act():
            ok, err, where = d.trash(name, it["id"])
            if not ok:
                raise OSError(0, f"Couldn't move it to the {plat.TRASH_NAME}: {err}" if err else
                              f"Couldn't move it to the {plat.TRASH_NAME}.")
            got["where"] = where
        if not _try(job, it["src"], act):
            return False
        job.record({"t": "trash", "path": d.child(name), "where": got["where"], "id": it["id"]})
    job.add_bytes(it.get("size") or 0)
    return True


def _delete_in(job, d, name, expect):
    """Permanent delete of d/name. Every item is re-checked before removal; folders are entered only through
    held handles, never through a link or into another disk; protected items inside are refused and reported."""
    job.checkpoint()
    path = d.child(name)
    job.current = path
    st = d.check(name, expect)
    if _kind(st) == "d":
        with d.sub(name, safefs.ident(st)) as sub:
            for c, cst in _children(job, sub):
                _guarded(job, sub.child(c), lambda c=c, cst=cst: _delete_in(job, sub, c, safefs.ident(cst)))
        _rmdir_left(job, d, name, st, "Some items inside were left, so the folder stays.")
        return
    if _try(job, path, lambda: d.unlink(name, safefs.ident(st))):
        job.add_bytes(st.st_size if _kind(st) == "f" else 0)
    job.tick()


def _exec_delete(job, plan, items):
    job.current = "Counting…"
    total = 0
    for it in items:
        try:
            total += _count(it["src"], os.lstat(it["src"]))
        except OSError:
            pass
    job.total_n = total
    for it in items:
        job.checkpoint()

        def top(it=it):
            _refuse_critical(it["src"])
            d, name = safefs.open_parent(it["src"])
            with d:
                _delete_in(job, d, name, it["id"])
                return not d.exists(name)
        ok = _guarded(job, it["src"], top)
        job.counts["done" if ok else "failed"] += 1


def _two_phase(job, steps, rollback_label):
    """Renames in two steps (all to temporary names, then to the new ones) so swaps and chains (a->b, b->c) work.
    steps: [{"d", "name", "new", "expect", "done": callback}]. The plan is saved to disk before anything moves;
    whatever happens (error, cancel, abort), every item not finished goes back to its old name, and any that
    can't is reported (and restored at the next start)."""
    for i, s in enumerate(steps):
        s["tmp"] = f".hoard-{job.id}-{i}.renaming"
    _pending_add({"job": job.id, "kind": "rename",
                  "items": [[s["d"].path, s["name"], s["tmp"], s["new"]] for s in steps]})
    staged, finished, stuck = [], set(), False
    try:
        for s in steps:
            job.checkpoint()
            path = s["d"].child(s["name"])
            if _try(job, path, lambda s=s: s["d"].rename_to(s["name"], s["d"], s["tmp"], s["expect"])):
                staged.append(s)
            else:
                job.counts["skipped"] += 1
        for s in staged:
            job.checkpoint()
            path = s["d"].child(s["name"])
            job.current = path

            def finish(s=s):
                if s["d"].exists(s["new"]):
                    raise Unsafe(f"‘{s['new']}’ already exists in this folder. Hoard will not replace it.")
                s["d"].rename_to(s["tmp"], s["d"], s["new"], guard_src=False)
            if _try(job, path, finish):
                finished.add(id(s))
                s["done"](s)
            job.tick()
    finally:
        for s in staged:
            if id(s) in finished:
                continue
            try:
                s["d"].rename_to(s["tmp"], s["d"], s["name"], guard_src=False)
                job.counts["skipped"] += 1
            except (OSError, Unsafe) as ex:
                stuck = True
                job.error(s["d"].child(s["name"]), f"{rollback_label} It's now named {s['tmp']}: "
                          f"{str(ex) if isinstance(ex, Unsafe) else _plain(ex)}")
        if not stuck:
            _pending_drop(job.id)


def _exec_rename(job, plan, items):
    if plan["op"] == "rename":
        it = items[0]

        def one():
            _refuse_critical(it["src"], it["dst"])
            d, name = safefs.open_parent(it["src"])
            with d:
                new = os.path.basename(it["dst"])
                if not _try(job, it["src"], lambda: d.rename_to(name, d, new, it["id"])):
                    return False
                job.record({"t": "move", "from": d.child(name), "to": d.child(new), "id": _idl(d.lstat(new))})
                return True
        ok = _guarded(job, it["src"], one)
        job.counts["done" if ok else "skipped"] += 1
        job.tick()
        return
    dirs, steps = {}, []
    try:
        for it in items:
            def prepare(it=it):
                _refuse_critical(it["src"], it["dst"])
                folder, name = os.path.split(it["src"])
                if folder not in dirs:
                    dirs[folder] = safefs.Dir.open(folder, guard=True)
                return {"d": dirs[folder], "name": name, "new": os.path.basename(it["dst"]), "expect": it["id"]}
            s = _guarded(job, it["src"], prepare)
            if s:
                steps.append(s)
            else:
                job.counts["failed"] += 1

        def done(s):
            d = s["d"]
            job.record({"t": "move", "from": d.child(s["name"]), "to": d.child(s["new"]), "id": _idl(d.lstat(s["new"]))})
            job.counts["done"] += 1
        for s in steps:
            s["done"] = done
        if steps:
            _two_phase(job, steps, "Hoard couldn't give it back its old name.")
    finally:
        for d in dirs.values():
            d.close()


def _exec_create(job, plan, items):
    it = items[0]

    def one():
        _refuse_critical(it["dst"])
        with safefs.Dir.open(plan["dest"], guard=True, expect=plan["destId"]) as d:
            name = os.path.basename(it["dst"])
            if not _try(job, it["dst"], lambda: d.mkdir(name) if it["kind"] == "d" else d.create(name).close()):
                return False
            st = d.lstat(name)
            job.record({"t": "created", "path": d.child(name), "id": _idl(st), "kind": it["kind"],
                        "size": st.st_size, "mtime": st.st_mtime_ns})
            return True
    ok = _guarded(job, it["dst"], one)
    job.counts["done" if ok else "skipped"] += 1
    job.tick()


_EXEC = {"copy": _exec_transfer, "move": _exec_transfer, "trash": _exec_trash, "delete": _exec_delete,
         "rename": _exec_rename, "rename_many": _exec_rename, "mkdir": _exec_create, "mkfile": _exec_create,
         "extract": lambda *a: _exec_extract(*a), "pack": lambda *a: _exec_pack(*a),
         "checksum_save": lambda *a: _exec_checksum_save(*a), "edit_save": lambda *a: _exec_edit_save(*a),
         "sync": lambda *a: _exec_sync(*a)}


# ---------------------------------------------------------------- undo
# Same validation as going forward: protection (path and identity, both ends), held folders, identities,
# never through links, never replacing anything. Each action's outcome is saved; failed ones stay retryable.
ORIGINAL_TAKEN = "Something else is at the original place now. Hoard will not replace it."


def _expect_unchanged(d, name, a):
    try:
        st = d.check(name, a.get("id"))
    except Unsafe as ex:
        raise Unsafe(UNDO_CHANGED if str(ex) == safefs.CHANGED else str(ex))
    if a.get("kind") in ("f", "l") and a.get("size") is not None and \
            (st.st_size, st.st_mtime_ns) != (a.get("size"), a.get("mtime")):
        raise Unsafe(UNDO_CHANGED)
    return st


def _undo_move(job, a):
    src, dst = a["to"], a["from"]
    _refuse_critical(src, dst)
    sd, sname = safefs.open_parent(src, strict=True)
    with sd:
        sst = _expect_unchanged(sd, sname, {"id": a.get("id")})
        with safefs.ensure_dir(os.path.dirname(dst), strict=True) as dd:
            dname = os.path.basename(dst)
            if dd.exists(dname):
                raise Unsafe(ORIGINAL_TAKEN)
            if _same_disk_dirs(sd, dd):
                return _try(job, src, lambda: sd.rename_to(sname, dd, dname, safefs.ident(sst)))
            return _transfer(job, sd, sname, safefs.ident(sst), dd, dname, move=True, record=False)


def _undo_created(job, a, op):
    _refuse_critical(a["path"])
    d, name = safefs.open_parent(a["path"], strict=True)
    with d:
        st = _expect_unchanged(d, name, a)
        if a["kind"] == "d":
            try:
                d.rmdir(name, safefs.ident(st))
                return True
            except OSError:
                raise Unsafe("It isn't empty anymore. Hoard will not remove it.")
        if op == "mkfile":
            return _try(job, a["path"], lambda: d.unlink(name, safefs.ident(st)))
        ok, err, _where = d.trash(name, safefs.ident(st))     # copies go to the trash, never straight to deletion
        if not ok:
            raise OSError(0, err or f"Couldn't move it to the {plat.TRASH_NAME}.")
        return True


def _untrash(job, a):
    where, orig = a.get("where"), a["path"]
    gone = f"Hoard couldn't find it in the {plat.TRASH_NAME} anymore."
    if not where:
        raise Unsafe(f"Hoard doesn't know where the {plat.TRASH_NAME} put it, so it can't put it back.")
    _refuse_critical(orig)
    try:
        sd, sname = safefs.open_parent(where, guard=False, strict=True)
    except (FileNotFoundError, Unsafe):
        raise Unsafe(gone)
    with sd:
        try:
            sst = sd.check(sname, a.get("id"), guard=False)
        except (FileNotFoundError, Unsafe):
            raise Unsafe(gone)
        with safefs.ensure_dir(os.path.dirname(orig), strict=True) as dd:
            dname = os.path.basename(orig)
            if dd.exists(dname):
                raise Unsafe("Something else is at that place now. Hoard will not replace it.")
            if not _try(job, orig, lambda: sd.rename_to(sname, dd, dname, safefs.ident(sst), guard_src=False)):
                return False
    meta = plat.trash_meta(where)
    if meta:
        try:
            md, mname = safefs.open_parent(meta, guard=False)
            with md:
                md.unlink(mname, None)
        except (OSError, Unsafe):
            pass
    return True


def _scan_tree(d, rel, out, job):
    """rel path -> lstat for everything under a held folder (not entering links or other disks)."""
    for name, st in _children(job, d):
        r = os.path.join(rel, name) if rel else name
        out[r] = st
        if _kind(st) == "d":
            try:
                with d.sub(name, safefs.ident(st), guard=False) as sub:
                    _scan_tree(sub, r, out, job)
            except (Unsafe, OSError):
                out[r] = None                           # can't look inside: treat as changed


def _undo_group(job, acts):
    """Undo a copied folder: if everything in it is exactly what was copied, trash the folder; otherwise trash
    only the unchanged copies, remove folders left empty, and leave (and report) whatever changed or was added."""
    top = next((a for a in acts if a.get("top")), None)
    if top is None:
        return
    base = top["path"]
    _refuse_critical(base)
    d, name = safefs.open_parent(base, strict=True)
    with d:
        tst = _expect_unchanged(d, name, top)
        current = {}
        with d.sub(name, safefs.ident(tst)) as sub:
            _scan_tree(sub, "", current, job)
        rec = {os.path.relpath(a["path"], base): a for a in acts if not a.get("top")}

        def same(a, st):
            return st is not None and safefs.same_id(st, a["id"]) and \
                (a["kind"] == "d" or (st.st_size, st.st_mtime_ns) == (a.get("size"), a.get("mtime")))
        if set(current) == set(rec) and all(same(rec[r], current[r]) for r in rec):
            ok, err, _w = d.trash(name, safefs.ident(tst))
            if not ok:
                raise OSError(0, err or f"Couldn't move it to the {plat.TRASH_NAME}.")
            for a in acts:
                a["ok"] = True
            job.counts["done"] += 1
            return
    for r in sorted(set(current) - set(rec)):
        job.error(os.path.join(base, r), "This was added after the copy, so Hoard left it.")
    for r, a in sorted(rec.items(), key=lambda kv: -kv[0].count(os.sep)):        # deepest first
        if a.get("ok"):
            continue
        if r not in current:                            # already gone: nothing left to undo
            a["ok"] = True
            continue
        if a["kind"] != "d" and not same(a, current[r]):
            job.error(a["path"], UNDO_CHANGED)
            continue
        if _guarded(job, a["path"], lambda a=a: _undo_created(job, a, "copy") if a["kind"] != "d" else _rmdir_created(a)):
            a["ok"] = True
    if _guarded(job, base, lambda: _rmdir_created(top)):
        top["ok"] = True
        job.counts["done"] += 1
    else:
        job.counts["skipped"] += 1


def _rmdir_created(a):
    _refuse_critical(a["path"])
    d, name = safefs.open_parent(a["path"], strict=True)
    with d:
        st = _expect_unchanged(d, name, a)
        try:
            d.rmdir(name, safefs.ident(st))
        except OSError:
            raise Unsafe("Some things in this folder changed or were added, so Hoard left it.")
    return True


def _exec_undo(job, entry):
    acts = [a for a in entry.get("actions") or [] if not a.get("ok")]
    job.total_n = job.total_items = len(acts)
    try:
        if entry.get("op") == "rename_many":
            _undo_renames(job, acts)
        else:
            groups = set()
            for a in reversed(acts):
                job.checkpoint()
                job.current = a.get("path") or a.get("to")
                if a.get("g") is not None:
                    if a["g"] not in groups:
                        groups.add(a["g"])
                        _guarded(job, job.current, lambda g=a["g"]: _undo_group(job, [x for x in acts if x.get("g") == g]))
                    continue
                if a["t"] == "move":
                    ok = _guarded(job, a["to"], lambda a=a: _undo_move(job, a))
                elif a["t"] == "created":
                    ok = _guarded(job, a["path"], lambda a=a: _undo_created(job, a, entry.get("op")))
                elif a["t"] == "trash":
                    ok = _guarded(job, a["path"], lambda a=a: _untrash(job, a))
                elif a["t"] == "edit":
                    ok = _guarded(job, a["path"], lambda a=a: _undo_edit(job, a))
                else:
                    ok = False
                if ok:
                    a["ok"] = True
                job.counts["done" if ok else "skipped"] += 1
                job.tick()
                if time.time() - job._saved > 1:
                    _journal_put(entry)
                    job._saved = time.time()
    finally:
        entry["undone"] = all(a.get("ok") for a in entry.get("actions") or [])
        _journal_put(entry)
        if entry["undone"]:
            for h in _history():
                if h.get("undo") == entry["id"]:
                    h["undo"] = None


def _undo_renames(job, acts):
    dirs, steps = {}, []
    try:
        for a in acts:
            def prepare(a=a):
                _refuse_critical(a["to"], a["from"])
                folder = os.path.dirname(a["to"])
                if folder not in dirs:
                    dirs[folder] = safefs.Dir.open(folder, guard=True, strict=True)
                d = dirs[folder]
                _expect_unchanged(d, os.path.basename(a["to"]), {"id": a.get("id")})
                return {"d": d, "name": os.path.basename(a["to"]), "new": os.path.basename(a["from"]),
                        "expect": a.get("id"), "a": a}
            s = _guarded(job, a["to"], prepare)
            if s:
                steps.append(s)
            else:
                job.counts["skipped"] += 1

        def done(s):
            s["a"]["ok"] = True
            job.counts["done"] += 1
        for s in steps:
            s["done"] = done
        if steps:
            _two_phase(job, steps, "Hoard couldn't finish undoing the rename.")
    finally:
        for d in dirs.values():
            d.close()


@api
def fm_undo(b):
    jid = b.get("journal")
    entry = next((e for e in _journal_read() if e.get("id") == jid), None)
    if entry is None:
        raise FmError("Hoard has no record of that operation anymore, so it can't undo it.")
    if entry.get("undone"):
        raise FmError("That was already undone.")
    if not entry.get("actions"):
        raise FmError("There's nothing to undo.")
    if any(j.id == jid and j.state in ("queued", "running", "paused", "waiting") for j in JOBS.values()) or \
            any(j.op == "undo" and getattr(j, "undoing", None) == jid and not j.finished for j in JOBS.values()):
        raise FmError("Wait for that operation to finish first.")
    job = Job("undo", f"Undoing: {entry.get('summary', '')}")
    job.undoing = jid
    job.touched = [a[k] for a in entry["actions"] for k in ("from", "to", "path") if a.get(k)]
    job.total_items = len([a for a in entry["actions"] if not a.get("ok")])
    _submit(job, lambda j: _exec_undo(j, entry))
    return {"job": job.id}


# ---------------------------------------------------------------- side jobs: sizes and find
@api
def fm_sizes(b):
    paths = [clean_abs(p) for p in (b.get("paths") or [])[:1000]]
    if not paths:
        raise FmError("Choose at least one folder.")
    job = Job("sizes", f"Calculating {_items(len(paths))}", mutating=False)
    job.total_n = len(paths)
    job.result = {"sizes": {}}

    def run(job):
        for p in paths:
            job.current = p
            total = 0
            try:
                st = os.lstat(p)
            except OSError:
                job.error(p, GONE)
                continue
            if _kind(st) == "d":
                for _x, _r, s, *_rest in _iter_tree(p, p, job):
                    if _kind(s) != "d":
                        total += plat.size_of(s)
                        job.add_bytes(plat.size_of(s))
            elif _kind(st) == "f":
                total = plat.size_of(st)
            job.result["sizes"][p] = total
            job.counts["done"] += 1
            job.tick()
    return {"job": _side(job, run).id}


def _name_matcher(spec, case):
    pats = [p.strip() for p in (spec or "").split(";") if p.strip()]
    if not pats:
        return lambda n: True
    if not case:
        pats = [p.casefold() for p in pats]

    def match(name):
        n = name if case else name.casefold()
        for p in pats:
            if any(c in p for c in "*?["):
                if fnmatch.fnmatchcase(n, p):
                    return True
            elif p in n:
                return True
        return False
    return match


def _text_matcher(text, regex, case):
    if not text:
        return None, 0
    raw = text.encode("utf-8")
    try:
        rx = re.compile(raw if regex else re.escape(raw), 0 if case else re.IGNORECASE)
    except re.error as ex:
        raise FmError(f"That pattern isn't a valid regular expression: {ex}")
    return rx, (4096 if regex else len(raw) - 1)


def _file_contains(job, path, rx, overlap):
    """Search a file in 1 MB chunks (with overlap so matches across chunk edges are found)."""
    try:
        with open(path, "rb") as f:
            tail = b""
            while True:
                job.checkpoint()
                chunk = f.read(CHUNK)
                if not chunk:
                    return False
                buf = tail + chunk
                if rx.search(buf):
                    return True
                tail = buf[-overlap:] if overlap > 0 else b""
    except OSError:
        return False


@api
def fm_find(b):
    start = clean_abs(b.get("start"))
    if not os.path.isdir(start):
        raise FmError("That folder doesn't exist.")
    case, hidden = bool(b.get("case")), bool(b.get("hidden"))
    name_ok = _name_matcher(b.get("name"), case)
    rx, overlap = _text_matcher(b.get("text") or "", bool(b.get("regex")), case)
    canon = plat.strip_long(os.path.realpath(start))
    job = Job("find", f"Finding in {start}", mutating=False)
    rows = []
    job.result = {"rows": rows, "truncated": False}

    def run(job):
        for path, rel, st, dev, fid, alloc, cpath in _iter_tree(start, canon, job, hidden=hidden):
            job.current = path
            job.tick()
            if not name_ok(os.path.basename(rel)):
                continue
            if rx is not None:
                if not stat.S_ISREG(st.st_mode) or plat.is_link(st):
                    continue
                job.add_bytes(st.st_size)
                if not _file_contains(job, path, rx, overlap):
                    continue
            row = _row(rel, st, dev, fid, alloc, plat.critical_key(_key(cpath)))
            row["path"] = path
            rows.append(row)
            if len(rows) >= MAX_FIND:
                job.result["truncated"] = True
                return
    return {"job": _side(job, run).id}


# ---------------------------------------------------------------- viewer reads and preferences
def open_for_read(path):
    """Shared by fm_read and /fmfile: a plain file reached without any link on the way (links in folders above it
    are refused too), opened without following a link and verified (fstat) to be the file that was checked.
    Returns (file object, fstat); raises FmError."""
    path = clean_abs(path)
    try:
        return safefs.open_file_for_read(path)
    except safefs.Unsafe as ex:
        msg = str(ex)
        raise FmError("That's a link, or reached through one. Hoard doesn't open links."
                      if msg == safefs.LINK else msg)
    except FileNotFoundError:
        raise FmError("That file doesn't exist anymore.")
    except OSError as ex:
        raise FmError(f"Hoard can't read that file. {_plain(ex)}")


@api
def fm_read(b):
    try:
        offset = int(b.get("offset") or 0)
        length = int(b.get("length") if b.get("length") is not None else MAX_READ)
    except (TypeError, ValueError):
        raise FmError("Bad range.")
    if offset < 0 or length < 0:
        raise FmError("Bad range.")
    length = min(length, MAX_READ)
    f, st = open_for_read(b.get("path"))
    with f:
        try:
            f.seek(offset)
            data = f.read(length)
        except OSError as ex:
            raise FmError(f"Hoard can't read that file. {_plain(ex)}")
    return {"data": base64.b64encode(data).decode("ascii"), "size": st.st_size, "eof": offset + len(data) >= st.st_size}


PREF_DEFAULTS = {"left": {"tabs": [], "active": 0}, "right": {"tabs": [], "active": 0}, "hotlist": [],
                 "history": {"left": [], "right": []}, "density": "compact", "hidden": False}


def _load_prefs():
    stored = core._read_json(PREFS, {})
    prefs = json.loads(json.dumps(PREF_DEFAULTS))
    if isinstance(stored, dict):
        prefs.update({k: v for k, v in stored.items() if k in PREF_DEFAULTS and type(v) is type(PREF_DEFAULTS[k])})
    return prefs


@api
def fm_prefs(b):
    prefs = _load_prefs()
    new = b.get("set")
    if isinstance(new, dict):
        for k, v in new.items():
            if k in PREF_DEFAULTS and type(v) is type(PREF_DEFAULTS[k]):
                prefs[k] = v
        hist = prefs.get("history")
        if isinstance(hist, dict):
            prefs["history"] = {s: list(v)[-100:] for s, v in hist.items() if s in ("left", "right") and isinstance(v, list)}
        if isinstance(prefs.get("hotlist"), list):
            prefs["hotlist"] = prefs["hotlist"][:200]
        if len(json.dumps(prefs)) > 512 * 1024:
            raise FmError("Those settings are too large to save.")
        core._write_json(PREFS, prefs)
    return {"prefs": prefs}


# ================================================================ round two: results, compare, sync, archives,
# checksums and the editor. Reads are side jobs; every write is a plan run by fm_start through the same checks.
RESULTS = collections.OrderedDict()              # result id -> stored rows for this session (paged by fm_results)
MAX_RESULT_ROWS = 200_000
TIME_SLACK = 2                                    # seconds: FAT keeps modified times to 2 s


def _store(res):
    rid = uuid.uuid4().hex[:12]
    res.setdefault("tags", set())
    res["time"] = time.time()
    with _QLOCK:
        RESULTS[rid] = res
        while len(RESULTS) > 30:
            old = RESULTS.popitem(last=False)[1]
            _drop_previews(old)
    return rid


def _result(rid, kind=None):
    res = RESULTS.get(rid)
    if res is None or (kind and res["kind"] != kind):
        raise FmError("Those results aren't available anymore. Run it again.")
    return res


def _public_row(res, row):
    out = {k: v for k, v in row.items() if not k.startswith("_")}
    out["tagged"] = row["id"] in res["tags"]
    return out


DIFFERENT = ("only_left", "only_right", "newer_left", "newer_right", "different")


def _differing_file(row):
    side = row.get("left") or row.get("right")
    return row["status"] in DIFFERENT and not row.get("_conflict") and side is not None and side["k"] == "f" and \
        all(r is None or r["k"] == "f" for r in (row.get("left"), row.get("right")))


@api
def fm_results(b):
    """Page through stored results. Tags and sync choices live here, server-side; each filter's row index is
    built once and reused for later pages."""
    res = _result(b.get("result"))
    rows = res["rows"]
    views = res.setdefault("_views", {})
    action = b.get("tagAction")
    ids = {int(i) for i in (b.get("tagIds") or []) if isinstance(i, int) or str(i).isdigit()}
    if action is not None:
        if action == "set":
            res["tags"] = set(ids)
        elif action == "add":
            res["tags"] |= ids
        elif action == "remove":
            res["tags"] -= ids
        elif action == "clear":
            res["tags"] = set()
        elif action == "differences":                 # every existing differing file; not same/unreadable/unresolved
            res["tags"] = {r["id"] for r in rows if _differing_file(r)}
        else:
            raise FmError("Hoard doesn't know that tag action.")
        views.pop("tagged", None)
    overrides = res.setdefault("overrides", {})
    for a in b.get("setActions") or []:
        if isinstance(a, dict) and isinstance(a.get("id"), int) and 0 <= a["id"] < len(rows):
            if a.get("action") in (None, "default"):
                overrides.pop(a["id"], None)
            elif a.get("action") in ("lr", "rl", "skip", "trash"):
                overrides[a["id"]] = a["action"]
            else:
                raise FmError("Hoard doesn't know that action.")
    flt = b.get("filter") or "all"
    if flt == "all":
        view = None
    else:
        view = views.get(flt)
        if view is None:
            if flt == "differences":
                view = [i for i, r in enumerate(rows) if r["status"] in DIFFERENT]
            elif flt == "tagged":
                view = [i for i, r in enumerate(rows) if r["id"] in res["tags"]]
            else:
                view = [i for i, r in enumerate(rows) if r["status"] == flt]
            views[flt] = view
    total = len(rows) if view is None else len(view)
    offset = max(0, int(b.get("offset") or 0))
    limit = max(1, min(500, int(b.get("limit") or 500)))
    picked = rows[offset:offset + limit] if view is None else [rows[i] for i in view[offset:offset + limit]]
    out = []
    for r in picked:
        row = _public_row(res, r)
        if r["id"] in overrides:
            row["action"] = overrides[r["id"]]
        out.append(row)
    return {"rows": out, "total": total, "selected": len(res["tags"]), "complete": res["complete"],
            "truncated": res["truncated"]}


SYNC_TREE_CHANGED = "This folder changed since the comparison, so Hoard left it."
NOT_REVIEWED = "Its contents weren't compared. Compare with subfolders included to sync this folder."


def _hash_file(job, path, alg):
    """Hex digest of a plain file reached without links (side jobs: cancellable, progress in bytes)."""
    h = hashlib.new(alg)
    f, _st = open_for_read(path)
    with f:
        while True:
            job.checkpoint()
            chunk = f.read(CHUNK)
            if not chunk:
                return h.hexdigest()
            h.update(chunk)
            job.add_bytes(len(chunk))


def _version(st, digest=None, ident=None):
    """What a later check must find again. ident: (dev, file id) when st came from a folder listing."""
    fp = _idl(st)
    if ident is not None:
        fp[0], fp[1] = ident[0], ident[1]
    v = {"id": fp, "size": st.st_size, "mtime": st.st_mtime_ns, "kind": _kind(st)}
    if digest:
        v["digest"] = digest
    return v


# ---------------------------------------------------------------- compare
def _case_insensitive(folder):
    """Does this folder's file system ignore case? Probed on the folder's own name when it has letters."""
    head, name = os.path.split(folder.rstrip("\\/"))
    if name and name.lower() != name.upper():
        alt = os.path.join(head, name.swapcase())
        try:
            return os.path.exists(alt) and os.path.samefile(alt, folder)
        except OSError:
            pass
    return plat.NAME != "linux"


class _TooMany(Exception):
    pass


def _walk_side(job, root, recursive, hidden):
    """rel -> (lstat, dev, file id, allocated) for one side, and the folders that couldn't be read."""
    entries, unreadable = {}, []
    stack = [(root, "")]
    while stack:
        d, rel = stack.pop()
        try:
            ents = _folder_entries(d)
        except OSError:
            unreadable.append(rel.rstrip(os.sep))
            continue
        for name, st, dev, fid, alloc in ents:
            job.checkpoint()
            if not hidden and _hidden(name, st):
                continue
            r = rel + name
            entries[r] = (st, dev, fid, alloc)
            if len(entries) > MAX_RESULT_ROWS:
                raise _TooMany
            if recursive and _kind(st) == "d":
                stack.append((os.path.join(d, name), r + os.sep))
    return entries, unreadable


def _under(rel, folders):
    return any(f == "" or rel == f or rel.startswith(f + os.sep) for f in folders)


def _compare_pair(job, res, lroot, rroot, rel, l, r):
    """Status of one matched pair (both present)."""
    lst, rst = l[0], r[0]
    lk, rk = _kind(lst), _kind(rst)
    if "l" in (lk, rk):
        return "not_compared", "Not compared: link.", {}
    if lk != rk:
        return "different", "One is a file and the other a folder.", {"_conflict": True}
    if lk == "d":
        if res["recursive"]:
            return "same", None, {"_folder": True}            # settled by its contents below
        return "not_compared", "Not compared: folder contents.", {}
    extra = {}
    if res["mode"] == "thorough" and lst.st_size == rst.st_size:
        try:
            ld = _hash_file(job, os.path.join(lroot, rel), "sha256")
            rd = _hash_file(job, os.path.join(rroot, r[4]), "sha256")
        except FmError as ex:
            return "not_compared", f"Not compared: {ex}", {}
        extra = {"_ld": ld, "_rd": rd}
        if ld == rd:
            return "same", None, extra
    elif res["mode"] == "quick" and lst.st_size == rst.st_size and abs(lst.st_mtime - rst.st_mtime) < TIME_SLACK:
        return "same", "Metadata matches; contents not checked.", extra
    if abs(lst.st_mtime - rst.st_mtime) >= TIME_SLACK:
        return ("newer_left" if lst.st_mtime > rst.st_mtime else "newer_right"), None, extra
    return "different", None, extra


def _side_row(rel, ent, root):
    st, dev, fid, alloc = ent[:4]
    row = _row(rel, st, dev, fid, alloc, plat.critical_key(_key(os.path.join(root, rel))))
    return row


def _run_compare(job, res):
    lroot, rroot = res["left"], res["right"]
    fold = res["fold"]
    try:
        left, lbad = _walk_side(job, lroot, res["recursive"], res["hidden"])
        right, rbad = _walk_side(job, rroot, res["recursive"], res["hidden"])
    except _TooMany:
        res["truncated"] = True
        return
    sides = []
    for ents in (left, right):
        keyed, clash = {}, set()
        for rel, ent in ents.items():
            k = rel.casefold() if fold else rel
            if k in keyed:
                clash.add(k)
            keyed.setdefault(k, []).append((rel, ent))
        sides.append((keyed, clash))
    (lk, lclash), (rk, rclash) = sides
    rows = []
    for k in sorted(set(lk) | set(rk), key=lambda x: [p.casefold() for p in x.split(os.sep)]):
        job.checkpoint()
        ls, rs = lk.get(k, []), rk.get(k, [])
        if k in lclash or k in rclash or len(ls) > 1 or len(rs) > 1:
            names = ", ".join(sorted({rel for rel, _e in ls + rs}))
            for rel, ent in ls:
                rows.append({"rel": rel, "left": _side_row(rel, ent, lroot), "right": None, "status": "not_compared",
                             "reason": f"Not compared: names differ only in case ({names}).", "_conflict": True,
                             "_lv": _version(ent[0], ident=ent[1:3])})
            for rel, ent in rs:
                rows.append({"rel": rel, "left": None, "right": _side_row(rel, ent, rroot), "status": "not_compared",
                             "reason": f"Not compared: names differ only in case ({names}).", "_conflict": True,
                             "_rv": _version(ent[0], ident=ent[1:3])})
            continue
        l = ls[0] if ls else None
        r = rs[0] if rs else None
        rel = (l or r)[0]
        row = {"rel": rel, "left": _side_row(l[0], l[1], lroot) if l else None,
               "right": _side_row(r[0], r[1], rroot) if r else None}
        if l:
            row["_lv"] = _version(l[1][0], ident=l[1][1:3])
        if r:
            row["_rv"] = _version(r[1][0], ident=r[1][1:3])
            row["_rrel"] = r[0]
        if l and r:
            status, reason, extra = _compare_pair(job, res, lroot, rroot, l[0], l[1], r[1] + (r[0],))
            row.update(extra)
            if extra.get("_ld"):
                row["_lv"]["digest"], row["_rv"]["digest"] = extra.pop("_ld"), extra.pop("_rd")
                row.pop("_ld", None)
                row.pop("_rd", None)
        else:
            present = l or r
            if _kind(present[1][0]) == "l":
                status, reason = "not_compared", "Not compared: link."
            elif _under(rel, rbad if l else lbad):
                status, reason = "not_compared", "Not compared: Hoard cannot read the other folder."
            else:
                status, reason = ("only_left" if l else "only_right"), None
        row["status"] = status
        if reason:
            row["reason"] = reason
        if _under(rel, lbad) or _under(rel, rbad):
            row["status"], row["reason"] = "not_compared", "Not compared: Hoard cannot read this folder."
        rows.append(row)
        if len(rows) >= MAX_RESULT_ROWS:
            res["truncated"] = True
            break
    if res["recursive"]:                             # folders: same only when everything inside is the same
        dirty = set()
        for row in rows:
            if row["status"] != "same":
                p = os.path.dirname(row["rel"])
                while p:
                    dirty.add(p)
                    p = os.path.dirname(p)
        for row in rows:
            if row.pop("_folder", False):
                if row["rel"] in dirty:
                    row["status"], row["reason"] = "different", "Contents differ."
    for i, row in enumerate(rows):
        row["id"] = i
    for bad in lbad + rbad:
        res.setdefault("unreadable", []).append(bad)
    res["rows"] = rows
    res["complete"] = not res["truncated"]


@api
def fm_compare(b):
    left, right = clean_abs(b.get("left")), clean_abs(b.get("right"))
    lreal = plat.strip_long(os.path.realpath(left))
    rreal = plat.strip_long(os.path.realpath(right))
    for p in (lreal, rreal):
        if not os.path.isdir(p):
            raise FmError("Choose two folders to compare.")
    lk, rk = _key(lreal), _key(rreal)
    if lk == rk or lk.startswith(rk.rstrip(os.sep) + os.sep) or rk.startswith(lk.rstrip(os.sep) + os.sep):
        raise FmError("Choose two different folders, neither inside the other.")
    mode = "thorough" if b.get("mode") == "thorough" else "quick"
    res = {"kind": "compare", "left": lreal, "right": rreal, "mode": mode, "recursive": bool(b.get("recursive")),
           "hidden": bool(b.get("hidden")), "rows": [], "complete": False, "truncated": False,
           "fold": _case_insensitive(lreal) or _case_insensitive(rreal)}
    job = Job("compare", f"Comparing {lreal} and {rreal}", mutating=False)

    def run(job):
        try:
            _run_compare(job, res)
        finally:
            job.result = {"result": _store(res), "complete": res["complete"], "truncated": res["truncated"]}
            job.counts["done"] = len(res["rows"])
    return {"job": _side(job, run).id}


# ---------------------------------------------------------------- sync (a reviewed plan over a comparison)
SYNC_CHANGED = "These files changed. Compare again before continuing."


def _sync_default(row, delete_from):
    s = row["status"]
    if s in ("only_left", "only_right"):
        if delete_from == ("right" if s == "only_right" else "left"):
            return "trash"
        return "lr" if s == "only_left" else "rl"
    if s == "newer_left":
        return "lr"
    if s == "newer_right":
        return "rl"
    return "skip"


@api
def fm_sync_plan(b):
    """An explicit, immutable list of per-item operations taken only from the reviewed comparison: every file
    copy, every folder creation and every folder sent to the Trash (with the exact tree it held) is in the plan,
    and nothing else is ever copied or trashed. Choices: stored ones (fm_results setActions), then `actions`."""
    res = _result(b.get("comparison"), "compare")
    if not res["complete"]:
        raise FmError("These results are incomplete. Compare again before syncing.")
    delete_from = b.get("deleteFrom")
    if delete_from not in (None, "left", "right"):
        raise FmError("Bad request.")
    overrides = dict(res.get("overrides") or {})
    for a in b.get("actions") or []:
        if isinstance(a, dict) and isinstance(a.get("id"), int):
            overrides[a["id"]] = a.get("action")
    rows = res["rows"]
    if b.get("scope") == "tags":
        if not res["tags"]:
            raise FmError("Tag some rows first, or sync all differences.")
        tagged = [r for r in rows if r["id"] in res["tags"]]
        folders = tuple(r["rel"] + os.sep for r in tagged if _row_kind(r) == "d")
        scope = tagged + ([r for r in rows if r["id"] not in res["tags"] and r["rel"].startswith(folders)]
                          if folders else [])
    else:
        scope = [r for r in rows if r["status"] in DIFFERENT]
    _expire_plans()
    plan = {"id": uuid.uuid4().hex[:12], "op": "sync", "time": time.time(), "items": [], "blocked": [],
            "conflicts": [], "dest": None, "destId": None, "preserveTimes": True, "result": b.get("comparison"),
            "thorough": res["mode"] == "thorough"}
    lr = {"n": 0, "bytes": 0}
    rl = {"n": 0, "bytes": 0}
    tr = {"n": 0, "bytes": 0}
    trashed = ()
    for row in sorted(scope, key=lambda r: (r["rel"].count(os.sep), r["rel"])):
        action = overrides.get(row["id"])
        if action in (None, "default"):
            action = _sync_default(row, delete_from)
        if action == "skip" or (trashed and row["rel"].startswith(trashed)):
            continue
        lpath = os.path.join(res["left"], row["rel"])
        rpath = os.path.join(res["right"], row.get("_rrel") or row["rel"])
        here = lpath if row.get("left") else rpath
        if action not in ("lr", "rl", "trash"):
            _block(plan, here, "Hoard doesn't know that action.")
            continue
        if row["status"] == "not_compared" or row.get("_conflict"):
            _block(plan, here, "One is a file and the other a folder. Sync can't replace one with the other."
                   if row["status"] == "different" else (row.get("reason") or "Not compared") + " Sync skips it.")
            continue
        kind = _row_kind(row)
        if row["status"] == "same" or (row["status"] == "different" and kind == "d"):
            continue
        if action == "trash":
            side = "right" if row["status"] == "only_right" else "left"
            if delete_from != side or row["status"] not in ("only_left", "only_right"):
                _block(plan, here, "Only items missing from the other side can be sent to the trash, and only "
                                   "when you choose to.")
                continue
            src = lpath if side == "left" else rpath
            ver = row["_lv"] if side == "left" else row["_rv"]
            why = plat.critical(src)
            if why:
                _block(plan, src, why)
                continue
            it = {"src": src, "dst": None, "kind": kind, "size": row[side].get("s") or 0, "id": ver["id"],
                  "action": "trash", "srcVer": ver, "key": src}
            if kind == "d":
                if not res["recursive"]:
                    _block(plan, src, NOT_REVIEWED)
                    continue
                pre = row["rel"] + os.sep
                inside = [r for r in rows if r["rel"].startswith(pre)]
                if any(overrides.get(r["id"]) not in (None, "default", "trash") or r["status"] != row["status"]
                       for r in inside):
                    _block(plan, src, "Some items inside are set differently, so the folder can't go to the "
                                      "Trash as a whole.")
                    continue
                vk = "_lv" if side == "left" else "_rv"
                it["tree"] = {r["rel"][len(pre):]: r[vk] for r in inside}
                it["size"] = sum(r[side].get("s") or 0 for r in inside if r[side]["k"] == "f")
                trashed = trashed + (pre,)
            plan["items"].append(it)
            tr["n"] += 1
            tr["bytes"] += it["size"]
            continue
        src_side = "left" if action == "lr" else "right"
        if row.get(src_side) is None:
            _block(plan, lpath if src_side == "right" else rpath, "There's nothing on that side to copy.")
            continue
        src, dst = (lpath, rpath) if action == "lr" else (rpath, lpath)
        sver = row["_lv"] if action == "lr" else row["_rv"]
        dver = row.get("_rv") if action == "lr" else row.get("_lv")
        why = plat.critical(dst)
        if why:
            _block(plan, dst, why)
            continue
        if kind == "d":
            if not res["recursive"]:
                _block(plan, src, NOT_REVIEWED)
                continue
            if dver is not None:
                continue                              # the folder is already there; its rows act for themselves
            plan["items"].append({"src": src, "dst": dst, "kind": "d", "size": 0, "id": sver["id"],
                                  "action": action, "srcVer": sver, "dstVer": None, "replace": False, "key": dst})
            continue
        size = row[src_side].get("s") or 0
        plan["items"].append({"src": src, "dst": dst, "kind": "f", "size": size, "id": sver["id"], "action": action,
                              "srcVer": sver, "dstVer": dver, "replace": dver is not None, "key": dst})
        if dver is not None:
            other = row["right" if action == "lr" else "left"]
            plan["conflicts"].append({"src": src, "dst": dst, "srcSize": size, "dstSize": other.get("s"),
                                      "srcTime": row[src_side]["m"], "dstTime": other["m"]})
        bucket = lr if action == "lr" else rl
        bucket["n"] += 1
        bucket["bytes"] += size
    if not plan["items"] and not plan["blocked"]:
        raise FmError("There's nothing to sync.")
    PLANS[plan["id"]] = plan
    return dict(_plan_view(plan), directions={"lr": lr, "rl": rl, "trash": tr}, result=b.get("comparison"))


def _version_ok(job, path, ver, thorough):
    """Is the item at path still the version the comparison saw? (None: must not exist.)"""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return ver is None
    if ver is None or not safefs.same_id(st, ver["id"]) or _kind(st) != ver["kind"]:
        return False
    if ver["kind"] == "f":
        if (st.st_size, st.st_mtime_ns) != (ver["size"], ver["mtime"]):
            return False
        if thorough and ver.get("digest"):
            try:
                return _hash_file(job, path, "sha256") == ver["digest"]
            except FmError:
                return False
    return True


def _ensure_parent(job, path):
    """Hold the folder that will contain path, creating (and journaling) missing folders one at a time."""
    real = plat.canonical(os.path.dirname(path), follow=True)
    missing, head = [], real
    while not os.path.lexists(head):
        head, name = os.path.split(head)
        if not name:
            raise FileNotFoundError(errno.ENOENT, "That drive isn't there.", path)
        missing.insert(0, name)
    d = safefs.Dir.open(head, guard=True)
    try:
        for name in missing:
            d.mkdir(name)
            st = d.lstat(name)
            job.record({"t": "created", "path": d.child(name), "id": _idl(st), "kind": "d"})
            d = d.sub(name, owns_parent=True)
    except BaseException:
        d.close()
        raise
    return d


def _sync_one(job, plan, it):
    while not (_version_ok(job, it["src"], it["srcVer"], plan["thorough"]) and
               (it["action"] == "trash" or it["kind"] == "d" or
                _version_ok(job, it["dst"], it["dstVer"], plan["thorough"]))):
        if "changed" in job.skip_all:
            job.error(it["src"], SYNC_CHANGED)
            return False
        ans, every = job.ask("error", it["src"], SYNC_CHANGED, ["retry", "skip", "skipall", "abort"])
        if ans == "abort":
            raise Stop
        if ans == "retry":
            continue
        if ans == "skipall" or every:
            job.skip_all.add("changed")
        job.error(it["src"], SYNC_CHANGED)
        return False
    if it["action"] == "trash":
        if it.get("tree") is not None and not _tree_matches(job, it):
            raise Unsafe(SYNC_TREE_CHANGED)
        return _trash_top(job, it)
    _refuse_critical(it["src"], it["dst"])
    if it["kind"] == "d":                             # create the folder only; its reviewed contents are items too
        try:
            if _kind(os.lstat(it["dst"])) == "d":
                return True
            raise Unsafe("Something else now has that name, so Hoard left it.")
        except FileNotFoundError:
            pass
        with _ensure_parent(job, os.path.join(it["dst"], "_")):
            pass
        return True
    sd, sname = safefs.open_parent(it["src"], guard=False)
    with sd, _ensure_parent(job, it["dst"]) as dd:
        dname = os.path.basename(it["dst"])
        job.force = "replace" if it["replace"] else None
        try:
            return _transfer(job, sd, sname, it["id"], dd, dname, move=False)
        finally:
            job.force = None


def _exec_sync(job, plan, items):
    lr = rl = 0
    for it in items:
        job.checkpoint()
        job.current = it["src"]
        ok = _guarded(job, it["src"], lambda it=it: _sync_one(job, plan, it))
        job.counts["done" if ok else "skipped"] += 1
        if ok and it["action"] == "lr":
            lr += it.get("size") or 0
        elif ok and it["action"] == "rl":
            rl += it.get("size") or 0
    job.result = {"result": plan["result"], "bytesLR": lr, "bytesRL": rl}


# ---------------------------------------------------------------- archives (read-only browsing, extract, pack)
ARCHIVE_EXT = (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar", ".zip")
MAX_MEMBERS = 200_000
MAX_EXPANDED = 100 << 30
MAX_RATIO = 1000
MAX_PREVIEW = 256 << 20
MAX_DEPTH = 256                                 # folder levels inside an archive
MAX_PATH_BYTES = 64 << 20                       # all indexed member paths together
ENCRYPTED = "Hoard can't open encrypted archives."
LIMITS = "This archive exceeds Hoard's browsing limits."
_PREVIEW = {"dir": None, "files": collections.OrderedDict()}


class ArchiveError(Exception):
    """The whole archive is refused; the message is shown as is."""


def _unreadable(reason):
    return ArchiveError(f"Hoard can't read this archive. {reason}")


def _archive_type(path):
    low = path.lower()
    for ext in ARCHIVE_EXT:
        if low.endswith(ext):
            return "zip" if ext == ".zip" else "tar"
    return None


def _member_path(raw):
    """A member name as a safe relative path 'a/b/c', or ArchiveError for anything that could land elsewhere."""
    if not isinstance(raw, str) or "\0" in raw:
        raise _unreadable("It contains an invalid name.")
    r = raw.replace("\\", "/")
    if r.startswith("/") or re.match(r"^[A-Za-z]:", r):
        raise _unreadable(f"It contains an absolute path ({raw}).")
    parts = [p for p in r.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise _unreadable(f"It contains a path that leads outside it ({raw}).")
    if len(parts) > MAX_DEPTH:
        raise ArchiveError(LIMITS + " It's nested too deeply.")
    if plat.NAME == "windows":
        for p in parts:
            if any(c in _WIN_BAD for c in p) or p.rstrip(" .") != p or p.split(".")[0].lower() in _WIN_RESERVED:
                raise _unreadable(f"It contains a name Windows can't use ({raw}).")
    return "/".join(parts)


def _alias_key(path):
    return "/".join(p.casefold().rstrip(" .") for p in path.split("/"))


def _index_members(job, entries):
    """entries: [(raw name, is_dir, size, mtime, opaque)] -> members index; refuses duplicates and aliases.
    Every indexed path, implied folders included, counts against the member and path-bytes limits."""
    members, keys, total = {}, {}, 0
    budget = [MAX_PATH_BYTES]

    def add(p, entry):
        members[p] = entry
        budget[0] -= len(p.encode("utf-8", "surrogatepass")) + 1
        if len(members) > MAX_MEMBERS or budget[0] < 0:
            raise ArchiveError(LIMITS)
    for raw, is_dir, size, mtime, ref in entries:
        job.checkpoint()
        p = _member_path(raw)
        if not p:
            continue
        k = _alias_key(p)
        if k in keys:
            if is_dir and members[keys[k]]["dir"] and keys[k] == p:
                continue                              # the same folder listed twice
            raise _unreadable(f"It contains two items that would have the same name ({keys[k]} and {p}).")
        keys[k] = p
        add(p, {"dir": is_dir, "size": 0 if is_dir else size, "mtime": int(mtime), "ref": ref})
        total += 0 if is_dir else size
        if total > MAX_EXPANDED:
            raise ArchiveError(LIMITS)
    for p in list(members):                          # folders implied by deeper names
        parent = p.rpartition("/")[0]
        while parent:
            m = members.get(parent)
            if m is None:
                k = _alias_key(parent)
                if k in keys and keys[k] != parent:
                    raise _unreadable(f"It contains two items that would have the same name ({keys[k]} and {parent}).")
                add(parent, {"dir": True, "size": 0, "mtime": 0, "ref": None})
                keys[k] = parent
            elif not m["dir"]:
                raise _unreadable(f"‘{parent}’ is both a file and a folder in it.")
            else:
                break                                 # already there, and so is everything above it
            parent = parent.rpartition("/")[0]
    children, sizes = {}, {}
    for p, m in members.items():
        children.setdefault(p.rpartition("/")[0], []).append(p)
        if not m["dir"]:
            parent = p.rpartition("/")[0]
            while True:
                sizes[parent] = sizes.get(parent, 0) + m["size"]
                if not parent:
                    break
                parent = parent.rpartition("/")[0]
    for p, m in members.items():
        if m["dir"]:
            m["size"] = sizes.get(p, 0)
    return members, children, total


def _zip_entries(zf, archive_size):
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise ArchiveError(LIMITS)
    out, declared, packed = [], 0, 0
    for zi in infos:
        if zi.flag_bits & 0x1:
            raise ArchiveError(ENCRYPTED)
        mode = (zi.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise _unreadable(f"It contains a link ({zi.filename}).")
        if mode and (stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            raise _unreadable(f"It contains a device or special file ({zi.filename}).")
        is_dir = zi.is_dir()
        if not is_dir and zi.file_size > MAX_RATIO * max(zi.compress_size, 1):
            raise ArchiveError(LIMITS + " An item expands more than 1,000 times.")
        declared += zi.file_size
        packed += zi.compress_size
        try:
            mtime = time.mktime(zi.date_time + (0, 0, -1))
        except (OverflowError, ValueError):
            mtime = 0
        out.append((zi.filename, is_dir, zi.file_size, mtime, zi.filename))
    if declared > MAX_EXPANDED:
        raise ArchiveError(LIMITS)
    if declared > MAX_RATIO * max(archive_size, 1):
        raise ArchiveError(LIMITS + " It expands more than 1,000 times.")
    return out


def _tar_entries(job, tf):
    out = []
    for ti in tf:                                   # streams through the archive (decompressing as it goes)
        job.checkpoint()
        if ti.issym() or ti.islnk():
            raise _unreadable(f"It contains a link ({ti.name}).")
        if ti.ischr() or ti.isblk() or ti.isfifo() or ti.isdev():
            raise _unreadable(f"It contains a device or special file ({ti.name}).")
        if not (ti.isfile() or ti.isdir()):
            raise _unreadable(f"It contains an entry Hoard doesn't support ({ti.name}).")
        if len(out) >= MAX_MEMBERS or tf.offset > MAX_EXPANDED:
            raise ArchiveError(LIMITS)
        out.append((ti.name, ti.isdir(), ti.size, ti.mtime, ti.name))
    return out


def _open_archive_file(path):
    """(file, fstat, kind) for an archive reached without links."""
    kind = _archive_type(path)
    if kind is None:
        raise FmError("Hoard opens .zip, .tar, .tar.gz, .tgz, .tar.bz2 and .tar.xz archives.")
    f, st = open_for_read(path)
    return f, st, kind


def _archive_obj(f, kind):
    try:
        if kind == "zip":
            return zipfile.ZipFile(f)
        return tarfile.open(fileobj=f, mode="r:*")
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, ValueError) as ex:
        raise _unreadable(str(ex) or "It's damaged or not an archive.")


@api
def fm_archive_open(b):
    path = clean_abs(b.get("path"))
    if _archive_type(path) is None:
        raise FmError("Hoard opens .zip, .tar, .tar.gz, .tgz, .tar.bz2 and .tar.xz archives.")
    job = Job("archive", f"Reading archive {os.path.basename(path)}", mutating=False)

    def run(job):
        f, st, kind = _open_archive_file(path)
        with f:
            arc = _archive_obj(f, kind)
            try:
                if kind == "zip":
                    entries = _zip_entries(arc, st.st_size)
                else:
                    entries = _tar_entries(job, arc)
            except (zipfile.BadZipFile, tarfile.TarError, EOFError, zlib.error, lzma.LZMAError, OSError) as ex:
                raise _unreadable(str(ex) or "It's damaged.")
            finally:
                arc.close()
        members, children, total = _index_members(job, entries)
        res = {"kind": "archive", "path": plat.strip_long(os.path.realpath(path)), "type": kind,
               "version": _version(st), "members": members, "children": children, "rows": [], "complete": True,
               "truncated": False}
        rid = _store(res)
        job.counts["done"] = len(members)
        job.result = {"result": rid, "complete": True, "truncated": False,
                      "archive": {"path": res["path"], "name": os.path.basename(path), "members": len(members),
                                  "bytes": total}}
    return {"job": _side(job, run).id}


def _archive(rid):
    """The stored archive, after checking the file on disk is still the one that was read."""
    res = _result(rid, "archive")
    try:
        st = os.lstat(res["path"])
    except OSError:
        raise FmError("The archive isn't there anymore.")
    v = res["version"]
    if not safefs.same_id(st, v["id"]) or (st.st_size, st.st_mtime_ns) != (v["size"], v["mtime"]):
        raise FmError("The archive changed. Open it again.")
    return res


@api
def fm_archive_list(b):
    res = _archive(b.get("archive"))
    inner = (b.get("inner") or "").strip("/")
    if inner and not res["members"].get(inner, {}).get("dir"):
        raise FmError("That folder isn't in the archive.")
    rows = []
    for i, p in enumerate(res["children"].get(inner, [])):
        m = res["members"][p]
        name = p.rpartition("/")[2]
        if not b.get("hidden") and name.startswith("."):
            continue
        rows.append({"n": name, "k": "d" if m["dir"] else "f", "s": m["size"], "a": None, "m": m["mtime"],
                     "c": core.OTHER if m["dir"] else core.cat_of(name), "h": name.startswith("."), "ro": True,
                     "p": None, "id": [0, hash(p) & 0x7FFFFFFF], "member": p})
    rows = _sort_rows(rows, b.get("sort") or "name", b.get("desc"), True)
    offset = max(0, int(b.get("offset") or 0))
    limit = max(1, min(MAX_ROWS, int(b.get("limit") or 5000)))
    crumbs = [{"n": os.path.basename(res["path"]), "inner": ""}]
    acc = []
    for part in [x for x in inner.split("/") if x]:
        acc.append(part)
        crumbs.append({"n": part, "inner": "/".join(acc)})
    return {"crumbs": crumbs, "rows": rows[offset:offset + limit], "total": len(rows), "path": res["path"]}


class _Capped:
    """A reader that refuses to give more than `limit` bytes (stops at limits even when headers lie)."""
    def __init__(self, f, limit, what):
        self.f, self.left, self.what = f, limit, what

    def read(self, n):
        data = self.f.read(min(n, self.left + 1))
        if len(data) > self.left:
            raise ArchiveError(f"Hoard can't read this archive. {self.what}")
        self.left -= len(data)
        return data


def _member_stream(arc, kind, m):
    if kind == "zip":
        return arc.open(m["ref"])
    ti = arc.getmember(m["ref"])
    f = arc.extractfile(ti)
    if f is None:
        raise _unreadable("That item can't be read.")
    return f


def _drop_previews(res):
    for k in [k for k in _PREVIEW["files"] if k[0] == id(res)]:
        try:
            os.remove(_PREVIEW["files"].pop(k))
        except OSError:
            pass


@api
def fm_archive_read(b):
    res = _archive(b.get("archive"))
    member = (b.get("member") or "").strip("/")
    m = res["members"].get(member)
    if m is None or m["dir"]:
        raise FmError("That isn't a file in the archive.")
    if m["size"] > MAX_PREVIEW:
        raise FmError("This item is over 256 MB. Extract it first.")
    try:
        offset = int(b.get("offset") or 0)
        length = min(MAX_READ, int(b.get("length") if b.get("length") is not None else MAX_READ))
    except (TypeError, ValueError):
        raise FmError("Bad range.")
    if offset < 0 or length < 0:
        raise FmError("Bad range.")
    key = (id(res), member)
    path = _PREVIEW["files"].get(key)
    if path is None or not os.path.exists(path):
        if _PREVIEW["dir"] is None:
            _PREVIEW["dir"] = tempfile.mkdtemp(prefix="hoard-preview-")   # never beside the archive
        path = os.path.join(_PREVIEW["dir"], uuid.uuid4().hex)
        f, _st, kind = _open_archive_file(res["path"])
        try:
            with f:
                arc = _archive_obj(f, kind)
                try:
                    with _member_stream(arc, kind, m) as src, open(path, "wb") as out:
                        capped = _Capped(src, min(m["size"], MAX_PREVIEW), "It's damaged or lies about its sizes.")
                        while True:
                            chunk = capped.read(CHUNK)
                            if not chunk:
                                break
                            out.write(chunk)
                finally:
                    arc.close()
        except (ArchiveError, zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, RuntimeError) as ex:
            try:
                os.remove(path)
            except OSError:
                pass
            raise FmError(str(ex) if isinstance(ex, ArchiveError) else f"Hoard can't read this archive. {ex}")
        _PREVIEW["files"][key] = path
        while len(_PREVIEW["files"]) > 8:
            try:
                os.remove(_PREVIEW["files"].popitem(last=False)[1])
            except OSError:
                pass
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(length)
    return {"data": base64.b64encode(data).decode("ascii"), "size": m["size"], "eof": offset + len(data) >= m["size"]}


def _plan_extract(plan, b):
    res = _archive(b.get("archive"))
    dest = _dest(plan, b.get("dest"))
    raw = b.get("members") or [""]
    sel = []
    for r in raw:
        p = (r or "").strip("/") if isinstance(r, str) else None
        if p is None or (p and p not in res["members"]):
            raise FmError("That item isn't in the archive.")
        sel.append(p)
    sel = [p for p in dict.fromkeys(sel) if not any(o != p and (o == "" or p.startswith(o + "/")) for o in sel)]
    tops = []
    for p in sel:
        if p == "":
            tops += [(c, "") for c in res["children"].get("", [])]
        else:
            tops.append((p, p.rpartition("/")[0]))
    seen = {}
    for member, base in tops:
        rel = member[len(base) + 1:] if base else member
        dst = os.path.join(dest, *rel.split("/"))
        k = _key(dst)
        if k in seen:
            _block(plan, dst, f"Two items would be extracted to the same place: {rel}.")
            continue
        seen[k] = member
        why = plat.critical(dst)
        if why:
            _block(plan, dst, why)
            continue
        m = res["members"][member]
        outputs = [member] + ([p for p in res["members"] if p.startswith(member + "/")] if m["dir"] else [])
        item = {"src": member, "dst": dst, "kind": "d" if m["dir"] else "f", "size": m["size"], "key": dst,
                "base": base, "outputs": outputs}
        if os.path.lexists(dst):
            dst_st = os.lstat(dst)
            if m["dir"] and _kind(dst_st) == "d":
                item["merge"] = True
            else:
                plan["conflicts"].append({"src": member, "dst": dst, "srcSize": None if m["dir"] else m["size"],
                                          "dstSize": dst_st.st_size if _kind(dst_st) == "f" else None,
                                          "srcTime": m["mtime"], "dstTime": int(dst_st.st_mtime)})
        plan["items"].append(item)
    plan["archive"] = b.get("archive")


def _fake_st(size, mtime, is_dir=False):
    """Stat-like facts for something that isn't on disk yet (archive members, new files), for conflict rules."""
    return types.SimpleNamespace(st_mode=(stat.S_IFDIR if is_dir else stat.S_IFREG) | 0o644, st_size=size,
                                 st_mtime=mtime, st_mtime_ns=int(mtime * 1e9), st_atime_ns=int(mtime * 1e9),
                                 st_dev=0, st_ino=0)


def _write_stream(job, dd, name, src, size, mtime, preserve, replace=None):
    """Write a stream to a hidden temp file in held folder dd (flushed to disk), then put it in place: a no-replace
    rename, or - for a replacement - the recoverable swap. A cancel or failure leaves the original untouched."""
    tmp = f".hoard-{job.id}-{next(_TMPN)}.part"
    made = False
    try:
        fo = dd.create(tmp)
        made = True
        with fo:
            while True:
                job.checkpoint()
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                fo.write(chunk)
                job.add_bytes(len(chunk))
            fo.flush()
            os.fsync(fo.fileno())
        if preserve and mtime:
            try:
                dd.utime(tmp, (int(mtime * 1e9), int(mtime * 1e9)))
            except OSError:
                pass
        if replace is None:
            dd.rename_to(tmp, dd, name, guard_src=False)
        else:
            _commit_swap(job, dd, tmp, replace.name, safefs.ident(replace.st), check=_unchanged(replace.st))
    except BaseException:
        if made:
            try:
                if dd.exists(tmp):
                    dd.unlink(tmp, None)
            except (OSError, Unsafe):
                pass
        raise
    return dd.lstat(name if replace is None else replace.name)


def _record_created(job, dd, name, st, group):
    act = {"t": "created", "path": dd.child(name), "id": _idl(st), "kind": _kind(st)}
    if _kind(st) == "f":
        act.update(size=st.st_size, mtime=st.st_mtime_ns)
    if group is not None:
        act["g"] = group
    job.record(act)


def _extract_item(job, res, arc, it, dd, budget):
    """Extract one top item (a member, with everything under it) into held folder dd."""
    m = res["members"][it["src"]]
    top = os.path.basename(it["dst"])
    target = _resolve_dst(job, it["src"], _fake_st(m["size"], m["mtime"], m["dir"]), dd, top, False, defer=True)
    if target is None:
        return False
    group, opened = None, {}
    try:
        if not m["dir"]:
            with _member_stream(arc, res["type"], m) as src:
                budget[0] -= m["size"]
                if budget[0] < 0:
                    raise ArchiveError(LIMITS)
                rep = target if isinstance(target, _Replace) else None
                st = _write_stream(job, dd, _target_name(target),
                                   _Capped(src, m["size"], "It's damaged or lies about its sizes."),
                                   m["size"], m["mtime"], job.preserve, replace=rep)
            job.tick()
            _record_created(job, dd, _target_name(target), st, None)
            return True
        if target is MERGE:
            root = dd.sub(top, guard=True)
        else:
            st = dd.mkdir(target)
            group = job.new_group()
            job.record({"t": "created", "path": dd.child(target), "id": _idl(st), "kind": "d", "g": group, "top": True})
            root = dd.sub(target, safefs.ident(st), guard=True)
        opened[""] = root
        for member in sorted(it["outputs"][1:], key=lambda p: (p.count("/"), p)):
            job.checkpoint()
            rel = member[len(it["src"]) + 1:]
            parent_rel, _s, name = rel.rpartition("/")
            parent = opened.get(parent_rel)
            if parent is None:
                job.error(member, "Its folder couldn't be created, so it was skipped.")
                continue
            out_path = os.path.join(parent.path, name)
            if not _key(out_path).startswith(_key(root.path) + os.sep):      # containment, rechecked per output
                raise Unsafe("An item would land outside the destination folder.")
            mm = res["members"][member]
            if mm["dir"]:
                def mk(parent=parent, name=name, rel=rel):
                    if parent.exists(name):
                        sub = parent.sub(name, guard=True)               # existing: never a link, not protected
                    else:
                        st = parent.mkdir(name)
                        _record_created(job, parent, name, st, group)
                        sub = parent.sub(name, safefs.ident(st), guard=True)
                    opened[rel] = sub
                _guarded(job, out_path, lambda mk=mk: mk() or True)
                continue

            def one(parent=parent, name=name, mm=mm, member=member):
                tname = _resolve_dst(job, member, _fake_st(mm["size"], mm["mtime"]), parent, name, False, defer=True)
                if tname is None or tname is MERGE:
                    return False
                budget[0] -= mm["size"]
                if budget[0] < 0:
                    raise ArchiveError(LIMITS)
                rep = tname if isinstance(tname, _Replace) else None
                with _member_stream(arc, res["type"], mm) as src:
                    st = _write_stream(job, parent, _target_name(tname),
                                       _Capped(src, mm["size"], "It's damaged or lies about its sizes."),
                                       mm["size"], mm["mtime"], job.preserve, replace=rep)
                _record_created(job, parent, _target_name(tname), st, group if rep is None else None)
                return True
            _guarded(job, out_path, one)
            job.tick()
    finally:
        for d in opened.values():
            d.close()
    return True


def _exec_extract(job, plan, items):
    res = _archive(plan["archive"])
    _pending_add({"job": job.id, "kind": "parts", "dirs": [plan["dest"]]})
    outputs = []
    try:
        f, _st, kind = _open_archive_file(res["path"])
        with f, safefs.Dir.open(plan["dest"], guard=True, expect=plan["destId"]) as dd:
            arc = _archive_obj(f, kind)
            try:
                budget = [MAX_EXPANDED]
                for it in items:
                    job.checkpoint()
                    job.current = it["src"]
                    _refuse_critical(it["dst"])
                    ok = _guarded(job, it["src"], lambda it=it: _extract_item(job, res, arc, it, dd, budget))
                    job.counts["done" if ok else "skipped"] += 1
                    if ok:
                        outputs.append(it["dst"])
            finally:
                arc.close()
    finally:
        _pending_drop(job.id)
        job.result = {"result": plan["archive"], "outputs": outputs}


def _plan_pack(plan, b):
    dest = _dest(plan, b.get("dest"))
    name = clean_name(b.get("name") or "Archive.zip")
    dst = os.path.join(dest, name)
    srcs = _sources(plan, b.get("sources"), guard=False)
    why = plat.critical(dst)
    if why:
        _block(plan, dst, why)
        return
    names = {}
    for p, st in srcs:
        k = _alias_key(os.path.basename(p))
        if k in names:
            _block(plan, p, f"Two items would have the same name in the archive: {os.path.basename(p)}.")
            continue
        if _kind(st) == "l":
            _block(plan, p, "Links aren't packed.")
            continue
        real = _key(plat.canonical(p))
        if _kind(st) == "d" and (_key(dest) == real or _key(dest).startswith(real + os.sep)):
            _block(plan, p, "The archive can't be saved inside a folder it packs.")
            continue
        names[k] = p
        plan["items"].append({"src": p, "dst": dst, "kind": _kind(st), "size": _estimate(p, st, time.time() + 2),
                              "id": _fp(p), "key": p})
    if os.path.lexists(dst):
        dst_st = os.lstat(dst)
        plan["conflicts"].append({"src": None, "dst": dst, "srcSize": None,
                                  "dstSize": dst_st.st_size if _kind(dst_st) == "f" else None,
                                  "srcTime": int(time.time()), "dstTime": int(dst_st.st_mtime)})
    plan["packName"] = name


def _zip_time(mtime):
    t = time.localtime(max(mtime, 315532800))
    return (min(max(t.tm_year, 1980), 2107),) + tuple(t[1:6])


def _pack_tree(job, zf, d, name, expect, arcname):
    """Add d/name (and, for a folder, what's inside) to the zip. Never follows links; other disks are skipped."""
    job.checkpoint()
    st = d.check(name, expect, guard=False)
    k = _kind(st)
    if k == "l":
        job.error(d.child(name), "Not packed: link.")
        return
    if k == "d":
        zi = zipfile.ZipInfo(arcname + "/", _zip_time(st.st_mtime))
        zi.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
        zf.writestr(zi, b"")
        with d.sub(name, safefs.ident(st), guard=False) as sub:
            for c, cst in _children(job, sub):
                _guarded(job, sub.child(c),
                         lambda c=c, cst=cst: _pack_tree(job, zf, sub, c, safefs.ident(cst), arcname + "/" + c))
        return
    f, fst = d.open_read(name, safefs.ident(st))
    with f:
        zi = zipfile.ZipInfo(arcname, _zip_time(fst.st_mtime))
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.external_attr = (stat.S_IFREG | 0o644) << 16
        zi.file_size = fst.st_size
        with zf.open(zi, "w", force_zip64=fst.st_size > (1 << 31)) as w:
            while True:
                job.checkpoint()
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                w.write(chunk)
                job.add_bytes(len(chunk))
    job.tick()


def _exec_pack(job, plan, items):
    _pending_add({"job": job.id, "kind": "parts", "dirs": [plan["dest"]]})
    try:
        with safefs.Dir.open(plan["dest"], guard=True, expect=plan["destId"]) as dd:
            _refuse_critical(dd.child(plan["packName"]))
            target = _resolve_dst(job, None, _fake_st(0, time.time()), dd, plan["packName"], False, defer=True)
            if target is None or target is MERGE:
                job.counts["skipped"] += len(items)
                return
            tmp = f".hoard-{job.id}-{next(_TMPN)}.part"
            fo = dd.create(tmp)
            try:
                with fo:
                    with zipfile.ZipFile(fo, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                        for it in items:
                            def add(it=it):
                                sd, sname = safefs.open_parent(it["src"], guard=False)
                                with sd:
                                    _pack_tree(job, zf, sd, sname, it["id"], os.path.basename(it["src"]))
                                return True
                            ok = _guarded(job, it["src"], add)
                            job.counts["done" if ok else "skipped"] += 1
                    fo.flush()
                    os.fsync(fo.fileno())
                if isinstance(target, _Replace):     # the new archive is complete: now swap it in, recoverably
                    _commit_swap(job, dd, tmp, target.name, safefs.ident(target.st), check=_unchanged(target.st))
                else:
                    dd.rename_to(tmp, dd, target, guard_src=False)
            except BaseException:
                try:
                    if dd.exists(tmp):
                        dd.unlink(tmp, None)
                except (OSError, Unsafe):
                    pass
                raise
            name = _target_name(target)
            st = dd.lstat(name)
            _record_created(job, dd, name, st, None)
            job.result = {"result": None, "outputs": [dd.child(name)]}
    finally:
        _pending_drop(job.id)


# ---------------------------------------------------------------- checksums
ALGOS = {"md5": ("md5", 32, "MD5"), "sha1": ("sha1", 40, "SHA-1"), "sha256": ("sha256", 64, "SHA-256")}
_ALGO_ALIASES = {"md5": "md5", "sha1": "sha1", "sha-1": "sha1", "sha256": "sha256", "sha-256": "sha256"}
CHECKSUM_EXT = {"md5": "md5", "sha1": "sha1", "sha256": "sha256"}


def _algo(name):
    a = _ALGO_ALIASES.get(str(name or "sha256").lower())
    if a is None:
        raise FmError("Choose MD5, SHA-1 or SHA-256.")
    return a


def _hex_ok(value, alg):
    return isinstance(value, str) and len(value) == ALGOS[alg][1] and all(c in "0123456789abcdefABCDEF" for c in value)


_GNU = re.compile(r"^(\\?)([0-9a-fA-F]+) [ *](.+)$")
_BSD = re.compile(r"^(MD5|SHA1|SHA-1|SHA256|SHA-256) ?\((.+)\) ?= ?([0-9a-fA-F]+)$")


def _parse_checksum_file(path, alg):
    """{path key: digest} from a GNU ('<hex>  name') or BSD ('SHA256 (name) = <hex>') checksum file. References
    must stay inside the file's folder: absolute or '..' references make Hoard refuse the whole file."""
    f, st = open_for_read(path)
    if st.st_size > 64 << 20:
        f.close()
        raise FmError("That checksum file is too large.")
    with f:
        text = f.read().decode("utf-8", "replace")
    base = os.path.dirname(path)
    out = {}
    for line in text.splitlines():
        line = line.strip("﻿").rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _BSD.match(line)
        if m:
            line_alg, ref, digest = _ALGO_ALIASES[m.group(1).lower()], m.group(2), m.group(3)
            if line_alg != alg:
                raise FmError(f"That checksum file uses {ALGOS[line_alg][2]}. Choose {ALGOS[line_alg][2]}.")
        else:
            m = _GNU.match(line)
            if not m:
                continue
            digest, ref = m.group(2), m.group(3)
            if m.group(1):
                ref = ref.replace("\\n", "\n").replace("\\\\", "\\")
        if not _hex_ok(digest, alg):
            raise FmError(f"That checksum isn't valid for {ALGOS[alg][2]}.")
        r = ref.replace("\\", "/")
        parts = [p for p in r.split("/") if p not in ("", ".")]
        if r.startswith("/") or re.match(r"^[A-Za-z]:", r) or ".." in parts or not parts:
            raise FmError(f"That checksum file points outside its folder ({ref}), so Hoard won't use it.")
        out[_key(os.path.join(base, *parts))] = digest.lower()
    return out


@api
def fm_checksums(b):
    alg = _algo(b.get("algorithm"))
    rows, files = [], []
    for raw in (b.get("paths") or [])[:MAX_RESULT_ROWS]:
        p = clean_abs(raw)
        row = {"id": len(rows), "path": p, "digest": None, "expected": None, "status": "pending"}
        try:
            st = os.lstat(p)
            k = _kind(st)
            if k != "f" or not stat.S_ISREG(st.st_mode):
                row["status"] = "excluded"
                row["error"] = {"d": "Folders aren't checked.", "l": "Links aren't followed."}.get(k, "Not a regular file.")
            else:
                files.append(row)
        except OSError:
            row["status"], row["error"] = "unreadable", GONE
        rows.append(row)
    if not files:
        raise FmError("Choose at least one regular file.")
    expected, exp = b.get("expected"), None
    if isinstance(expected, dict) and expected.get("value") is not None:
        value = str(expected["value"]).strip()
        if not _hex_ok(value, alg):
            raise FmError(f"That checksum isn't valid for {ALGOS[alg][2]}.")
        target = clean_abs(expected.get("path") or (files[0]["path"] if len(files) == 1 else ""))
        if len(files) != 1 or _key(target) != _key(files[0]["path"]):
            raise FmError("A pasted checksum checks one chosen file.")
        exp = {_key(files[0]["path"]): value.lower()}
    elif isinstance(expected, dict) and expected.get("file"):
        exp = _parse_checksum_file(clean_abs(expected["file"]), alg)
    res = {"kind": "checksum", "algorithm": alg, "rows": rows, "complete": False, "truncated": False,
           "verify": exp is not None}
    job = Job("checksums", f"Computing {ALGOS[alg][2]} checksums", mutating=False)
    job.total_n = len(files)
    job.total_b = sum(os.lstat(r["path"]).st_size for r in files if os.path.exists(r["path"]))

    def run(job):
        try:
            for row in files:
                job.current = row["path"]
                try:
                    row["digest"] = _hash_file(job, row["path"], ALGOS[alg][0])
                except FmError as ex:
                    row["status"], row["error"] = "unreadable", str(ex)
                    job.tick()
                    continue
                if exp is None:
                    row["status"] = "done"
                else:
                    want = exp.get(_key(row["path"]))
                    row["expected"] = want
                    row["status"] = "missing" if want is None else ("match" if want == row["digest"] else "mismatch")
                job.counts["done"] += 1
                job.tick()
            res["complete"] = True
        finally:
            for row in files:
                if row["status"] == "pending":
                    row["status"] = "not_checked"
            job.result = {"result": _store(res), "complete": res["complete"], "truncated": False}
    return {"job": _side(job, run).id}


def _gnu_line(digest, name):
    if "\n" in name or "\\" in name:
        return "\\" + digest + "  " + name.replace("\\", "\\\\").replace("\n", "\\n") + "\n"
    return digest + "  " + name + "\n"


def _plan_checksum_save(plan, b):
    res = _result(b.get("result"), "checksum")
    alg = _algo(b.get("algorithm") or res["algorithm"])
    if alg != res["algorithm"]:
        raise FmError(f"Compute {ALGOS[alg][2]} checksums first.")
    by_dir = {}
    for row in res["rows"]:
        if row.get("digest"):
            by_dir.setdefault(os.path.dirname(row["path"]), []).append(row)
    if not by_dir:
        raise FmError("There are no checksums to save yet.")
    for folder, rows in sorted(by_dir.items()):
        dst = os.path.join(folder, "checksums." + CHECKSUM_EXT[alg])
        data = "".join(_gnu_line(r["digest"], os.path.basename(r["path"]))
                       for r in sorted(rows, key=lambda r: r["path"])).encode("utf-8")
        why = plat.critical(dst)
        if why:
            _block(plan, dst, why)
            continue
        if os.path.lexists(dst):
            dst_st = os.lstat(dst)
            plan["conflicts"].append({"src": None, "dst": dst, "srcSize": len(data),
                                      "dstSize": dst_st.st_size if _kind(dst_st) == "f" else None,
                                      "srcTime": int(time.time()), "dstTime": int(dst_st.st_mtime)})
        try:
            dest_id = _fp(plat.strip_long(os.path.realpath(folder)))
        except OSError:
            _block(plan, dst, GONE)
            continue
        plan["items"].append({"src": None, "dst": dst, "kind": "f", "size": len(data), "key": dst, "data": data,
                              "destId": dest_id})


def _exec_checksum_save(job, plan, items):
    outputs = []
    for it in items:
        job.checkpoint()

        def one(it=it):
            _refuse_critical(it["dst"])
            folder, name = os.path.split(it["dst"])
            with safefs.Dir.open(folder, guard=True, expect=it["destId"]) as dd:
                target = _resolve_dst(job, None, _fake_st(len(it["data"]), time.time()), dd, name, False, defer=True)
                if target is None or target is MERGE:
                    return False
                rep = target if isinstance(target, _Replace) else None
                st = _write_stream(job, dd, _target_name(target), io.BytesIO(it["data"]), len(it["data"]), 0, False,
                                   replace=rep)
                _record_created(job, dd, _target_name(target), st, None)
                outputs.append(dd.child(_target_name(target)))
            return True
        ok = _guarded(job, it["dst"], one)
        job.counts["done" if ok else "skipped"] += 1
        job.tick()
    job.result = {"result": plan.get("result"), "outputs": outputs}


# ---------------------------------------------------------------- editor
MAX_EDIT = 5 << 20
BACKUPS = os.path.join(core.DATA_DIR, "fm-backups")    # previous versions of edited files, for undo
EDIT_CHANGED = "This file changed on disk."
BOMS = {"utf-8": b"\xef\xbb\xbf", "utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}


def _detect_encoding(raw):
    """(encoding, bom, text). BOM first; then strict UTF-8 (text with NUL bytes doesn't count: ASCII in UTF-16 is
    'valid' UTF-8); BOM-less UTF-16 only with clear alternating-zero evidence; otherwise Latin-1 (any bytes)."""
    for enc in ("utf-8", "utf-16-le", "utf-16-be"):
        if raw.startswith(BOMS[enc]):
            try:
                return enc, True, raw[len(BOMS[enc]):].decode(enc)
            except UnicodeDecodeError:
                break
    if b"\0" not in raw:
        try:
            return "utf-8", False, raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    if len(raw) >= 4 and len(raw) % 2 == 0:
        even, odd = raw[0::2], raw[1::2]
        ze, zo = even.count(0) / len(even), odd.count(0) / len(odd)
        for enc, zeros, other in (("utf-16-le", zo, ze), ("utf-16-be", ze, zo)):
            if zeros >= 0.9 and other <= 0.05:
                try:
                    return enc, False, raw.decode(enc)
                except UnicodeDecodeError:
                    pass
    return "latin-1", False, raw.decode("latin-1")


def _endings(text):
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    counts = {"crlf": crlf, "lf": lf, "cr": cr}
    main = max(counts, key=lambda k: (counts[k], k == "lf")) if any(counts.values()) else (
        "crlf" if plat.NAME == "windows" else "lf")
    return {"main": main.upper(), "mixed": sum(1 for v in counts.values() if v) > 1, "counts": counts}


def _looks_binary(raw, enc):
    if enc.startswith("utf-16"):
        return False
    if b"\0" in raw:
        return True
    if enc == "latin-1" and raw:
        ctrl = sum(1 for c in raw if c < 32 and c not in (9, 10, 12, 13, 27, 8))
        return ctrl / len(raw) > 0.3
    return False


def _version_token(st, digest):
    return f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{digest}"


def _read_verified(path, limit=None):
    f, st = open_for_read(path)
    with f:
        data = f.read() if limit is None else f.read(limit + 1)
    return data, st


def _same_version(st, data, token):
    try:
        dev, ino, size, mtime, digest = str(token).split(":")
    except ValueError:
        return False
    return safefs.same_id(st, (int(dev), int(ino))) and (st.st_size, st.st_mtime_ns) == (int(size), int(mtime)) and \
        hashlib.sha256(data).hexdigest() == digest


@api
def fm_edit_open(b):
    path = clean_abs(b.get("path"))
    f, st = open_for_read(path)
    with f:
        if st.st_size > MAX_EDIT:                    # known from the size alone: nothing is read or hashed
            return {"version": None, "data": "", "encoding": None, "bom": False, "endings": None, "writable": False,
                    "reason": "This file is over 5 MB. Opened in the viewer.", "binary": False, "tooLarge": True}
        raw = f.read(MAX_EDIT + 1)
    enc, bom, text = _detect_encoding(raw)
    binary = _looks_binary(raw, enc)
    why = plat.critical(path)
    if why is None and (getattr(st, "st_file_attributes", 0) & 0x1 if plat.NAME == "windows" else not os.access(path, os.W_OK)):
        why = "This file is read-only."
    out = {"version": _version_token(st, hashlib.sha256(raw).hexdigest()),
           "data": "" if binary else base64.b64encode(text.encode("utf-8", "surrogatepass")).decode("ascii"),
           "encoding": enc, "bom": bom, "endings": _endings(text), "writable": why is None, "binary": binary,
           "tooLarge": False}
    if why:
        out["reason"] = why
    return out


# ---- the recoverable replacement transaction (editor saves; extract/pack/checksum replacements)
_FAULTS = set()            # tests: steps that fail on purpose ("backup", "temp", "commit", "second", "rollback", "trash")
SWAP_MODE = None           # tests: force "renames" (the path used without ReplaceFileW or hard links) or "link"


def _encode_text(text, enc, bom):
    if enc not in ("utf-8", "utf-16-le", "utf-16-be", "latin-1"):
        raise FmError("Hoard doesn't know that encoding.")
    try:
        data = text.encode(enc)
    except UnicodeEncodeError:
        if enc == "latin-1":
            raise FmError("Some characters can't be saved as Latin-1.", code="needs_utf8")
        raise FmError("Some characters can't be saved in this encoding.")
    return (BOMS[enc] if bom and enc in BOMS else b"") + data


def _plan_edit_save(plan, b):
    srcs = b.get("sources") or []
    if len(srcs) != 1:
        raise FmError("Save one file at a time.")
    path = clean_abs(srcs[0])
    why = plat.critical(path)
    if why:
        raise FmError(why)
    try:
        text = base64.b64decode(b.get("data") or "", validate=True).decode("utf-8", "surrogatepass")
    except (ValueError, UnicodeDecodeError):
        raise FmError("Bad request.")
    data = _encode_text(text, b.get("encoding") or "utf-8", bool(b.get("bom")))
    try:
        cur, st = _read_verified(path, MAX_EDIT)
    except FmError:
        raise FmError(EDIT_CHANGED)
    if not _same_version(st, cur, b.get("version")):
        raise FmError(EDIT_CHANGED)
    plan["items"].append({"src": path, "dst": path, "kind": "f", "size": len(data), "id": _idl(st), "key": path})
    plan["data"], plan["version"] = data, b.get("version")


def _replace_contents(job, d, name, expect, data, check=None):
    """Save new bytes over a file: the current bytes are backed up (fsynced) first, the new ones written to a
    same-folder temp (fsynced) with the original's metadata, then _commit_swap. Returns the new lstat.
    check(st, bytes) -> bool verifies the current version (identity, size, mtime, digest) before and at commit."""
    st = d.check(name, expect)
    f, fst = d.open_read(name, safefs.ident(st))
    tmp = f".hoard-{job.id}-{next(_TMPN)}.part"
    made = False
    try:
        with f:
            old = f.read()
            if check is not None and not check(fst, old):
                raise Unsafe(EDIT_CHANGED)
            _fault("backup")
            os.makedirs(BACKUPS, exist_ok=True)
            backup = os.path.join(BACKUPS, f"{job.id}-{next(_TMPN)}.bin")
            with open(backup, "wb") as bf:
                bf.write(old)
                bf.flush()
                os.fsync(bf.fileno())
            _fault("temp")
            fo = d.create(tmp)
            made = True
            with fo:
                fo.write(data)
                fo.flush()
                _preserve_meta(f, fst, fo)
                os.fsync(fo.fileno())

        def at_commit(now):                          # identity, size, mtime and the bytes themselves, once more
            if check is None:
                return True
            g, gst = d.open_read(name, safefs.ident(st))
            with g:
                return check(gst, g.read())
        new = _commit_swap(job, d, tmp, name, safefs.ident(st), check=at_commit, record_trash=False)
    except BaseException:
        if made:
            try:
                if d.exists(tmp):
                    d.unlink(tmp, None)
            except (OSError, Unsafe):
                pass
        raise
    job.record({"t": "edit", "path": d.child(name), "id": _idl(new), "kind": "f", "size": new.st_size,
                "mtime": new.st_mtime_ns, "backup": backup})
    return new


def _exec_edit_save(job, plan, items):
    """One action; the job is 'done' only if it committed (result.version is then the new version)."""
    it = items[0]

    def one():
        _refuse_critical(it["src"])
        d, name = safefs.open_parent(it["src"])
        with d:
            def check(st, old):
                return _same_version(st, old, plan["version"])
            return _replace_contents(job, d, name, safefs.ident(d.lstat(name)), plan["data"], check)
    new = _guarded(job, it["src"], one)
    job.tick()
    if not new:
        job.counts["failed"] += 1
        raise JobFailed
    job.counts["done"] += 1
    job.add_bytes(len(plan["data"]))
    job.result = {"result": None, "outputs": [it["src"]],
                  "version": _version_token(new, hashlib.sha256(plan["data"]).hexdigest())}


def _undo_edit(job, a):
    """Put the previous version back with the same transaction (the edited version goes to the Trash)."""
    _refuse_critical(a["path"])
    try:
        with open(a["backup"], "rb") as bf:
            old = bf.read()
    except OSError:
        raise Unsafe("Hoard no longer has the previous version of this file.")
    d, name = safefs.open_parent(a["path"], strict=True)
    with d:
        st = _expect_unchanged(d, name, a)
        _replace_contents(job, d, name, safefs.ident(st), old,
                          lambda s, cur: (s.st_size, s.st_mtime_ns) == (a["size"], a["mtime"]))
    return True


def _fp(path):
    """The identity a plan records for path (see safefs.ident)."""
    return _idl(os.lstat(path))


def _row_kind(row):
    side = row.get("left") or row.get("right")
    return side["k"] if side else None


def _tree_matches(job, it):
    """Is the folder still exactly the reviewed tree (same names, hidden ones included, same items)?"""
    d, name = safefs.open_parent(it["src"])
    with d:
        st = d.check(name, it["id"])
        cur = {}
        with d.sub(name, safefs.ident(st)) as sub:
            _scan_tree(sub, "", cur, job)
    rec = it["tree"]
    if set(cur) != set(rec):
        return False
    for rel, ver in rec.items():
        s = cur[rel]
        if s is None or _kind(s) != ver["kind"] or not safefs.same_id(s, ver["id"]):
            return False
        if ver["kind"] == "f" and (s.st_size, s.st_mtime_ns) != (ver["size"], ver["mtime"]):
            return False
    return True


class _Replace:
    """A destination to be replaced only once the new output is complete (see _commit_swap)."""
    def __init__(self, name, st):
        self.name, self.st = name, st


def _unchanged(old):
    """A commit-time check: the destination still has the size and modified time seen when deciding."""
    return lambda st: (st.st_size, st.st_mtime_ns) == (old.st_size, old.st_mtime_ns)


def _target_name(target):
    return target.name if isinstance(target, _Replace) else target


class JobFailed(Exception):
    """The job's one action didn't happen (its reason is already in the job's errors)."""


class _Stranded(Unsafe):
    """A failed replacement whose rollback also failed: the original sits under its temporary name."""


def _fault(step):
    if step in _FAULTS:
        raise OSError(errno.EIO, f"Injected failure at {step}.")


def _swap_mode():
    if SWAP_MODE:
        return SWAP_MODE
    return "replacefile" if plat.NAME == "windows" else "link"


def _rollback(d, prev, name):
    """Give the original its name back after a failed swap; if even that fails, say exactly where it is."""
    try:
        _fault("rollback")
        d.rename_to(prev, d, name, guard_src=False)
    except (OSError, Unsafe) as ex:
        raise _Stranded(f"Saving failed and Hoard couldn't put the original back ({_plain(ex) if isinstance(ex, OSError) else ex}). "
                        f"It's safe as ‘{prev}’ in {d.path}: rename it to ‘{name}’.")


def _same_after_rename(now, before, folder):
    """Is `now` (after a rename) the item `before` was? By file id where ids survive renames; on FAT/exFAT by
    size, modified and created times."""
    if (now.st_size, now.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        return False
    if plat.stable_ids(folder):
        return safefs.same_id(now, safefs.ident(before))
    return now.st_dev == before.st_dev and safefs.birth(now) is not None and safefs.birth(now) == safefs.birth(before)


def _commit_swap(job, d, tmp, name, expect, check=None, record_trash=True):
    """Put the complete, flushed temp file `tmp` in the place of `name`, recoverably. The pending record (on disk)
    stays until the swap or its rollback is verified.
      Windows: ReplaceFileW, keeping the original's ACLs, attributes, creation time, streams, encryption and
               compression; the original survives as '<name> (previous version)'.
      POSIX:   a hard link keeps the original as the previous version, then rename(2) over the name (dir_fd).
      Neither: rename the original aside, then the temp into place; any failure renames the original back.
    `check(st)` revalidates the original just before. Afterwards the previous version goes to the Trash and (with
    record_trash) is journaled so undo puts it back. Returns the new lstat."""
    st = d.check(name, expect)
    if not st.st_ino:
        raise Unsafe(safefs.NO_ID)
    if check is not None and not check(st):
        raise Unsafe(EDIT_CHANGED)
    stem, ext = os.path.splitext(name)
    prev = f"{stem} (previous version){ext}"
    if d.exists(prev):
        prev = _free_in(d, prev)
    token = uuid.uuid4().hex
    _pending_add({"job": job.id, "token": token, "kind": "swap", "dir": d.path, "name": name, "tmp": tmp,
                  "prev": prev})
    keep = False
    try:
        mode = _swap_mode()
        if mode == "replacefile":
            _fault("commit")
            ok, err = plat.replace_file(d.child(name), d.child(tmp), d.child(prev))
            if not ok:
                if not d.exists(name) and d.exists(prev):
                    _rollback(d, prev, name)
                raise OSError(errno.EIO, f"Windows couldn't replace the file (error {err}).", d.child(name))
        elif mode == "link" and d.link(name, prev, safefs.ident(st)):
            pst = d.lstat(prev)
            if not safefs.same_id(pst, safefs.ident(st)) or (pst.st_size, pst.st_mtime_ns) != (st.st_size, st.st_mtime_ns) \
                    or (check is not None and not check(pst)):
                d.unlink(prev, None)
                raise Unsafe(EDIT_CHANGED)
            try:
                _fault("commit")
                d.replace(tmp, name, safefs.ident(st))
            except BaseException:
                d.unlink(prev, None)                 # only a second name of the untouched original
                raise
        else:
            _fault("commit")
            d.rename_to(name, d, prev, safefs.ident(st))
            try:
                pst = d.lstat(prev)
                if (pst.st_size, pst.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                    raise Unsafe(EDIT_CHANGED)
                _fault("second")
                d.rename_to(tmp, d, name, guard_src=False)
            except BaseException:
                _rollback(d, prev, name)
                raise
        pst = d.lstat(prev)                           # the original is now 'prev': was it really the one checked?
        if not _same_after_rename(pst, st, d.path):
            ok, _err = plat.replace_file(d.child(name), d.child(prev), None) if mode == "replacefile" else (False, 0)
            if not ok:
                raise _Stranded(f"This file changed on disk while saving. The other version is kept as ‘{prev}’ "
                                f"in {d.path}.")
            raise Unsafe(EDIT_CHANGED)
        new = d.lstat(name)
        try:
            _fault("trash")
            ok, err, where = d.trash(prev, safefs.ident(pst))      # pst: prev as it is now (ids may differ on FAT)
        except (Unsafe, OSError) as ex:
            ok, err, where = False, str(ex), None
        if ok and record_trash:
            job.record({"t": "trash", "path": d.child(name), "where": where, "id": _idl(pst)})
        elif not ok:
            job.error(d.child(prev), f"The previous version couldn't go to the {plat.TRASH_NAME}, so it's kept "
                                     f"as ‘{prev}’. {err or ''}".strip())
        return new
    except _Stranded:
        keep = True                                   # recovery (startup) needs the record
        raise
    finally:
        if not keep:
            _pending_drop_token(token)


def _preserve_meta(src_f, st, tmp_f):
    """POSIX: give the new file the original's mode, owner and extended attributes, or refuse the save."""
    if plat.NAME == "windows":
        return                                        # ReplaceFileW carries the metadata over
    fd = tmp_f.fileno()
    os.fchmod(fd, stat.S_IMODE(st.st_mode))
    now = os.fstat(fd)
    if (now.st_uid, now.st_gid) != (st.st_uid, st.st_gid):
        try:
            os.fchown(fd, st.st_uid, st.st_gid)
        except OSError:
            raise Unsafe("Hoard can't keep this file's owner, so it won't save over it.")
    if hasattr(os, "listxattr"):
        try:
            for n in os.listxattr(src_f.fileno()):
                os.setxattr(fd, n, os.getxattr(src_f.fileno(), n))
        except OSError as ex:
            if ex.errno not in (errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
                raise Unsafe("Hoard can't keep this file's extended attributes, so it won't save over it.")
