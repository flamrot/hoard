"""Platform layer: everything that differs between Windows, macOS and Linux lives behind this module.

Each OS module provides:
    NAME, DATA_DIR, TRASH_NAME, REVEAL_NAME
    size_of(stat_result) -> int           bytes the file really uses on disk
    list_volumes() -> [dict]              id, root, name, label, type, total, used, free
    scan_excludes(root) -> set[str]       absolute paths the scanner must not enter
    trash(path) -> (ok: bool, error: str | None)
    reveal(path) -> None                  open the folder / select the file in the file manager
    protected(root, parts, full) -> str | None   reason the item may not be deleted
    app_browsers() -> [str]               Chromium-style browsers that support --app=URL
"""
import os
import re
import shutil
import subprocess
import sys
import time

if sys.platform == "win32":
    from . import windows as _os
elif sys.platform == "darwin":
    from . import macos as _os
else:
    from . import linux as _os

NAME = _os.NAME
DATA_DIR = os.environ.get("HOARD_DATA_DIR") or _os.DATA_DIR
TRASH_NAME = _os.TRASH_NAME
REVEAL_NAME = _os.REVEAL_NAME
size_of = _os.size_of
cloud_size = _os.cloud_size                # bytes of an online-only (cloud placeholder) file, else 0
list_volumes = _os.list_volumes
scan_excludes = _os.scan_excludes
trash = _os.trash
reveal = _os.reveal
protected = _os.protected
schedule_install = _os.schedule_install    # (hour, minute, argv) -> (ok, error)
schedule_remove = _os.schedule_remove      # () -> (ok, error)
schedule_status = _os.schedule_status      # () -> {"installed": bool, "how": str}
SCHEDULE_NOTE = _os.SCHEDULE_NOTE
notify = _os.notify                        # (title, body) -> None, best effort
bin_size = _os.bin_size                    # () -> bytes in the Recycle Bin / Trash, or None
empty_bin = _os.empty_bin                  # () -> (ok, error)
cleanup_specs = _os.cleanup_specs          # () -> [cleanup target dicts], see cleanup.py
installed_apps = _os.installed_apps        # () -> [app dicts], see apps.py
run_uninstall = _os.run_uninstall          # (app) -> (ok, error)
HIDDEN_NOTE = _os.HIDDEN_NOTE              # why "used" space can exceed what the scan sees
context_menu_install = _os.context_menu_install   # (argv) -> (ok, error): "Open in Hoard" in the file manager
context_menu_remove = _os.context_menu_remove
context_menu_status = _os.context_menu_status
alloc_size = getattr(_os, "alloc_size", None)
dir_info = getattr(_os, "dir_info", None)       # (folder) -> {name: (file id, allocated)}: Windows only   # (path, logical) -> bytes really allocated (Windows sparse/compressed)


def file_info(path):
    """Owner, attributes and permissions of a file or folder, for the details panel."""
    if NAME == "windows":
        return _os.file_info(path)
    import grp
    import pwd
    import stat as st_
    s = os.lstat(path)
    try:
        owner = pwd.getpwuid(s.st_uid).pw_name
    except KeyError:
        owner = str(s.st_uid)
    try:
        group = grp.getgrgid(s.st_gid).gr_name
    except KeyError:
        group = str(s.st_gid)
    mode = st_.filemode(s.st_mode)

    def words(rwx):
        w = [n for c, n in zip(rwx, ("Read", "Write", "Run")) if c != "-"]
        return ", ".join(w) or "No access"
    attrs = []
    if os.path.basename(path).startswith("."):
        attrs.append("Hidden")
    if getattr(s, "st_flags", 0) & 0x2:
        attrs.append("Locked")
    if st_.S_ISLNK(s.st_mode):
        attrs.append("Symbolic link")
    return {"owner": owner, "attrs": attrs, "mode": mode,
            "perms": [{"who": f"{owner} (owner)", "can": words(mode[1:4])},
                      {"who": f"{group} (group)", "can": words(mode[4:7])},
                      {"who": "Everyone else", "can": words(mode[7:10])}]}


def owner_of(path):
    if NAME == "windows":
        return _os.owner_of(path)
    import pwd
    try:
        return pwd.getpwuid(os.lstat(path).st_uid).pw_name
    except (OSError, KeyError):
        return "Unknown"


