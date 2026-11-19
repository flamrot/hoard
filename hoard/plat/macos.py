import os
import shutil
import subprocess

NAME = "macos"
DATA_DIR = os.path.expanduser("~/Library/Application Support/Hoard")
TRASH_NAME = "Trash"
REVEAL_NAME = "Finder"

# On the startup disk, "/" already shows the Data volume through firmlinks (/Users, /Applications, …).
# Entering /System/Volumes would count everything twice; /Volumes holds the other disks.
ROOT_EXCLUDES = {"/System/Volumes", "/Volumes", "/dev", "/net", "/home", "/cores"}


def size_of(st):
    return st.st_blocks * 512   # real allocation: sparse files (Docker.raw, VM disks) and clones count correctly


def _startup_name():
    try:
        for n in os.listdir("/Volumes"):
            p = os.path.join("/Volumes", n)
            if os.path.islink(p) and os.path.realpath(p) == "/":
                return n
    except OSError:
        pass
    return "Macintosh HD"


def list_volumes():
    out = []
    roots = [("/", _startup_name())]
    try:
        for n in sorted(os.listdir("/Volumes")):
            p = os.path.join("/Volumes", n)
            if os.path.islink(p) or not os.path.ismount(p):
                continue
            roots.append((p, n))
    except OSError:
        pass
    for root, name in roots:
        try:
            u = shutil.disk_usage(root)
        except OSError:
            continue
        # APFS volumes share their container's space, so "used" is the container's total minus free.
        used = u.total - u.free
        out.append({"id": "root" if root == "/" else "vol-" + _slug(name), "root": root, "name": name,
                    "label": "Startup disk" if root == "/" else "External disk",
                    "type": "fixed" if root == "/" else "removable",
                    "total": u.total, "used": used, "free": u.free})
    return out


def _slug(t):
    from . import slug
    return slug(t)


def scan_excludes(root):
    return set(ROOT_EXCLUDES) if root == "/" else set()


def trash(path):
    try:
        from Foundation import NSFileManager, NSURL  # pyobjc, bundled with pywebview
        ok, _url, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            NSURL.fileURLWithPath_(path), None, None)
        if ok:
            return True, None
        return False, str(err.localizedDescription()) if err else "macOS couldn't move it to the Trash."
    except ImportError:
        pass
    r = subprocess.run(["osascript", "-e", "on run argv",
                        "-e", 'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)',
                        "-e", "end run", path], capture_output=True, text=True)
    if r.returncode == 0 and not os.path.lexists(path):
        return True, None
    return False, (r.stderr.strip() or "macOS couldn't move it to the Trash.")


def reveal(path):
    subprocess.Popen(["open", path] if os.path.isdir(path) else ["open", "-R", path])


SYSTEM = ("/System", "/usr", "/bin", "/sbin", "/private/etc", "/private/var/db", "/Library/Apple",
          "/Library/Extensions", "/Library/Frameworks")


def protected(root, parts, full):
    if root == "/" and len(parts) < 2:
        return "Top-level folders can't be removed from here."
    if root != "/" and not parts:
        return "That's the whole disk."
    if any(full == s or full.startswith(s + "/") for s in SYSTEM):
        return "macOS system files can't be removed from here."
    home = os.path.expanduser("~")
    guarded = {home} | {os.path.join(home, d) for d in
                        ("Library", "Documents", "Desktop", "Downloads", "Pictures", "Movies", "Music",
                         "Applications", "Library/Application Support", "Library/Containers")}
    if full in guarded:
        return "That's a core folder of your home folder. Remove what's inside it instead."
    return None


LABEL = "app.hoard.scan"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
SCHEDULE_NOTE = "Runs in the background through launchd, even when Hoard is closed. If the Mac is asleep at that time, it runs when it wakes."


def schedule_install(hour, minute, argv):
    import plistlib
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "wb") as f:
        plistlib.dump({"Label": LABEL, "ProgramArguments": list(argv),
                       "StartCalendarInterval": {"Hour": hour, "Minute": minute},
                       "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10}, f)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", domain, PLIST], capture_output=True, text=True)
    if r.returncode != 0:   # older macOS
        r = subprocess.run(["launchctl", "load", "-w", PLIST], capture_output=True, text=True)
    return (True, None) if r.returncode == 0 else (False, r.stderr.strip() or "launchd refused the job.")


def schedule_remove():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    try:
        os.remove(PLIST)
    except FileNotFoundError:
        pass
    except OSError as ex:
        return False, str(ex)
    return True, None


def schedule_status():
    return {"installed": os.path.exists(PLIST), "how": f"launchd agent {LABEL}"}


def notify(title, body):
    try:
        subprocess.run(["osascript", "-e", "on run argv", "-e",
                        "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                        title, body], capture_output=True, timeout=15)
    except Exception:  # noqa: BLE001
        pass


def app_browsers():
    cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
             "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
             "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    return [c for c in cands if os.path.exists(c)]
