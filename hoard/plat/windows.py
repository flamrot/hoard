import ctypes
import os
import shutil
import string
import subprocess
from ctypes import wintypes

NAME = "windows"
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Hoard")
TRASH_NAME = "Recycle Bin"
REVEAL_NAME = "Explorer"


def size_of(st):
    return st.st_size


def list_volumes():
    k32 = ctypes.windll.kernel32
    out = []
    mask = k32.GetLogicalDrives()
    for i, L in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = f"{L}:\\"
        dtype = k32.GetDriveTypeW(root)
        if dtype not in (2, 3, 4):
            continue
        try:
            u = shutil.disk_usage(root)
        except OSError:
            continue
        label = ctypes.create_unicode_buffer(261)
        k32.GetVolumeInformationW(root, label, 261, None, None, None, None, 0)
        kind = {2: "removable", 3: "fixed", 4: "network"}[dtype]
        out.append({"id": L, "root": root, "name": f"{L}:", "label": label.value or
                    {"fixed": "Local disk", "removable": "Removable", "network": "Network"}[kind],
                    "type": kind, "total": u.total, "used": u.used, "free": u.free})
    return out


def scan_excludes(root):
    return set()   # junctions and mounted folders are skipped by the scanner itself


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def trash(path):
    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_WANTNUKEWARNING = 3, 0x40, 0x10, 0x4000
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = path + "\0"
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_WANTNUKEWARNING
    r = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if r == 0 and not op.fAnyOperationsAborted and not os.path.exists(path):
        return True, None
    return False, "Windows didn't remove it (cancelled, in use, or access denied)."


def reveal(path):
    if os.path.isdir(path):
        os.startfile(path)
    else:
        subprocess.Popen(f'explorer /select,"{path}"')


def protected(root, parts, full):
    if len(parts) < 2:
        return "Top-level folders and files can't be removed from here."
    full = full.lower()
    win = (os.environ.get("SystemRoot") or "C:\\Windows").lower()
    if full == win or full.startswith(win + "\\"):
        return "Windows system files can't be removed from here."
    home = os.path.expanduser("~").lower()
    guarded = {home, home + "\\appdata", home + "\\appdata\\local", home + "\\appdata\\roaming",
               home + "\\documents", home + "\\desktop", home + "\\downloads", home + "\\onedrive"}
    if full in guarded:
        return "That's a core folder of your user profile. Remove what's inside it instead."
    return None


def app_browsers():
    cands = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft\\Edge\\Application\\msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft\\Edge\\Application\\msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Google\\Chrome\\Application\\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google\\Chrome\\Application\\chrome.exe"),
    ]
    return [c for c in cands if c and os.path.exists(c)]