def _known_folder(guid_hex, xdg_name, fallback):
    if NAME == "windows":
        try:
            import ctypes
            buf = ctypes.c_wchar_p()
            guid = (ctypes.c_byte * 16).from_buffer_copy(bytes.fromhex(guid_hex))
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(buf)) == 0:
                path = buf.value
                ctypes.windll.ole32.CoTaskMemFree(buf)
                return path
        except Exception:  # noqa: BLE001
            pass
    if NAME == "linux":
        try:
            r = subprocess.run(["xdg-user-dir", xdg_name], capture_output=True, text=True, env=host_env())
            got = r.stdout.strip()
            # an unset XDG folder comes back as the home folder itself: that is not "Documents"
            if r.returncode == 0 and got and os.path.normpath(got) != os.path.normpath(os.path.expanduser("~")):
                return got
        except OSError:
            pass
    return os.path.join(os.path.expanduser("~"), fallback)


def downloads_dir():
    return _known_folder("90e24d373f126545916439c4925e467b", "DOWNLOAD", "Downloads")    # FOLDERID_Downloads


def documents_dir():
    return _known_folder("d09ad3fd8f23af46adb46c85480369c7", "DOCUMENTS", "Documents")   # FOLDERID_Documents


def open_default(path):
    """Open a file with its default app."""
    if NAME == "windows":
        os.startfile(path)
    elif NAME == "macos":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=host_env())


def self_command():
    """argv that starts this same app again (used for the scheduled scan)."""
    if getattr(sys, "frozen", False):
        return [os.environ.get("APPIMAGE") or sys.executable]   # an AppImage runs from a temp mount
    exe = sys.executable
    if NAME == "windows":
        pw = os.path.join(os.path.dirname(exe), "pythonw.exe")   # no console window
        if os.path.exists(pw):
            exe = pw
    return [exe, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server.py")]

os.makedirs(DATA_DIR, exist_ok=True)


def slug(text):
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return s[:60] or "root"


def vol_id(prefix, key):
    """A volume id that can't collide: a readable part plus a hash of the exact root (or remote name).
    Older versions used the readable part alone, so 'D:\\archive one' and 'D:\\archive-one' shared an id."""
    import hashlib
    h = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:10]
    return f"{prefix}-{slug(key)[:40]}-{h}"


def legacy_vol_id(prefix, key):
    """The id older versions gave this volume (used once, to move its saved scans to the new id)."""
    return f"{prefix}-{slug(key)}"


# ---------------------------------------------------------------- what Hoard shouldn't *suggest* removing
BOGUS_BEFORE = 631152000          # 1990-01-01: earlier "last changed" dates are placeholders, not real use

_MANAGED_SEGMENTS = {"appdata", "program files", "program files (x86)", "programdata", "windows", "windowsapps",
                     "system volume information", "recovery", "perflogs", "steamapps", "steamlibrary", "epic games",
                     "xboxgames", "gog games", "node_modules", "site-packages", "library", "applications"}
_MANAGED_FILES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log", "swapfile", "swap.img"}


def _managed_prefixes():
    home = os.path.expanduser("~")
    if NAME == "windows":
        roots = [os.environ.get(v) for v in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
                                             "LOCALAPPDATA", "APPDATA")]
    elif NAME == "macos":
        roots = ["/System", "/Library", "/Applications", "/private", "/usr", "/opt", "/bin", "/sbin",
                 os.path.join(home, "Library")]
    else:
        roots = ["/usr", "/var", "/opt", "/snap", "/etc", "/boot", "/lib", "/lib64", "/bin", "/sbin", "/proc", "/sys"]
    return [os.path.normcase(r.rstrip("\\/")) for r in roots if r]


def managed(root, parts):
    """True for things the OS or an installed app manages: system and program folders, app data and
    browser profiles, game libraries, the Recycle Bin, hidden folders, app bundles. These can still be
    removed by hand from the map, but Hoard never *suggests* them in its friendly lists."""
    segs = [p.casefold() for p in parts]
    if not segs:
        return True
    if segs[-1] in _MANAGED_FILES:
        return True
    for s in segs[:-1]:
        if s.startswith((".", "$")) or s.endswith(".app") or s in _MANAGED_SEGMENTS:
            return True
    full = os.path.normcase(os.path.join(root, *parts))
    return any(full == p or full.startswith(p + os.sep) for p in _managed_prefixes())


