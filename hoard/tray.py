"""Windows tray icon (Hoard.exe --tray): free space at a glance, low-space alerts, quick open / check.

Plain Win32 through ctypes (no extra libraries): a hidden window receives the icon's mouse messages and
a 10-minute timer that refreshes the tooltip and checks for drives running out of space.
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

import automation
import core
import plat

CLASS = "HoardTrayWnd"
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_TIMER, WM_COMMAND, WM_CLOSE, WM_DESTROY = 0x0113, 0x0111, 0x0010, 0x0002
WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_LBUTTONUP = 0x0203, 0x0205, 0x0202
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
CMD_OPEN, CMD_SCAN, CMD_QUIT = 1, 2, 9
TIMER_ID, EVERY_MS = 1, 10 * 60 * 1000
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Hoard tray"

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", ctypes.c_wchar * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", ctypes.c_wchar * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


u32, sh32, k32 = (ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32) if sys.platform == "win32" else (None,) * 3


def _setup_api():
    u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u32.DefWindowProcW.restype = LRESULT
    u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                    wintypes.HINSTANCE, wintypes.LPVOID]
    u32.CreateWindowExW.restype = wintypes.HWND
    u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.LPVOID]
    u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    u32.CreatePopupMenu.restype = wintypes.HMENU
    u32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID]
    u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    u32.FindWindowW.restype = wintypes.HWND
    u32.LoadIconW.restype = wintypes.HICON
    u32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
    sh32.ExtractIconW.restype = wintypes.HICON
    sh32.ExtractIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT]
    sh32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    k32.GetModuleHandleW.restype = wintypes.HMODULE


def tooltip_text(vols, report, now=None):
    """'Hoard · C: 167 GB free (+1.4 GB today) · D: 253 GB free', trimmed to the 127 characters Windows allows."""
    import shutil
    now = now or time.time()
    parts = []
    for v in vols:
        try:
            free = shutil.disk_usage(v["root"]).free
        except OSError:
            continue
        grew = [e for e in report if e.get("vol") == v["id"] and now - e.get("time", 0) < 86400 and "delta" in e]
        g = f" ({'+' if grew[-1]['delta'] >= 0 else '−'}{core.human(abs(grew[-1]['delta']))} today)" if grew and abs(grew[-1]["delta"]) >= 50 << 20 else ""
        parts.append(f"{v['name']} {core.human(free)} free{g}")
    return ("Hoard · " + " · ".join(parts))[:127]


class Tray:
    def __init__(self):
        _setup_api()
        self.proc = WNDPROC(self.wndproc)        # keep a reference: Windows calls back into it
        hinst = k32.GetModuleHandleW(None)
        wc = WNDCLASSW(lpfnWndProc=self.proc, hInstance=hinst, lpszClassName=CLASS)
        u32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = u32.CreateWindowExW(0, CLASS, "Hoard", 0, 0, 0, 0, 0, None, None, hinst, None)
        icon = sh32.ExtractIconW(hinst, sys.executable, 0) if getattr(sys, "frozen", False) else None
        self.icon = icon or u32.LoadIconW(None, ctypes.c_void_p(32512))      # IDI_APPLICATION
        self.nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                                   uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP, uCallbackMessage=WM_TRAY, hIcon=self.icon)
        self.nid.szTip = "Hoard"
        self.added = bool(sh32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid)))
        u32.SetTimer(self.hwnd, TIMER_ID, EVERY_MS, None)
        self.refresh()

    def balloon(self, title, body):
        self.nid.uFlags = NIF_INFO
        self.nid.szInfoTitle, self.nid.szInfo, self.nid.dwInfoFlags = title[:63], body[:255], 0x2   # NIIF_WARNING
        sh32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def refresh(self):
        try:
            vols = [v for v in plat.list_volumes() if v["type"] in ("fixed", "removable")]
            self.nid.uFlags = NIF_TIP
            self.nid.szTip = tooltip_text(vols, core.read_report())
            sh32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))
            automation.check_low_space(vols, core.load_settings(), notify=self.balloon)
        except Exception as ex:  # noqa: BLE001
            core.log_error("tray", str(ex))

    def menu(self):
        m = u32.CreatePopupMenu()
        u32.AppendMenuW(m, 0, CMD_OPEN, "Open Hoard")
        u32.AppendMenuW(m, 0, CMD_SCAN, "Check my drives now")
        u32.AppendMenuW(m, 0x800, 0, None)          # separator
        u32.AppendMenuW(m, 0, CMD_QUIT, "Hide this icon")
        pt = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        u32.SetForegroundWindow(self.hwnd)          # so the menu closes when you click elsewhere
        u32.TrackPopupMenu(m, 0, pt.x, pt.y, 0, self.hwnd, None)
        u32.DestroyMenu(m)

    def wndproc(self, hwnd, msg, wp, lp):
        if msg == WM_TRAY:
            if lp in (WM_LBUTTONDBLCLK,):
                self.open()
            elif lp in (WM_RBUTTONUP, WM_LBUTTONUP):
                self.menu()
            return 0
        if msg == WM_COMMAND:
            cmd = wp & 0xFFFF
            if cmd == CMD_OPEN:
                self.open()
            elif cmd == CMD_SCAN:
                subprocess.Popen(plat.self_command() + ["--scan"], creationflags=0x08000000)
                self.balloon("Checking your drives", "Hoard is scanning in the background. Open it to see the results.")
            elif cmd == CMD_QUIT:
                u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            return 0
        if msg == WM_TIMER:
            self.refresh()
            return 0
        if msg == WM_CLOSE:
            sh32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            u32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            u32.PostQuitMessage(0)
            return 0
        return u32.DefWindowProcW(hwnd, msg, wp, lp)

    def open(self):
        subprocess.Popen(plat.self_command())

    def loop(self):
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))


def run():
    if sys.platform != "win32":
        print("The tray icon is Windows-only.")
        return 1
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW(None, False, "Local\\HoardTray")
    if k32.GetLastError() == 183:                   # ERROR_ALREADY_EXISTS: one tray icon is enough
        return 0
    t = Tray()
    if "--tray-test" in sys.argv:                   # add the icon, refresh once, remove it (used by tests)
        ok = t.added
        u32.PostMessageW(t.hwnd, WM_CLOSE, 0, 0)
        t.loop()
        return 0 if ok else 2
    t.loop()
    return 0


def set_autostart(on):
    """Start the tray icon at sign-in (this account only), and start or stop it now."""
    import winreg
    cmd = subprocess.list2cmdline(plat.self_command() + ["--tray"])
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass
    _setup_api()
    hwnd = u32.FindWindowW(CLASS, None)
    if on and not hwnd:
        subprocess.Popen(plat.self_command() + ["--tray"], creationflags=0x08000000)
    if not on and hwnd:
        u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return True, None
