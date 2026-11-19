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
import itertools
import json
import os
import re
import stat
import threading
import time
import traceback
import uuid

import core
import plat
import safefs

# Set by server.py (avoids a circular import):
#   scan(path) -> (node or None, scan time or None)   Hoard's last scan of the folder at path
#   volumes() -> [volume dicts]                         connected disks and added folders
HOOKS = {"scan": None, "volumes": None}

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
    """A plain-language refusal, returned to the client as {"error": message}."""


class Stop(Exception):
    """The job was cancelled, or the user chose Abort."""


class Changed(OSError):
    """The item at a path is no longer the one that was chosen."""


def api(fn):
    def wrapper(b):
        try:
            return fn(b if isinstance(b, dict) else {})
        except (FmError, ValueError) as ex:
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
        self.created, self.finished = time.time(), None
        self.entry = None                                   # undo journal entry, once something changed
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
        except Exception as ex:  # noqa: BLE001
            core.log_error(f"files job {job.op}", traceback.format_exc())
            job.error(job.current, _plain(ex) if isinstance(ex, OSError) else str(ex))
            final = "failed"
    job.question = None
    job.current = ""
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
               "mkfile": "Created", "delete": "Deleted", "undo": "Undid", "sizes": "Measured", "find": "Searched"}


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
        if rec.get("job") in JOBS:
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
    for text, jid in notes:
        _history().append({"id": uuid.uuid4().hex[:12], "op": "recover", "summary": text, "time": int(time.time()),
                           "undo": jid})
        NOTICES.append(text)
    return [t for t, _ in notes]


# ---------------------------------------------------------------- planning
OPS = ("copy", "move", "trash", "delete", "rename", "rename_many", "mkdir", "mkfile")
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
    plan["dest"], plan["destId"] = real, plat.identity(real)
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
        item = {"src": p, "dst": dst, "kind": kind, "size": _estimate(p, st, deadline), "id": plat.identity(p)}
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
        plan["items"].append({"src": p, "kind": _kind(st), "size": _estimate(p, st, deadline), "id": plat.identity(p)})


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
            plan["items"].append({"src": p, "dst": dst, "kind": _kind(st), "id": plat.identity(p)})


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
             "rename": _plan_rename, "rename_many": _plan_rename, "mkdir": _plan_create, "mkfile": _plan_create}


@api
def fm_plan(b):
    op = b.get("op")
    if op not in OPS:
        raise FmError("Hoard doesn't know that operation.")
    _expire_plans()
    opts = b.get("options") if isinstance(b.get("options"), dict) else {}
    plan = {"id": uuid.uuid4().hex[:12], "op": op, "time": time.time(), "items": [], "blocked": [], "conflicts": [],
            "dest": None, "destId": None, "preserveTimes": bool(opts.get("preserveTimes", True))}
    _PLANNERS[op](plan, b)
    PLANS[plan["id"]] = plan
    items = [{k: v for k, v in it.items() if k in ("src", "dst", "size", "kind", "merge", "dup") and v is not None}
             for it in plan["items"]]
    return {"plan": plan["id"], "op": op, "items": items, "blocked": plan["blocked"], "conflicts": plan["conflicts"],
            "total": {"n": len(items), "bytes": sum(it.get("size") or 0 for it in plan["items"])},
            "undoable": op != "delete"}


def _title(plan, n):
    dest = plan.get("dest")
    first = os.path.basename((plan["items"][0].get("src") or plan["items"][0]["dst"])) if plan["items"] else ""
    return {"copy": f"Copying {_items(n)} to {dest}", "move": f"Moving {_items(n)} to {dest}",
            "trash": f"Moving {_items(n)} to the {plat.TRASH_NAME}", "delete": f"Deleting {_items(n)} for good",
            "rename": f"Renaming ‘{first}’", "rename_many": f"Renaming {_items(n)}",
            "mkdir": f"Creating folder ‘{first}’", "mkfile": f"Creating file ‘{first}’"}[plan["op"]]


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
    items = [it for it in plan["items"] if _key(it.get("src") or it["dst"]) not in exclude]
    if not items:
        raise FmError("There's nothing left to do.")
    PLANS.pop(plan["id"], None)                              # a plan authorises one run
    job = Job(plan["op"], _title(plan, len(items)))
    job.mode, job.preserve, job.total_items = mode, plan["preserveTimes"], len(items)
    job.total_n = len(items)
    job.total_b = sum(it.get("size") or 0 for it in items)
    _submit(job, lambda j: _EXEC[plan["op"]](j, plan, items))
    return {"job": job.id}


# ---------------------------------------------------------------- running operations
# Every change goes through safefs.Dir: the folder is opened through a verified chain and held, and each item is
# re-checked (identity, protection, same disk, not a link when entered) right before it is touched.
Unsafe = safefs.Unsafe
MERGE = object()


def _idl(st):
    return [st.st_dev, st.st_ino, stat.S_ISDIR(st.st_mode) and not plat.is_link(st)]


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
    rule = job.rule or job.mode
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


def _resolve_dst(job, src, sst, dd, name, move):
    """Deal with something already at dd/name. Returns the name to write, MERGE, or None (skip)."""
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
        if (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns) != (sst.st_dev, sst.st_ino, sst.st_size, sst.st_mtime_ns):
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
         "rename": _exec_rename, "rename_many": _exec_rename, "mkdir": _exec_create, "mkfile": _exec_create}


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
            return st is not None and safefs.ident(st) == tuple(a["id"][:2]) and \
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