# ---------------------------------------------------------------- links and identity (checked right before removing)
def is_link(st):
    """A symlink, or a Windows junction / mount point / symlink (name-surrogate reparse point).
    Cloud placeholders (OneDrive etc.) are reparse points too, but not links, so they don't count."""
    import stat as _stat
    if _stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & 0x400 and getattr(st, "st_reparse_tag", 0) & 0x20000000)


def linked_ancestor(root, parts):
    """The first folder between root and the item that is a link (or no longer a folder), else None."""
    import stat as _stat
    cur = root
    for name in parts[:-1]:
        cur = os.path.join(cur, name)
        try:
            st = os.lstat(cur)
        except OSError:
            return None                       # missing: the caller finds the item gone
        if is_link(st) or not _stat.S_ISDIR(st.st_mode):
            return cur
    return None


def identity(path):
    """(device, file id, is folder, modified time in ns, size) of the item at path itself, not following links;
    time and size are 0 for folders. They matter on Linux, which hands a deleted file's inode number to the next
    new file at once, and whose file clock ticks only every few milliseconds, so the time alone can match too."""
    import stat as _stat
    st = os.lstat(path)
    folder = _stat.S_ISDIR(st.st_mode)
    return [int(st.st_dev), int(st.st_ino), folder, 0 if folder else int(st.st_mtime_ns), 0 if folder else int(st.st_size)]


def pin_dir(path, expect=None):
    """Hold a folder while clearing it. Returns a token, or None when it's a link, not a folder, or not the
    folder we listed (`expect` = its file id). On Windows the handle also stops it being renamed or replaced."""
    if NAME == "windows":
        return _os.pin_dir(path, expect)
    import stat as _stat
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if is_link(st) or not _stat.S_ISDIR(st.st_mode) or (expect and st.st_ino != expect):
        return None
    return path


def unpin_dir(token):
    if NAME == "windows" and token is not None:
        _os.unpin_dir(token)


# ---------------------------------------------------------------- system-critical items (Files view and every removal)
# Never deleted, trashed, moved, renamed or replaced, whatever the request. Two independent checks, both must pass:
#   by path:     the canonical path (Windows resolves it itself: subst/mapped drives, junctions, short names, case)
#                against the rules; a folder that *contains* a critical item is critical too;
#   by identity: (device, file id) of the item and of every folder above it, against the protected items and
#                tree roots, so an alias (bind mount, subst drive, second path) can't get around the path rules.
# Writes through administrative or loopback shares are refused, and so is anything Hoard can't resolve.
UNSURE = "Protected: Hoard can't confirm what this is or where it really is, so it won't change it."
ADMIN_SHARE = ("Protected: Hoard doesn't change files through an administrative or loopback network share. "
               "Open the drive itself instead.")
TEST_PROTECT = {"trees": [], "items": []}      # extra paths protected by the tests (never used by the app)


def strip_long(p):
    """'\\\\?\\C:\\x' -> 'C:\\x', '\\\\?\\UNC\\srv\\share' -> '\\\\srv\\share' (Windows long-path prefixes)."""
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def canonical(path, follow=False):
    """Where the item really is: links *above* it resolved, the item itself only with `follow` (operations act on a
    link, never through it; a folder something is written *into* is followed). Windows asks the OS for the final path of the longest existing part (handle opened without
    following a final link), which also resolves subst and mapped drives. Raises OSError if it can't tell."""
    p = os.path.abspath(strip_long(path))
    if NAME == "windows":
        head, tail = p, []
        while True:
            got, err = _os.final_path(head, follow=follow or bool(tail))   # folders above the item: follow links
            if got:
                return os.path.join(strip_long(got), *tail)
            if err not in (2, 3, 21, 53, 67, 123, 161):  # not found / not ready / bad name: look one level up
                if tail or err not in (5, 32):          # access denied or in use on the item itself: use its parent
                    raise OSError(err, UNSURE, head)
            parent, name = os.path.split(head)
            if parent == head:
                return os.path.join(head, *tail)        # nothing exists, not even the drive
            tail.insert(0, name)
            head = parent
    try:
        link = is_link(os.lstat(p))
    except (OSError, ValueError):
        link = False
    if link and not follow:
        head, tail = os.path.split(p)
        return os.path.join(os.path.realpath(head), tail)
    return os.path.realpath(p)


