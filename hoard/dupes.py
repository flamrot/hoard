"""Duplicate finder: same size -> same sampled bytes -> same full BLAKE2 hash. Hard links are not duplicates."""
import hashlib
import os
import threading

SAMPLE = 64 * 1024
CHUNK = 4 << 20


def _sample(path, size):
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        for off in (0, max(0, size // 2 - SAMPLE // 2), max(0, size - SAMPLE)):
            f.seek(off)
            h.update(f.read(SAMPLE))
    return h.digest()


def _full(path, job):
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as f:
        while True:
            if job["cancel"]:
                raise InterruptedError
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            job["done"] += len(b)
    return h.digest()


def _regroup(groups, key):
    out = []
    for g in groups:
        buckets = {}
        for f in g:
            try:
                buckets.setdefault(key(f), []).append(f)
            except OSError:
                pass
        out += [b for b in buckets.values() if len(b) > 1]
    return out


def run(job, files):
    """files: dicts with path (absolute), p (parts), size, m (mtime), c (category)."""
    try:
        by_size = {}
        for f in files:
            by_size.setdefault(f["size"], []).append(f)
        groups = [g for g in by_size.values() if len(g) > 1]

        def inode(f):
            s = os.stat(f["path"])
            return (s.st_dev, s.st_ino) if s.st_ino else f["path"]
        cleaned = []
        for g in groups:                       # the same file reached twice (hard link) isn't a duplicate
            seen, keep = set(), []
            for f in g:
                try:
                    k = inode(f)
                except OSError:
                    continue
                if k not in seen:
                    seen.add(k)
                    keep.append(f)
            if len(keep) > 1:
                cleaned.append(keep)
        job["phase"] = "Comparing samples"
        groups = _regroup(cleaned, lambda f: _sample(f["path"], f["size"]))
        job["phase"] = "Comparing contents"
        job["total"] = sum(f["size"] for g in groups for f in g)
        job["done"] = 0
        groups = _regroup(groups, lambda f: _full(f["path"], job))
        result = []
        for g in groups:
            for f in g:                     # use the live date (older scans don't record one)
                try:
                    f["m"] = int(os.stat(f["path"]).st_mtime)
                except OSError:
                    pass
            g.sort(key=lambda f: f["m"])
            result.append({"size": g[0]["size"], "wasted": g[0]["size"] * (len(g) - 1), "c": g[0]["c"],
                           "files": [{"p": f["p"], "path": f["path"], "m": f["m"]} for f in g]})
        result.sort(key=lambda r: -r["wasted"])
        job["groups"] = result
    except InterruptedError:
        job["cancelled"] = True
    except Exception as ex:  # noqa: BLE001
        job["error"] = str(ex)
    finally:
        job["running"] = False


def start(files):
    job = {"running": True, "phase": "Grouping by size", "done": 0, "total": 0, "groups": None,
           "cancel": False, "cancelled": False, "error": None, "candidates": len(files)}
    threading.Thread(target=run, args=(job, files), daemon=True).start()
    return job
