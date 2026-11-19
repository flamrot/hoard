"""Windows fast scan: run the native hoard-mft helper (reads the NTFS file table) with admin rights."""
import ctypes
import json
import os
import sys
import tempfile
import time
from ctypes import wintypes

import core
import plat


class Declined(Exception):
    """The user said no to the Windows permission prompt."""


def helper_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(getattr(sys, "_MEIPASS", here), "hoard-mft.exe"),
              os.path.join(here, "bin", "hoard-mft.exe"),
              os.path.join(os.path.dirname(here), "native", "target", "release", "hoard-mft.exe")):
        if os.path.isfile(p):
            return p
    return None


def filesystem(root):
    buf = ctypes.create_unicode_buffer(64)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, None, buf, 64)
    return buf.value if ok else ""


def available(v):
    return (plat.NAME == "windows" and v.get("type") == "fixed" and len(v.get("root", "")) == 3
            and helper_path() is not None and filesystem(v["root"]) == "NTFS")


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def _run_elevated(exe, args):
    import subprocess
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x400            # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = subprocess.list2cmdline(args)
    info.nShow = 0                       # SW_HIDE
    shell = ctypes.windll.shell32
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.GetLastError()
        if err == 1223:                  # ERROR_CANCELLED: the prompt was declined
            raise Declined()
        raise OSError(f"couldn't start the fast scanner (Windows error {err})")
    return info.hProcess


def scan(v, st):
    exe = helper_path()
    k32 = ctypes.windll.kernel32
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    # Not tempfile.mkdtemp: its owner-only permissions would lock this (unelevated) process out of
    # files the elevated helper creates, because those are owned by the Administrators group.
    import secrets
    work = os.path.join(core.DATA_DIR, "mft-" + secrets.token_hex(6))
    os.makedirs(work)
    out, prog, extmap = (os.path.join(work, n) for n in ("tree.json", "progress.txt", "ext.tsv"))
    with open(extmap, "w", encoding="utf-8") as f:
        f.writelines(f"{e}\t{c}\n" for e, c in core.EXT.items())
    st.current = "Waiting for permission…"
    proc = _run_elevated(exe, [v["root"][0], out, prog, extmap])
    try:
        while k32.WaitForSingleObject(proc, 200) == 0x102:      # WAIT_TIMEOUT
            if st.cancel:
                k32.TerminateProcess(proc, 1)
                return None
            try:
                with open(prog, encoding="utf-8") as f:
                    phase, done, total, nbytes = (f.readline().rstrip("\n").split("\t") + ["0"] * 4)[:4]
                st.current = f"{phase}…"
                if phase.startswith("Reading") and total.isdigit() and int(total):
                    st.bytes = int(v.get("used", 0) * int(done) / int(total))   # shows progress against the disk
                elif nbytes.isdigit():
                    st.bytes = int(nbytes)
            except (OSError, ValueError):
                pass
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(proc, ctypes.byref(code))
    finally:
        k32.CloseHandle(proc)
    try:
        if code.value != 0:
            msg = ""
            try:
                with open(prog, encoding="utf-8") as f:
                    msg = f.read().split("\t", 1)[-1].strip()
            except OSError:
                pass
            raise RuntimeError("fast scan failed: " + (msg or f"exit code {code.value}"))
        st.current = "Loading…"
        with open(out, encoding="utf-8") as f:
            tree = core._dec(json.load(f))
        st.files, st.bytes, st.cloud = tree.nfiles, tree.size, tree.cloud
        return tree
    finally:
        for p in (out, prog, extmap):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(work)
        except OSError:
            pass