def path_key(p):
    """Comparable form of an absolute path: case folded where the OS ignores case; on Windows trailing dots and
    spaces dropped from every name (Windows ignores them: 'C:\\Windows.' is C:\\Windows)."""
    k = os.path.normcase(p)
    if NAME == "windows":
        drive, rest = os.path.splitdrive(k)
        k = drive + "\\".join(s.rstrip(" .") for s in rest.split("\\"))
        if not rest.strip("\\"):
            return drive + "\\"                       # a drive or share root
    elif NAME == "macos":
        k = k.casefold()                              # APFS/HFS+ are normally case-insensitive
    return k.rstrip(os.sep) or os.sep


def _is_root_key(k):
    if NAME == "windows":
        drive, rest = os.path.splitdrive(k)
        return bool(drive) and not rest.strip("\\")
    return k == "/"


_SYS_WORD = {"windows": "Windows", "macos": "macOS"}.get(NAME, "Your system")
_KEEP = "Hoard can't delete, move or rename it."
_RULES = {"t": 0.0}
_LOCAL = {}


def _guid_hex(guid):
    import uuid
    return uuid.UUID(guid).bytes_le.hex()


def _profile_folders():
    """(path, label) for the home folder and its core folders, including redirected known folders."""
    home = os.path.expanduser("~")
    out = [(home, "user")]
    if NAME == "windows":
        known = {"Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641", "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
                 "Downloads": "374DE290-123F-4565-9164-39C4925E467B", "Pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
                 "Music": "4BD8D571-6D19-48D3-BE97-422220080E43", "Videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC"}
        for label, guid in known.items():
            out.append((os.path.join(home, label), label))
            out.append((_known_folder(_guid_hex(guid), "", label), label))
        out += [(os.path.join(home, "AppData"), "AppData"), (os.path.join(home, "AppData", "Local"), "AppData\\Local"),
                (os.path.join(home, "AppData", "Roaming"), "AppData\\Roaming"), (os.path.join(home, "OneDrive"), "OneDrive")]
        for var in ("LOCALAPPDATA", "APPDATA"):
            if os.environ.get(var):
                out.append((os.environ[var], "AppData\\Local" if var == "LOCALAPPDATA" else "AppData\\Roaming"))
        for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            if os.environ.get(var):
                out.append((os.environ[var], "OneDrive"))
        return out
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Music", "Library"]
    names += ["Movies", "Applications"] if NAME == "macos" else ["Videos", ".local", ".local/share", ".config"]
    out += [(os.path.join(home, n), n) for n in names]
    if NAME == "linux":
        for xdg, label in (("DESKTOP", "Desktop"), ("DOCUMENTS", "Documents"), ("DOWNLOAD", "Downloads"),
                           ("PICTURES", "Pictures"), ("MUSIC", "Music"), ("VIDEOS", "Videos")):
            p = _known_folder("", xdg, label)
            if os.path.normpath(p) != os.path.normpath(home):     # unset XDG dirs point at home itself
                out.append((p, label))
    return out


def _mount_points():
    if NAME == "windows":
        return []
    try:
        if NAME == "linux":
            return [m for _src, m, _fs in _os._mounts()]
        out = [v["root"] for v in _os.list_volumes()]
        try:
            out += [os.path.join("/System/Volumes", n) for n in os.listdir("/System/Volumes")]
        except OSError:
            pass
        return out
    except Exception:  # noqa: BLE001
        return []


def _local_hosts():
    """Names and addresses that mean 'this computer' in a network path."""
    if not _LOCAL:
        import socket
        names = {"localhost", "127.0.0.1", "::1", "[::1]", "0:0:0:0:0:0:0:1", ".", "?"}
        for n in (os.environ.get("COMPUTERNAME"), socket.gethostname()):
            if n:
                names |= {n.lower(), n.split(".")[0].lower()}
        try:
            host = socket.gethostname()
            names.add(socket.getfqdn(host).lower())
            names |= set(socket.gethostbyname_ex(host)[2])
        except OSError:
            pass
        _LOCAL["names"] = names
    return _LOCAL["names"]


def share_problem(path):
    """Windows: refuse administrative shares (\\\\any\\C$, \\\\any\\ADMIN$) and any share on this computer
    (\\\\localhost\\…, \\\\127.0.0.1\\…, \\\\<this PC>\\…): they're second paths to local system folders."""
    if NAME != "windows":
        return None
    p = strip_long(path).replace("/", "\\")
    if not p.startswith("\\\\"):
        return None
    parts = p[2:].split("\\")
    host = parts[0].lower()
    share = parts[1].lower() if len(parts) > 1 else ""
    if host in _local_hosts() or host.startswith("127.") or host.endswith(".localhost") or host.endswith("ipv6-literal.net"):
        return ADMIN_SHARE
    if share.endswith("$"):
        return ADMIN_SHARE
    return None


