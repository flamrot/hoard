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


# OneDrive / Google Drive for desktop / Dropbox "online-only" placeholders: the bytes live in the cloud
# RECALL_ON_DATA_ACCESS | OFFLINE. Not RECALL_ON_OPEN (0x40000): it shares its value with
# FILE_ATTRIBUTE_EA, which ordinary local files can carry; cloud *files* always have RECALL_ON_DATA_ACCESS.
CLOUD_ATTRS = 0x400000 | 0x1000


def size_of(st):
    return 0 if st.st_file_attributes & CLOUD_ATTRS else st.st_size


def cloud_size(st):
    return st.st_size if st.st_file_attributes & CLOUD_ATTRS else 0


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
        out.append({"id": L, "system": root.upper().startswith((os.environ.get("SystemDrive") or "C:").upper()),
                    "root": root, "name": f"{L}:", "label": label.value or
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
    if not parts:
        return "That's the whole drive." if len(root.rstrip("\\/")) <= 2 else "That's the whole folder Hoard is mapping."
    sysdrive = (os.environ.get("SystemDrive") or "C:").upper()
    # judge by the position on the *drive*, not inside the volume: an added folder on C: is not C:'s top level
    drive, rest = os.path.splitdrive(os.path.normpath(full))
    dparts = [p for p in rest.split("\\") if p]
    if drive.upper() == sysdrive and len(dparts) < 2:
        return "Top-level folders and files of the Windows drive can't be removed from here."
    if dparts and dparts[0].lower() in ("$recycle.bin", "system volume information", "recovery", "boot",
                                        "$windows.~bt", "$windows.~ws", "$winreagent", "config.msi"):
        return "That's a Windows system folder; it can't be removed from here."
    full = os.path.normpath(full).lower()
    win = (os.environ.get("SystemRoot") or "C:\\Windows").lower()
    if full == win or full.startswith(win + "\\"):
        return "Windows system files can't be removed from here."
    home = os.path.expanduser("~").lower()
    guarded = {home, home + "\\appdata", home + "\\appdata\\local", home + "\\appdata\\roaming",
               home + "\\documents", home + "\\desktop", home + "\\downloads", home + "\\onedrive"}
    if full in guarded:
        return "That's a core folder of your user profile. Remove what's inside it instead."
    return None


TASK = "Hoard daily scan"
NO_WINDOW = 0x08000000   # CREATE_NO_WINDOW: don't flash a console from the windowed app
SCHEDULE_NOTE = "Runs in the background through Windows Task Scheduler, even when Hoard is closed. If the PC is asleep or off at that time, it runs as soon as it's back on."


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW, **kw)


def _x(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


class _SEI(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def run_elevated(exe, args, timeout_ms=120000):
    """Run a program with admin rights (Windows asks first) and wait. Returns its exit code, or None if declined."""
    info = _SEI()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x400            # NOCLOSEPROCESS | FLAG_NO_UI
    info.lpVerb, info.lpFile, info.lpParameters, info.nShow = "runas", exe, subprocess.list2cmdline(args), 0
    sh = ctypes.windll.shell32
    sh.ShellExecuteExW.argtypes = [ctypes.POINTER(_SEI)]
    if not sh.ShellExecuteExW(ctypes.byref(info)):
        return None if ctypes.GetLastError() == 1223 else -1
    k32 = ctypes.windll.kernel32
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.WaitForSingleObject(info.hProcess, timeout_ms)
    code = wintypes.DWORD()
    k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    k32.CloseHandle(info.hProcess)
    return code.value


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             wintypes.DWORD, wintypes.HANDLE]
_k32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_INVALID = wintypes.HANDLE(-1).value


def pin_dir(path, expect=None):
    """Open the folder itself (never what a junction points to) and keep the handle: without delete sharing,
    nobody can rename, delete or swap it for a junction until unpin_dir(). Checks it's a real folder and,
    with `expect`, that it's the same one we listed (by file id)."""
    FILE_LIST_DIRECTORY, FILE_READ_ATTRIBUTES = 0x1, 0x80
    SHARE_READ_WRITE = 0x1 | 0x2                      # deliberately no FILE_SHARE_DELETE
    OPEN_EXISTING = 3
    BACKUP_SEMANTICS, OPEN_REPARSE_POINT = 0x02000000, 0x00200000
    h = _k32.CreateFileW(path, FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES, SHARE_READ_WRITE, None, OPEN_EXISTING,
                         BACKUP_SEMANTICS | OPEN_REPARSE_POINT, None)
    if h is None or h == _INVALID:
        return None
    info = _BY_HANDLE_FILE_INFORMATION()
    ok = _k32.GetFileInformationByHandle(h, ctypes.byref(info))
    attrs = info.dwFileAttributes
    idx = (info.nFileIndexHigh << 32) | info.nFileIndexLow
    if not ok or not attrs & 0x10 or attrs & 0x400 or (expect and idx != expect & 0xFFFFFFFFFFFFFFFF):
        _k32.CloseHandle(h)
        return None
    return h


def unpin_dir(h):
    _k32.CloseHandle(h)


def schedule_install(hour, minute, argv, elevated=False):
    import tempfile
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Hoard scans your disks once a day so it can show what grew.</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{hour:02d}:{minute:02d}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>{"HighestAvailable" if elevated else "LeastPrivilege"}</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT3H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{_x(argv[0])}</Command><Arguments>{_x(subprocess.list2cmdline(argv[1:]))}</Arguments></Exec>
  </Actions>
</Task>
"""
    path = os.path.join(tempfile.gettempdir(), f"hoard-task-{os.getpid()}.xml")   # readable when elevated
    try:
        with open(path, "w", encoding="utf-16") as f:
            f.write(xml)
        args = ["/Create", "/TN", TASK, "/XML", path, "/F"]
        if elevated:
            # a task that runs with admin rights must be registered with admin rights: one prompt, now
            code = run_elevated("schtasks.exe", args)
            if code is None:
                return False, "The permission prompt was declined, so the daily scan wasn't set to use fast scan."
            return (True, None) if code == 0 else (False, f"Task Scheduler refused the task (code {code}).")
        r = _run(["schtasks"] + args)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return (True, None) if r.returncode == 0 else (False, (r.stderr or r.stdout).strip() or "Task Scheduler refused the task.")


def schedule_remove():
    if not schedule_status()["installed"]:
        return True, None
    r = _run(["schtasks", "/Delete", "/TN", TASK, "/F"])
    if r.returncode == 0:
        return True, None
    if "denied" in (r.stderr + r.stdout).lower():      # an admin-level task needs admin rights to remove
        code = run_elevated("schtasks.exe", ["/Delete", "/TN", TASK, "/F"])
        if code == 0:
            return True, None
    return False, (r.stderr or r.stdout).strip()


def schedule_status():
    r = _run(["schtasks", "/Query", "/TN", TASK, "/XML"])
    return {"installed": r.returncode == 0, "elevated": "HighestAvailable" in r.stdout,
            "how": f"Task Scheduler → “{TASK}”"}


def notify(title, body):
    """Windows toast through PowerShell (no extra dependencies). Text is passed via env vars, never in the script."""
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$x = $t.GetElementsByTagName('text');"
        "$x.Item(0).AppendChild($t.CreateTextNode($env:HOARD_TITLE)) > $null;"
        "$x.Item(1).AppendChild($t.CreateTextNode($env:HOARD_BODY)) > $null;"
        "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    )
    try:
        _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
             env=dict(os.environ, HOARD_TITLE=title, HOARD_BODY=body), timeout=30)
    except Exception:  # noqa: BLE001
        pass


HIDDEN_NOTE = ("Space Windows uses but the scan can't open: system restore points, the file table, "
               "protected system folders and files in use. Windows' Disk Cleanup can reclaim some of it.")


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]


def bin_size():
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) == 0:
        return info.i64Size
    return None


def empty_bin():
    SHERB_NOCONFIRMATION, SHERB_NOSOUND = 0x1, 0x4
    r = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, SHERB_NOCONFIRMATION | SHERB_NOSOUND)
    # S_OK, or E_UNEXPECTED (0x8000FFFF) when it was already empty
    return (True, None) if r in (0, -2147418113) else (False, f"Windows couldn't empty the Recycle Bin (code {r & 0xFFFFFFFF:#x}).")


def cleanup_specs():
    L = os.environ.get("LOCALAPPDATA", "")
    H = os.path.expanduser("~")
    T = os.environ.get("TEMP") or os.path.join(L, "Temp")
    sysdrive = os.environ.get("SystemDrive", "C:") + "\\"
    chromium = []
    for base in ("Google/Chrome", "Microsoft/Edge", "BraveSoftware/Brave-Browser", "Vivaldi"):
        for sub in ("Cache", "Code Cache", "GPUCache", "Service Worker/CacheStorage"):
            chromium.append(f"{L}/{base}/User Data/*/{sub}")
    return [
        {"id": "bin", "group": "Recycle Bin", "title": "Recycle Bin",
         "desc": "Things you've already deleted. Emptying it frees the space for good.", "action": "empty_bin"},
        {"id": "temp", "group": "System", "title": "Temporary files",
         "desc": "Leftovers in your temp folder older than a day. Files in use are skipped.",
         "action": "clear", "paths": [T], "olderDays": 1},
        {"id": "crash", "group": "System", "title": "Crash dumps and error reports",
         "desc": "Memory dumps and reports saved when apps crashed.", "action": "clear",
         "paths": [f"{L}/CrashDumps", f"{L}/Microsoft/Windows/WER"]},
        {"id": "shader", "group": "System", "title": "Graphics shader caches",
         "desc": "AMD, NVIDIA and DirectX shader caches. Games rebuild them, so the first launch may stutter a little.",
         "action": "clear", "paths": [f"{L}/AMD/DxCache", f"{L}/AMD/DxcCache", f"{L}/AMD/GLCache", f"{L}/AMD/VkCache",
                                      f"{L}/NVIDIA/DXCache", f"{L}/NVIDIA/GLCache", f"{L}/D3DSCache"]},
        {"id": "browsers", "group": "Browsers", "title": "Browser caches",
         "desc": "Cached pages and images for Chrome, Edge, Brave, Vivaldi and Firefox. You stay signed in; close the browser first for the best result.",
         "action": "clear", "paths": chromium + [f"{L}/Mozilla/Firefox/Profiles/*/cache2"]},
        {"id": "npm", "group": "Developer", "title": "npm cache", "desc": "Downloaded packages; npm fetches them again when needed.",
         "action": "clear", "paths": [f"{L}/npm-cache/_cacache"]},
        {"id": "pip", "group": "Developer", "title": "pip and uv caches", "desc": "Downloaded Python packages.",
         "action": "clear", "paths": [f"{L}/pip/cache", f"{L}/uv/cache"]},
        {"id": "yarn", "group": "Developer", "title": "Yarn, Go, Gradle and Cargo caches",
         "desc": "Build and package caches; rebuilt on the next build.", "action": "clear",
         "paths": [f"{L}/Yarn/Cache", f"{L}/go-build", f"{H}/.gradle/caches", f"{H}/.cargo/registry/cache"]},
        {"id": "nuget", "group": "Developer", "title": "NuGet packages", "desc": "Downloaded .NET packages; restored on the next build.",
         "action": "clear", "paths": [f"{H}/.nuget/packages"], "review": True},
        {"id": "hf", "group": "Developer", "title": "Downloaded AI models (Hugging Face)",
         "desc": "Models cached by Python AI tools. Pick the ones you no longer use.", "action": "stage_children",
         "paths": [f"{H}/.cache/huggingface/hub"]},
        {"id": "node_modules", "group": "Your files", "title": "node_modules in inactive projects",
         "desc": "Projects you haven't changed for 90 days. Run npm install to get them back.", "action": "stage_files",
         "finder": "node_modules"},
        {"id": "downloads", "group": "Your files", "title": "Old installers and archives in Downloads",
         "desc": "Setup files, disk images and archives older than 30 days.", "action": "stage_files", "finder": "downloads"},
        {"id": "winclean", "group": "System", "title": "Windows Update and system files",
         "desc": "Old Windows Update downloads, Delivery Optimization files and upgrade leftovers. Windows' own Disk Cleanup removes these.",
         "action": "tool", "tool": ["cleanmgr.exe", "/d", sysdrive[0]],
         "paths": [os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "SoftwareDistribution", "Download")]},
        {"id": "livekernel", "group": "System", "title": "Windows kernel crash reports",
         "desc": "Memory dumps Windows saves after driver or system hangs (often several GB). Only a fast scan can measure "
                 "this protected folder; remove them from an admin terminal.",
         "action": "command", "admin": True,
         "command": f'del /q "{os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "LiveKernelReports")}\\*.dmp"',
         "paths": [os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "LiveKernelReports")]},
        {"id": "hiber", "group": "System", "title": "Hibernation file",
         "desc": "Reserved for hibernate and Fast Startup. If you never hibernate, you can turn it off in an admin terminal.",
         "action": "command", "command": "powercfg /h off", "admin": True, "file": sysdrive + "hiberfil.sys"},
    ]


def installed_apps():
    import hashlib
    import winreg
    roots = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")]
    out, seen = [], set()
    for hive, path in roots:
        try:
            k = winreg.OpenKey(hive, path)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(k)[0]):
            try:
                sub = winreg.EnumKey(k, i)
                sk = winreg.OpenKey(k, sub)
            except OSError:
                continue

            def v(n, sk=sk):
                try:
                    return winreg.QueryValueEx(sk, n)[0]
                except OSError:
                    return None
            name, un = v("DisplayName"), v("UninstallString")
            if not name or not un or v("SystemComponent") == 1 or v("ParentKeyName") \
                    or v("ReleaseType") in ("Update", "Hotfix", "Security Update"):
                continue
            key = (name.strip().lower(), v("DisplayVersion"))
            if key in seen:
                continue
            seen.add(key)
            loc = (v("InstallLocation") or "").strip().strip('"')
            if not loc and v("DisplayIcon"):
                icon = str(v("DisplayIcon")).split(",")[0].strip().strip('"')
                loc = os.path.dirname(icon) if icon.lower().endswith(".exe") else ""
            out.append({"id": hashlib.sha1(f"{hive}{path}{sub}".encode()).hexdigest()[:12],
                        "name": name.strip(), "version": v("DisplayVersion") or "", "publisher": v("Publisher") or "",
                        "location": loc if loc and os.path.isdir(loc) else "",
                        "estimate": int(v("EstimatedSize") or 0) * 1024, "date": str(v("InstallDate") or ""),
                        "uninstall": "run", "cmd": str(un)})
    return out


def run_uninstall(app):
    cmd = app["cmd"]
    if "msiexec" in cmd.lower():
        cmd = cmd.replace("/I{", "/X{").replace("/i{", "/X{")     # "modify" entries -> uninstall
    try:
        subprocess.Popen(cmd, shell=True)
        return True, None
    except OSError as ex:
        return False, str(ex)


_ATTR_NAMES = [(0x1, "Read-only"), (0x2, "Hidden"), (0x4, "System"), (0x20, "Archive"), (0x800, "Compressed"),
               (0x4000, "Encrypted"), (0x200, "Sparse"), (0x400, "Link or junction"), (CLOUD_ATTRS, "Online-only (in the cloud)")]
_PERM = {"F": "Full control", "M": "Modify", "RX": "Read & run", "R": "Read", "W": "Write", "D": "Delete",
         "N": "No access", "GR": "Read", "GW": "Write", "GA": "Full control", "GE": "Run", "RD": "Read",
         "WD": "Write", "AD": "Add files", "X": "Run", "DE": "Delete", "DC": "Delete contents", "RA": "Read",
         "REA": "Read", "WA": "Write", "WEA": "Write", "WDAC": "Change permissions", "WO": "Take ownership",
         "RC": "Read permissions", "MA": "Full control"}


def _owner_sid(path):
    adv = ctypes.windll.advapi32
    adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD] + [ctypes.POINTER(ctypes.c_void_p)] * 5
    adv.GetNamedSecurityInfoW.restype = wintypes.DWORD
    adv.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
                                      wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    owner, sd = ctypes.c_void_p(), ctypes.c_void_p()
    if adv.GetNamedSecurityInfoW(path, 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(sd)) != 0:
        return None
    try:
        name, dom = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        n, d, use = wintypes.DWORD(256), wintypes.DWORD(256), wintypes.DWORD()
        if adv.LookupAccountSidW(None, owner, name, ctypes.byref(n), dom, ctypes.byref(d), ctypes.byref(use)):
            return f"{dom.value}\\{name.value}" if dom.value else name.value
        sid = wintypes.LPWSTR()
        if adv.ConvertSidToStringSidW(owner, ctypes.byref(sid)):
            s = sid.value
            ctypes.windll.kernel32.LocalFree(sid)
            return s
        return None
    finally:
        ctypes.windll.kernel32.LocalFree(sd)


def owner_of(path):
    return _owner_sid(path) or "Unknown"


def file_info(path):
    a = ctypes.windll.kernel32.GetFileAttributesW(path)
    attrs = [n for bit, n in _ATTR_NAMES if a != 0xFFFFFFFF and a & bit]
    perms = []
    try:
        r = _run(["icacls", path], timeout=10)
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        if lines and lines[0].lower().startswith(path.lower()):
            lines[0] = lines[0][len(path):].strip()
        for ln in lines:
            if ln.lower().startswith("successfully") or ":" not in ln:
                continue
            who, rights = ln.rsplit(":", 1)
            groups = [c for c in rights.replace(")", "").split("(") if c]
            codes = [x for g in groups for x in g.split(",")]
            words = [_PERM[c] for c in codes if c in _PERM]
            can = ", ".join(dict.fromkeys(words)) or rights
            if "IO" in codes:
                can += " (for what's inside)"
            perms.append({"who": who.strip(), "can": can, "inherited": "I" in codes, "deny": "DENY" in rights.upper()})
    except Exception:  # noqa: BLE001
        pass
    return {"owner": _owner_sid(path) or "Unknown", "attrs": attrs, "perms": perms}


_CM_KEYS = [(r"Software\Classes\Directory\shell\Hoard", '"%1"'),
            (r"Software\Classes\Drive\shell\Hoard", '"%1"'),
            (r"Software\Classes\Directory\Background\shell\Hoard", '"%V"')]


def context_menu_install(argv):
    """'Open in Hoard' when right-clicking a folder, a drive, or the empty space inside a folder (this account only)."""
    import winreg
    try:
        for key, arg in _CM_KEYS:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "Open in Hoard")
                winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, argv[0])
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key + r"\command") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, subprocess.list2cmdline(argv + ["--open"]) + " " + arg)
        return True, None
    except OSError as ex:
        return False, str(ex)


def context_menu_remove():
    import winreg
    for key, _ in _CM_KEYS:
        for k in (key + r"\command", key):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)
            except OSError:
                pass
    return True, None


def context_menu_status():
    import winreg
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, _CM_KEYS[0][0] + r"\command"))
        return True
    except OSError:
        return False


def app_browsers():
    cands = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft\\Edge\\Application\\msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft\\Edge\\Application\\msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Google\\Chrome\\Application\\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google\\Chrome\\Application\\chrome.exe"),
    ]
    return [c for c in cands if c and os.path.exists(c)]
