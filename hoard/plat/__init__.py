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