def _build_rules():
    """items: key -> (shown path, reason) for the item itself; trees: (key, reason, exempt keys) for it and
    everything inside; names: Windows names protected at the root of any drive. Plus the same as identities:
    id_items / id_trees: (dev, ino) -> (shown, reason); id_exempt: folders inside a tree whose contents are free."""
    items, trees, names = {}, [], {}
    item_paths, tree_paths, exempt_paths = [], [], []
    folder = f"Protected: {_SYS_WORD} needs this folder. {_KEEP}"
    file_ = f"Protected: {_SYS_WORD} needs this file. {_KEEP}"

    def canon(p):
        try:
            return canonical(p)
        except OSError:
            return os.path.abspath(p)

    def add(path, why):
        if path:
            item_paths.append((path, why))
            for p in {os.path.abspath(path), canon(path)}:
                items.setdefault(path_key(p), (p, why))

    def tree(path, why, exempt=()):
        trees.append((path_key(canon(path)), why, tuple(path_key(canon(e)) for e in exempt)))
        tree_paths.append((path, why))
        exempt_paths.extend(exempt)
        add(path, why)

    if NAME == "windows":
        tree(os.environ.get("SystemRoot") or "C:\\Windows", folder)
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData", "PUBLIC"):
            add(os.environ.get(var), folder)
        for n in ("Program Files", "Program Files (x86)", "ProgramData", "Users", "Recovery", "System Volume Information",
                  "$Recycle.Bin", "Boot", "EFI", "$WinREAgent", "PerfLogs", "Windows"):
            names[n.lower()] = folder
        for n in ("pagefile.sys", "hiberfil.sys", "swapfile.sys", "bootmgr", "BOOTNXT", "DumpStack.log",
                  "DumpStack.log.tmp"):
            names[n.lower()] = file_
        try:                                          # the same names on every local drive, by identity too
            roots = [v["root"] for v in _os.list_volumes() if v["type"] in ("fixed", "removable")]
        except Exception:  # noqa: BLE001
            roots = []
        for root in roots:
            item_paths.append((root, f"Protected: this is a whole drive. {_KEEP}"))
            for n, why in names.items():
                item_paths.append((os.path.join(root, n), why))
    else:
        tree("/usr", folder, ["/usr/local"])
        for p in ("/System", "/bin", "/sbin", "/etc", "/private/etc", "/proc", "/sys", "/dev", "/boot"):
            tree(p, folder)
        if NAME == "macos":
            tree("/private/var/db", folder)
            for p in ("/Library/Apple", "/Library/Extensions", "/Library/Frameworks", "/System/Volumes"):
                tree(p, folder)
        else:
            home = os.path.expanduser("~")
            free = ["/var/tmp", "/var/home"] + ([home] if home.startswith("/var/") else [])
            tree("/var", folder, free)                   # system state: /var/lib, /var/log, /var/cache, …
            try:
                for n in os.listdir("/"):
                    if n.startswith("lib"):                       # /lib, /lib32, /lib64, /libx32 …
                        tree("/" + n, folder)
            except OSError:
                pass
        for p in ("/", "/private", "/var", "/private/var", "/tmp", "/private/tmp", "/Library", "/Applications", "/opt",
                  "/Users", "/Volumes", "/usr/local", "/home", "/root", "/srv", "/mnt", "/media", "/run", "/snap",
                  "/cores", "/var/tmp", "/var/home"):
            add(p, folder)
        for m in _mount_points():
            add(m, f"Protected: a disk is mounted here. {_KEEP}")
    for path, label in _profile_folders():
        why = ("Protected: this is your user folder. You can manage what's inside it." if label == "user" else
               f"Protected: this is your {label} folder. You can manage what's inside it.")
        add(path, why)
    tree(DATA_DIR, f"Protected: Hoard keeps its own data here. {_KEEP}")
    for exe in {sys.executable, os.environ.get("APPIMAGE") or ""} - {""}:
        add(exe, f"Protected: Hoard is running from here. {_KEEP}")
    for p in TEST_PROTECT["trees"]:
        tree(p, f"Protected: test tree. {_KEEP}")
    for p in TEST_PROTECT["items"]:
        add(p, f"Protected: test item. {_KEEP}")
    return {"t": time.time(), "items": items, "trees": trees, "names": names,
            "id_items": _identities(item_paths), "id_trees": _identities(tree_paths),
            "id_exempt": _identities([(p, "") for p in exempt_paths])}


