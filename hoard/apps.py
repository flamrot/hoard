"""Installed apps with their size, plus data folders that probably belong to them."""
import os
import re

import plat

GENERIC = {"the", "for", "and", "app", "apps", "x64", "x86", "64bit", "32bit", "bit", "version", "update",
           "windows", "win", "inc", "llc", "ltd", "corporation", "corp", "software", "edition", "desktop",
           "client", "setup", "installer", "tools", "tool", "runtime", "microsoft", "google", "the"}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def tokens(app):
    words = [w for w in re.split(r"[^a-z0-9]+", re.sub(r"\(.*?\)|\d+(\.\d+)+", " ", app["name"].lower())) if w]
    toks = {norm(" ".join(w for w in words if w not in {"x64", "x86"}))}
    toks |= {w for w in words if len(w) >= 5 and w not in GENERIC}
    if app.get("bundle"):
        toks.add(norm(app["bundle"].split(".")[-1]))
    return {t for t in toks if len(t) >= 4}


def data_roots():
    H = os.path.expanduser("~")
    if plat.NAME == "windows":
        L, R = os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")
        return [p for p in (L, R, os.environ.get("ProgramData", ""), os.path.join(L, "Programs")) if p]
    if plat.NAME == "macos":
        lib = os.path.join(H, "Library")
        return [os.path.join(lib, d) for d in ("Application Support", "Caches", "Containers", "Logs",
                                               "Saved Application State", "Preferences", "HTTPStorages")]
    return [os.path.join(H, d) for d in (".config", ".local/share", ".cache", ".var/app")]


def matches(name, toks, app):
    n = norm(name)
    if not n:
        return False
    if app.get("bundle") and name.lower().startswith(app["bundle"].lower()):
        return True
    return any(t == n or (len(t) >= 5 and t in n) or (len(n) >= 5 and n in t) for t in toks)


def related(app, children):
    """children(path) -> [(name, size, abs_path, is_dir)] for a folder (from the scan, or live)."""
    toks = tokens(app)
    pub = norm((app.get("publisher") or "").split(" ")[0]) if app.get("publisher") else ""
    out = []
    location = os.path.normcase(app.get("location") or "")
    for root in data_roots():
        for name, size, path, is_dir in children(root):
            if location and os.path.normcase(path) == location:
                continue
            if matches(name, toks, app):
                out.append({"path": path, "name": name, "size": size, "kind": "d" if is_dir else "f"})
            elif is_dir and pub and len(pub) >= 4 and norm(name) == pub:     # Publisher\App layout
                for n2, s2, p2, d2 in children(path):
                    if matches(n2, toks, app):
                        out.append({"path": p2, "name": f"{name}{os.sep}{n2}", "size": s2, "kind": "d" if d2 else "f"})
    return sorted(out, key=lambda r: -r["size"])
