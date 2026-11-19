import os
import shutil
import subprocess

NAME = "macos"
DATA_DIR = os.path.expanduser("~/Library/Application Support/Hoard")
TRASH_NAME = "Trash"
REVEAL_NAME = "Finder"

# On the startup disk, "/" already shows the Data volume through firmlinks (/Users, /Applications, â€¦).
# Entering /System/Volumes would count everything twice; /Volumes holds the other disks.
ROOT_EXCLUDES = {"/System/Volumes", "/Volumes", "/dev", "/net", "/home", "/cores"}


def size_of(st):
    return st.st_blocks * 512   # real allocation: sparse files (Docker.raw, VM disks) and clones count correctly


def cloud_size(st):
    """iCloud Drive 'dataless' files (Optimize Mac Storage) have their bytes in iCloud."""
    return st.st_size if getattr(st, "st_flags", 0) & 0x40000000 else 0


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
        out.append({"id": "root" if root == "/" else _vol_id("vol", root),
                    "legacy": None if root == "/" else "vol-" + _slug(name), "root": root, "name": name,
                    "label": "Startup disk" if root == "/" else "External disk",
                    "type": "fixed" if root == "/" else "removable",
                    "total": u.total, "used": used, "free": u.free})
    return out


def _vol_id(prefix, key):
    from . import vol_id
    return vol_id(prefix, key)


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


def schedule_install(hour, minute, argv, elevated=False):
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


HIDDEN_NOTE = ("Space macOS reports as used but the scan can't see: purgeable space, local Time Machine "
               "snapshots, the system volume and folders Hoard isn't allowed to read (give it Full Disk Access).")


def _du(path):
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_blocks * 512
            except OSError:
                pass
    return total


def bin_size():
    return _du(os.path.expanduser("~/.Trash"))


def empty_bin():
    r = subprocess.run(["osascript", "-e", 'tell application "Finder" to empty trash'], capture_output=True, text=True)
    return (True, None) if r.returncode == 0 else (False, r.stderr.strip() or "Finder couldn't empty the Trash.")


def cleanup_specs():
    H = os.path.expanduser("~")
    return [
        {"id": "bin", "group": "Trash", "title": "Trash", "desc": "Things you've already deleted. Emptying it frees the space for good.",
         "action": "empty_bin"},
        {"id": "caches", "group": "System", "title": "App caches", "desc": "Caches in your Library folder. Apps rebuild them as needed.",
         "action": "clear", "paths": [f"{H}/Library/Caches/*"], "review": True},
        {"id": "logs", "group": "System", "title": "Old logs", "desc": "App logs older than a week.",
         "action": "clear", "paths": [f"{H}/Library/Logs"], "olderDays": 7},
        {"id": "xcode", "group": "Developer", "title": "Xcode build data", "desc": "DerivedData and simulator caches; rebuilt on the next build.",
         "action": "clear", "paths": [f"{H}/Library/Developer/Xcode/DerivedData", f"{H}/Library/Developer/CoreSimulator/Caches"]},
        {"id": "devicesupport", "group": "Developer", "title": "Old iOS device support files", "desc": "One folder per iOS version you've connected.",
         "action": "stage_children", "paths": [f"{H}/Library/Developer/Xcode/iOS DeviceSupport"]},
        {"id": "npm", "group": "Developer", "title": "npm, pip and uv caches", "desc": "Downloaded packages; fetched again when needed.",
         "action": "clear", "paths": [f"{H}/.npm/_cacache", f"{H}/.cache/uv", f"{H}/.cache/pip"]},
        {"id": "gradle", "group": "Developer", "title": "Gradle and Cargo caches", "desc": "Rebuilt on the next build.",
         "action": "clear", "paths": [f"{H}/.gradle/caches", f"{H}/.cargo/registry/cache"]},
        {"id": "hf", "group": "Developer", "title": "Downloaded AI models (Hugging Face)", "desc": "Pick the models you no longer use.",
         "action": "stage_children", "paths": [f"{H}/.cache/huggingface/hub"]},
        {"id": "brew", "group": "Developer", "title": "Homebrew downloads", "desc": "Old package downloads. Run this in Terminal.",
         "action": "command", "command": "brew cleanup --prune=all", "paths": [f"{H}/Library/Caches/Homebrew"]},
        {"id": "node_modules", "group": "Your files", "title": "node_modules in inactive projects",
         "desc": "Projects you haven't changed for 90 days. Run npm install to get them back.", "action": "stage_files", "finder": "node_modules"},
        {"id": "downloads", "group": "Your files", "title": "Old installers and archives in Downloads",
         "desc": "Disk images, installers and archives older than 30 days.", "action": "stage_files", "finder": "downloads"},
        {"id": "snapshots", "group": "System", "title": "Local Time Machine snapshots",
         "desc": "macOS frees these automatically when space runs low. To remove them now, run this in Terminal.",
         "action": "command", "command": "tmutil thinlocalsnapshots / 999999999999 4", "admin": False},
    ]


def installed_apps():
    import hashlib
    import plistlib
    out = []
    for base in ("/Applications", os.path.expanduser("~/Applications")):
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for n in names:
            if not n.endswith(".app"):
                continue
            p = os.path.join(base, n)
            info = {}
            try:
                with open(os.path.join(p, "Contents", "Info.plist"), "rb") as f:
                    info = plistlib.load(f)
            except Exception:  # noqa: BLE001
                pass
            if info.get("CFBundleIdentifier", "").startswith("com.apple."):
                continue
            out.append({"id": hashlib.sha1(p.encode()).hexdigest()[:12], "name": n[:-4],
                        "version": str(info.get("CFBundleShortVersionString", "")), "publisher": "",
                        "bundle": info.get("CFBundleIdentifier", ""), "location": p, "estimate": 0, "date": "",
                        "uninstall": "trash", "cmd": ""})
    return out


def run_uninstall(app):
    return False, "Move the app to the Trash from Hoard's removal list."


def context_menu_install(argv):
    return False, "On macOS, drag a folder onto Hoard's icon in the Dock instead (a Finder Quick Action isn't built in yet)."


def context_menu_remove():
    return True, None


def context_menu_status():
    return False


def app_browsers():
    cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
             "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
             "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    return [c for c in cands if os.path.exists(c)]