def _identities(pairs):
    out = {}
    for path, why in pairs:
        try:
            st = os.lstat(path)
        except (OSError, ValueError):
            continue
        if st.st_ino:
            out.setdefault((st.st_dev, st.st_ino), (path, why))
    return out


def _rules():
    if time.time() - _RULES["t"] > 60:
        _RULES.update(_build_rules())
    return _RULES


def reset_rules():
    """Rebuild the protection rules on next use (tests, and after the protected lists change)."""
    _RULES["t"] = 0.0


def _tree_key(k, r):
    for t, why, exempt in r["trees"]:
        if (k == t or k.startswith(t + os.sep)) and not any(k.startswith(e + os.sep) for e in exempt):
            return why
    return None


def critical_key(k):
    """Reason the item with this path_key() must never be removed, moved, renamed or replaced, else None
    (path rules only; critical() adds the identity checks)."""
    r = _rules()
    if _is_root_key(k):
        return f"Protected: this is a whole drive. {_KEEP}" if NAME == "windows" else r["items"]["/"][1]
    hit = r["items"].get(k)
    if hit:
        return hit[1]
    why = _tree_key(k, r)
    if why:
        return why
    if NAME == "windows":
        drive, rest = os.path.splitdrive(k)
        if rest.count("\\") == 1 and rest[1:] in r["names"]:       # a protected name at the root of any drive
            return r["names"][rest[1:]]
    pre = k + os.sep
    for ik, (shown, _why) in r["items"].items():                 # a folder that contains a critical item
        if ik.startswith(pre):
            return f"Protected: this folder contains {shown}. {_KEEP}"
    for t, _why, _ex in r["trees"]:
        if t.startswith(pre):
            return f"Protected: this folder contains {t}. {_KEEP}"
    return None


def id_tree_step(st, why):
    """Walking down from the root: the protection reason after entering the folder with lstat `st`."""
    r = _rules()
    k = (st.st_dev, st.st_ino)
    if k in r["id_trees"]:
        return r["id_trees"][k][1]
    if k in r["id_exempt"]:
        return None
    return why


def id_item_why(st):
    """Reason the item with lstat `st` is itself protected by identity (an item or a tree root), else None."""
    r = _rules()
    k = (st.st_dev, st.st_ino)
    hit = r["id_items"].get(k) or r["id_trees"].get(k)
    return hit[1] if hit else None


def _components(p):
    drive, rest = os.path.splitdrive(p)
    cur = drive + os.sep if NAME == "windows" else os.sep
    out = [cur]
    for part in [x for x in rest.split(os.sep) if x]:
        cur = os.path.join(cur, part)
        out.append(cur)
    return out


def _walk_ids(real):
    """lstat of each existing folder from the root down to `real`. Raises OSError when one can't be read."""
    chain = []
    for pre in _components(real):
        try:
            chain.append(os.lstat(pre))
        except (FileNotFoundError, NotADirectoryError):
            break
    return chain


def tree_problem(path):
    """Reason nothing may be written *inside* the folder at path (it's in a protected tree, through any alias), or
    can't be resolved, else None. Folders that are protected only as items (Documents) may be written into."""
    try:
        given = os.path.abspath(strip_long(path))
        why = share_problem(given)
        if why:
            return why
        real = canonical(path, follow=True)
    except (OSError, ValueError):
        return UNSURE
    why = share_problem(real)
    if why:
        return why
    r = _rules()
    for p in (given, real):
        why = _tree_key(path_key(p), r)
        if why:
            return why
    try:
        chain = _walk_ids(real)
    except OSError:
        return UNSURE
    for st in chain:
        why = id_tree_step(st, why)
    return why


def is_volume_root(path):
    """Is path itself the root of its volume (Windows: GetVolumePathNameW gives the path back; POSIX: a mount)?"""
    try:
        if NAME == "windows":
            root = _os.volume_root(path)
            return bool(root) and path_key(root) == path_key(path)
        return os.path.ismount(path)
    except OSError:
        return False


