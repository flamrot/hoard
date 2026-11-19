"""Update checks (GitHub releases) and per-user install / uninstall on Windows.

Install puts Hoard.exe in %LOCALAPPDATA%\\Programs\\Hoard, adds a Start menu shortcut and an entry in
Settings → Apps (uninstall), all for this account only, with no admin rights needed.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import core
import plat

CACHE = os.path.join(core.DATA_DIR, "update.json")
APP_ID = "Hoard"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Hoard"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _ver(s):
    parts = []
    for p in (s or "0").lstrip("vV").split("-")[0].split("."):
        parts.append(int(p) if p.isdigit() else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def cached():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _pick_asset(assets):
    names = [(a.get("name", ""), a.get("browser_download_url")) for a in assets]
    if plat.NAME == "windows":
        want = [n for n in names if n[0].lower().endswith(".exe")]
    elif plat.NAME == "macos":
        import platform
        arch = "arm64" if platform.machine() == "arm64" else "x86_64"
        want = [n for n in names if n[0].endswith(".dmg") and arch in n[0]] or [n for n in names if n[0].endswith(".dmg")]
    else:
        want = [n for n in names if n[0].endswith(".AppImage")] or [n for n in names if n[0].endswith(".deb")]
    return want[0] if want else (None, None)


def check(force=False):
    settings = core.load_settings()
    c = cached()
    if c and not force and time.time() - c.get("checked", 0) < 86400:
        return c
    url = settings.get("updateUrl") or ""
    out = {"checked": time.time(), "current": core.VERSION}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Hoard-update-check", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            rel = json.load(r)
        name, asset = _pick_asset(rel.get("assets", []))
        out.update(latest=rel.get("tag_name", "").lstrip("v"), page=rel.get("html_url"), notes=(rel.get("body") or "")[:2000],
                   asset=asset, assetName=name)
        out["newer"] = _ver(out["latest"]) > _ver(core.VERSION)
    except urllib.error.HTTPError as ex:
        out["error"] = ("No releases are visible. If the GitHub repository is private, update checks can't see it; "
                        "make the releases public or update by hand.") if ex.code == 404 else f"GitHub said {ex.code}."
    except Exception as ex:  # noqa: BLE001
        out["error"] = f"Couldn't reach GitHub ({ex})."
    try:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(out, f)
    except OSError:
        pass
    return out


def apply():
    """Windows exe: download the new exe next to this one, swap them, and restart. Elsewhere: open the download."""
    c = cached() or check(force=True)
    if not c.get("newer"):
        return {"error": "You're on the newest version."}
    if plat.NAME != "windows" or not getattr(sys, "frozen", False) or not c.get("asset"):
        import webbrowser
        webbrowser.open(c.get("asset") or c.get("page"))
        return {"ok": True, "opened": True}
    exe = sys.executable
    new = exe + ".new"
    try:
        req = urllib.request.Request(c["asset"], headers={"User-Agent": "Hoard-update"})
        with urllib.request.urlopen(req, timeout=120) as r, open(new, "wb") as f:
            shutil.copyfileobj(r, f)
        if os.path.getsize(new) < 1 << 20:
            raise OSError("the download looks incomplete")
        old = exe + ".old"
        if os.path.exists(old):
            os.remove(old)
        os.replace(exe, old)          # Windows lets a running exe be renamed, not overwritten
        os.replace(new, exe)
    except OSError as ex:
        try:
            os.remove(new)
        except OSError:
            pass
        return {"error": f"The update couldn't be installed: {ex}"}
    subprocess.Popen([exe], creationflags=0x00000008)          # DETACHED_PROCESS: start the new version
    return {"ok": True, "restarting": True}


def cleanup_old():
    if getattr(sys, "frozen", False):
        old = sys.executable + ".old"
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass


# ---------------------------------------------------------------- Windows per-user install
def install_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Hoard")


def is_installed():
    return plat.NAME == "windows" and os.path.isfile(os.path.join(install_dir(), "Hoard.exe"))


def can_install():
    return plat.NAME == "windows" and bool(getattr(sys, "frozen", False))


def _shortcut(lnk, target, desc):
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:HOARD_LNK);"
          "$s.TargetPath=$env:HOARD_TARGET;$s.WorkingDirectory=(Split-Path $env:HOARD_TARGET);"
          "$s.IconLocation=$env:HOARD_TARGET+',0';$s.Description=$env:HOARD_DESC;$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=30,
                   creationflags=NO_WINDOW, env=dict(os.environ, HOARD_LNK=lnk, HOARD_TARGET=target, HOARD_DESC=desc))


def _start_menu():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Hoard.lnk")


def install(desktop=False):
    if not can_install():
        return {"error": "Installing is only for the Windows app (Hoard.exe)."}
    import winreg
    d = install_dir()
    os.makedirs(d, exist_ok=True)
    target = os.path.join(d, "Hoard.exe")
    if os.path.normcase(os.path.abspath(sys.executable)) != os.path.normcase(target):
        tmp = target + ".new"
        shutil.copyfile(sys.executable, tmp)
        if os.path.exists(target):
            try:
                os.replace(target, target + ".old")
            except OSError:
                pass
        os.replace(tmp, target)
    _shortcut(_start_menu(), target, "See what's taking up disk space")
    if desktop:
        _shortcut(os.path.join(os.path.expanduser("~"), "Desktop", "Hoard.lnk"), target, "See what's taking up disk space")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        for name, val in (("DisplayName", "Hoard"), ("DisplayVersion", core.VERSION), ("Publisher", "Hoard"),
                          ("DisplayIcon", target + ",0"), ("InstallLocation", d),
                          ("UninstallString", f'"{target}" --uninstall'), ("URLInfoAbout", "https://github.com/flamrot/hoard")):
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, os.path.getsize(target) // 1024)
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
    return {"ok": True, "path": target}


def uninstall_interactive():
    """Run from Settings → Apps → Hoard → Uninstall (Hoard.exe --uninstall)."""
    if plat.NAME != "windows":
        return 1
    import ctypes
    import winreg
    mb = ctypes.windll.user32.MessageBoxW
    if mb(None, "Remove Hoard from this computer?", "Uninstall Hoard", 0x4 | 0x20) != 6:     # Yes/No, question
        return 1
    keep = mb(None, "Keep your saved scans and settings?\n\nChoose No to delete them too.", "Uninstall Hoard", 0x4 | 0x20) == 6
    try:
        plat.schedule_remove()
    except Exception:  # noqa: BLE001
        pass
    for lnk in (_start_menu(), os.path.join(os.path.expanduser("~"), "Desktop", "Hoard.lnk")):
        try:
            os.remove(lnk)
        except OSError:
            pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    targets = [install_dir()] + ([] if keep else [core.DATA_DIR])
    # the running exe can't delete itself: a short-lived cmd does it once we've exited
    cmd = "ping -n 3 127.0.0.1 >nul & " + " & ".join(f'rmdir /s /q "{t}"' for t in targets)
    subprocess.Popen(["cmd", "/c", cmd], creationflags=NO_WINDOW | 0x00000008)
    mb(None, "Hoard has been removed.", "Uninstall Hoard", 0x40)
    return 0
