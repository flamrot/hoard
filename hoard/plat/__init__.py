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
alloc_size = getattr(_os, "alloc_size", None)   # (path, logical) -> bytes really allocated (Windows sparse/compressed)


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
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
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
    """(device, file id, is folder) of the item at path itself, not following links."""
    import stat as _stat
    st = os.lstat(path)
    return [int(st.st_dev), int(st.st_ino), _stat.S_ISDIR(st.st_mode)]


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