_FAT = {"fat", "fat12", "fat16", "fat32", "exfat", "vfat", "msdos"}


def stable_ids(path):
    """Do file ids on this volume survive a rename? Not on FAT/exFAT, where they come from the position of the
    directory entry: there an item renamed is recognised by size, modified and created times instead."""
    try:
        if NAME == "windows":
            return _os.fs_name(path).lower() not in _FAT
        if NAME == "linux":
            real = os.path.realpath(path)
            best = ("", "")
            for _src, mnt, fs in _os._mounts():
                if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best[0]):
                    best = (mnt, fs)
            return best[1].lower() not in _FAT
    except OSError:
        pass
    return True


def replace_file(replaced, replacement, backup):
    return _os.replace_file(replaced, replacement, backup) if NAME == "windows" else (False, 50)


def _ids_expected(folder):
    """Does this folder's disk give items real file ids? (FAT/exFAT roots and some shares report 0: there, only
    the path rules can apply.)"""
    try:
        return bool(os.lstat(folder).st_ino)
    except OSError:
        return True


def child_problem(parent_real, name, st=None):
    """Reason the item `name` inside an already verified folder may not be changed (path rules, and identity when
    its lstat is known), else None. Cheap: no walk, the folder above was checked when it was opened."""
    why = critical_key(path_key(os.path.join(parent_real, name)))
    if why is None and st is not None:
        why = id_item_why(st)
        if why is None and not st.st_ino and _ids_expected(parent_real):
            why = UNSURE                                # an id-less item on a disk that has ids: suspicious
    return why


def critical(path):
    """Reason `path` is system-critical (it, or something inside it, can never be removed, moved, renamed or
    replaced; or nothing may be written there), else None. Fails closed when the path can't be resolved."""
    try:
        given = os.path.abspath(strip_long(path))
        why = share_problem(given)
        if why:
            return why
        real = canonical(path)
    except (OSError, ValueError):
        return UNSURE
    why = share_problem(real)
    if why:
        return why
    for p in (given, real):
        why = critical_key(path_key(p))
        if why:
            return why
    try:
        chain = _walk_ids(real)
    except OSError:
        return UNSURE
    for st in chain:
        why = id_tree_step(st, why)
    if why:
        return why
    if len(chain) == len(_components(real)):            # the item exists: is it, by identity, a protected item?
        why = id_item_why(chain[-1])
        if why:
            return why
        if not chain[-1].st_ino and len(chain) > 1 and chain[-2].st_ino:
            return UNSURE                               # id-less item on a disk that has ids (else: path rules only)
        if NAME != "windows":
            try:
                if os.path.ismount(real):
                    return f"Protected: a disk is mounted here. {_KEEP}"
            except OSError:
                return UNSURE
    return None


# ---------------------------------------------------------------- renaming without ever replacing
_LIBC = {}


def _libc():
    if "c" not in _LIBC:
        import ctypes
        try:
            _LIBC["c"] = ctypes.CDLL(None, use_errno=True)
        except OSError:
            _LIBC["c"] = None
    return _LIBC["c"]


def rename_noreplace(src, dst, src_dir_fd=None, dst_dir_fd=None):
    """Rename src to dst, failing with FileExistsError if dst exists, atomically.
    Windows: os.rename never replaces. Linux: renameat2(RENAME_NOREPLACE). macOS: renameatx_np(RENAME_EXCL).
    Otherwise a hard link + unlink for files; folders are refused rather than risk replacing something."""
    if NAME == "windows":
        os.rename(src, dst)
        return
    import ctypes
    import errno as _errno
    libc = _libc()
    AT_FDCWD = -100 if NAME == "linux" else -2
    sfd = AT_FDCWD if src_dir_fd is None else src_dir_fd
    dfd = AT_FDCWD if dst_dir_fd is None else dst_dir_fd
    fn, flag = (("renameat2", 1) if NAME == "linux" else ("renameatx_np", 0x4))
    f = getattr(libc, fn, None) if libc is not None else None
    if f is not None:
        f.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        f.restype = ctypes.c_int
        if f(sfd, os.fsencode(src), dfd, os.fsencode(dst), flag) == 0:
            return
        e = ctypes.get_errno()
        if e not in (_errno.ENOSYS, _errno.EINVAL, getattr(_errno, "ENOTSUP", 95), getattr(_errno, "EOPNOTSUPP", 95)):
            raise OSError(e, os.strerror(e), dst)
    st = os.stat(src, dir_fd=src_dir_fd, follow_symlinks=False)
    import stat as _stat
    if _stat.S_ISDIR(st.st_mode):
        raise OSError(_errno.ENOTSUP, "This disk can't rename folders without risking replacing something, "
                                      "so Hoard won't.", dst)
    os.link(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, follow_symlinks=False)
    os.unlink(src, dir_fd=src_dir_fd)


