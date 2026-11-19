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


TASK = "Hoard daily scan"
NO_WINDOW = 0x08000000   # CREATE_NO_WINDOW: don't flash a console from the windowed app
SCHEDULE_NOTE = "Runs in the background through Windows Task Scheduler, even when Hoard is closed. If the PC is asleep or off at that time, it runs as soon as it's back on."


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, creationflags=NO_WINDOW, **kw)


def _x(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def schedule_install(hour, minute, argv):
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
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
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
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-16") as f:
            f.write(xml)
        r = _run(["schtasks", "/Create", "/TN", TASK, "/XML", path, "/F"])
    finally:
        os.remove(path)
    return (True, None) if r.returncode == 0 else (False, (r.stderr or r.stdout).strip() or "Task Scheduler refused the task.")


def schedule_remove():
    if not schedule_status()["installed"]:
        return True, None
    r = _run(["schtasks", "/Delete", "/TN", TASK, "/F"])
    return (True, None) if r.returncode == 0 else (False, (r.stderr or r.stdout).strip())


def schedule_status():
    r = _run(["schtasks", "/Query", "/TN", TASK])
    return {"installed": r.returncode == 0, "how": f"Task Scheduler → “{TASK}”"}


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


def app_browsers():
    cands = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Microsoft\\Edge\\Application\\msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Microsoft\\Edge\\Application\\msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Google\\Chrome\\Application\\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google\\Chrome\\Application\\chrome.exe"),
    ]
    return [c for c in cands if c and os.path.exists(c)]