# ---------------------------------------------------------------- trash with a receipt
def trash_receipt(path):
    """Move to the Recycle Bin / Trash and find where it went, so it can be put back.
    Returns (ok, error, where); `where` is None when the OS hid it (it's still in the bin, just not undoable here)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False, "It's no longer there.", None
    since = time.time() - 2
    ok, err = trash(path)
    if not ok:
        return False, err, None
    try:
        where = _find_trashed(path, st, since)
    except OSError:
        where = None
    return True, None, where


def _trash_dirs(path):
    """Folders the OS may have moved `path` into (top level only)."""
    if NAME == "windows":
        drive = os.path.splitdrive(os.path.abspath(path))[0]
        rb = os.path.join(drive + "\\", "$Recycle.Bin")
        try:
            return [e.path for e in os.scandir(rb) if e.is_dir(follow_symlinks=False)]
        except OSError:
            return []
    uid = os.getuid()
    home = os.path.expanduser("~")
    if NAME == "macos":
        dirs = [os.path.join(home, ".Trash")]
    else:
        dirs = [os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"), "Trash", "files")]
    top = os.path.dirname(os.path.abspath(path))
    while not os.path.ismount(top):
        top = os.path.dirname(top)
    dirs += [os.path.join(top, ".Trashes", str(uid)), os.path.join(top, f".Trash-{uid}", "files"),
             os.path.join(top, ".Trash", str(uid), "files")]
    return dirs


def _find_trashed(path, st, since):
    """The trashed item is the same file (a rename keeps its file id): find it by (device, id)."""
    for d in _trash_dirs(path):
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            try:
                if NAME == "windows":
                    # $Ixxxx holds the metadata, $Rxxxx the item; only look at ones written just now
                    if not e.name.startswith("$I") or e.stat(follow_symlinks=False).st_mtime < since:
                        continue
                    cand = os.path.join(d, "$R" + e.name[2:])
                    s = os.lstat(cand)
                else:
                    if e.inode() != st.st_ino:
                        continue
                    cand, s = e.path, e.stat(follow_symlinks=False)
                if (s.st_dev, s.st_ino) == (st.st_dev, st.st_ino):
                    return cand
            except OSError:
                continue
    return None


def trash_meta(where):
    """The bin's bookkeeping file for a trashed item (Windows $I…, Linux .trashinfo), or None."""
    if NAME == "windows":
        return os.path.join(os.path.dirname(where), "$I" + os.path.basename(where)[2:])
    if NAME == "linux":
        return os.path.join(os.path.dirname(os.path.dirname(where)), "info", os.path.basename(where) + ".trashinfo")
    return None


def open_ui(url):
    """Show the UI. Returns True when a native window was shown and has been closed (the app should exit),
    False when the UI was handed to a browser (the app keeps serving until the page says goodbye)."""
    if not os.environ.get("HOARD_BROWSER"):
        try:
            import webview  # pywebview: native window (WKWebView on macOS, WebView2 on Windows, GTK/Qt on Linux)
        except Exception:  # noqa: BLE001 - missing, or present without its GUI backend
            webview = None
        if webview is not None:
            try:
                webview.create_window("Hoard", url, width=1440, height=920, min_size=(860, 560),
                                      background_color="#1a1a19")
                webview.start()
                return True
            except Exception:  # noqa: BLE001 - no usable GUI backend; fall back to a browser
                pass
    for b in _os.app_browsers():
        try:
            subprocess.Popen([b, f"--app={url}", "--window-size=1440,920"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=host_env())
            return False
        except OSError:
            continue
    if NAME == "linux" and shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=host_env())
        return False
    import webbrowser
    webbrowser.open(url)
    return False


def which_all(names):
    return [p for p in (shutil.which(n) for n in names) if p]


def host_env():
    """Environment for launching the user's own programs. A PyInstaller build on Linux points
    LD_LIBRARY_PATH at its bundled libraries, which breaks host tools like xdg-open, gio and browsers."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False) and NAME == "linux":
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env
