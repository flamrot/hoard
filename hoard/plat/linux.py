import os
import shutil
import subprocess
import time
from urllib.parse import quote

NAME = "linux"
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "hoard")
TRASH_NAME = "Trash"
REVEAL_NAME = "Files"

REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "zfs", "jfs", "reiserfs", "bcachefs", "ntfs", "ntfs3",
           "fuseblk", "vfat", "exfat", "hfsplus", "nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "fuse.rclone"}
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "fuse.rclone"}
SKIP_PREFIX = ("/snap/", "/var/lib/docker/", "/var/lib/containers/", "/var/snap/", "/boot")


def size_of(st):
    return st.st_blocks * 512   # real allocation, so sparse VM/container images don't inflate the map


def cloud_size(st):
    return 0


def _unescape(s):
    return s.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


def _mounts():
    out = []
    try:
        with open("/proc/self/mounts", encoding="utf-8", errors="replace") as f:
            for line in f:
                p = line.split()
                if len(p) >= 3:
                    out.append((_unescape(p[0]), _unescape(p[1]), p[2]))
    except OSError:
        pass
    return out


def list_volumes():
    best = {}
    for src, mnt, fs in _mounts():
        if fs not in REAL_FS or mnt.startswith(SKIP_PREFIX) or mnt == "/boot":
            continue
        if mnt.startswith("/run/") and not mnt.startswith("/run/media/"):
            continue
        # btrfs subvolumes and bind mounts share a source: keep the shortest mount point once
        if src not in best or len(mnt) < len(best[src][0]):
            best[src] = (mnt, fs)
    out = []
    for src, (mnt, fs) in sorted(best.items(), key=lambda kv: (kv[1][0] != "/", kv[1][0])):
        try:
            u = shutil.disk_usage(mnt)
        except OSError:
            continue
        if u.total == 0:
            continue
        kind = "network" if fs in NETWORK_FS else \
            "removable" if mnt.startswith(("/media/", "/run/media/")) else "fixed"
        name = "/" if mnt == "/" else os.path.basename(mnt) or mnt
        out.append({"id": "root" if mnt == "/" else "mnt-" + _slug(mnt), "root": mnt, "name": name,
                    "label": "System" if mnt == "/" else mnt, "type": kind,
                    "total": u.total, "used": u.used, "free": u.free})
    return out


def _slug(t):
    from . import slug
    return slug(t)


def scan_excludes(root):
    """Stay on this volume: skip every other mount below root, except ones from the same device (btrfs subvolumes)."""
    mounts = _mounts()
    root_src = next((src for src, mnt, _ in mounts if mnt == root), None)
    prefix = root.rstrip("/") + "/"
    ex = {m for src, m, fs in mounts if m != root and m.startswith(prefix) and (src != root_src or fs not in REAL_FS)}
    if root == "/":
        ex |= {"/proc", "/sys", "/dev", "/run"}
    return ex


def _mount_point(path):
    p = os.path.abspath(path)
    while not os.path.ismount(p):
        p = os.path.dirname(p)
    return p


def _xdg_trash(path):
    """freedesktop.org Trash spec: home trash when on the same disk, otherwise $topdir/.Trash-$uid."""
    home_trash = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "Trash")
    os.makedirs(home_trash, mode=0o700, exist_ok=True)
    dev = os.lstat(path).st_dev
    if os.stat(home_trash).st_dev == dev:
        tdir, info_path = home_trash, os.path.abspath(path)
    else:
        top = _mount_point(path)
        tdir = os.path.join(top, f".Trash-{os.getuid()}")
        info_path = os.path.relpath(os.path.abspath(path), top)
    files, info = os.path.join(tdir, "files"), os.path.join(tdir, "info")
    os.makedirs(files, mode=0o700, exist_ok=True)
    os.makedirs(info, mode=0o700, exist_ok=True)
    base, n = os.path.basename(path.rstrip("/")), 1
    while True:
        name = base if n == 1 else f"{base}.{n}"
        try:
            fd = os.open(os.path.join(info, name + ".trashinfo"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            n += 1
    with os.fdopen(fd, "w") as f:
        f.write(f"[Trash Info]\nPath={quote(info_path)}\nDeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    try:
        os.rename(path, os.path.join(files, name))
    except OSError:
        os.unlink(os.path.join(info, name + ".trashinfo"))
        raise


def _env():
    from . import host_env
    return host_env()


def trash(path):
    for cmd in (["gio", "trash", "--", path], ["trash-put", "--", path], ["kioclient", "move", path, "trash:/"]):
        if shutil.which(cmd[0]):
            r = subprocess.run(cmd, capture_output=True, text=True, env=_env())
            if r.returncode == 0 and not os.path.lexists(path):
                return True, None
    try:
        _xdg_trash(path)
        return True, None
    except OSError as ex:
        return False, f"Couldn't move it to the Trash: {ex.strerror or ex}"


def reveal(path):
    uri = "file://" + quote(os.path.abspath(path))
    if shutil.which("gdbus"):
        r = subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.FileManager1",
                            "--object-path", "/org/freedesktop/FileManager1",
                            "--method", "org.freedesktop.FileManager1.ShowItems", f"['{uri}']", ""],
                           capture_output=True, timeout=5, env=_env())
        if r.returncode == 0:
            return
    subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_env())


SYSTEM = ("/usr", "/etc", "/boot", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/proc", "/sys", "/dev", "/run",
          "/var/lib/dpkg", "/var/lib/rpm", "/var/lib/apt", "/var/lib/pacman", "/snap")


def protected(root, parts, full):
    if root == "/" and len(parts) < 2:
        return "Top-level folders can't be removed from here."
    if root != "/" and not parts:
        return "That's the whole disk."
    if any(full == s or full.startswith(s + "/") for s in SYSTEM):
        return "System files can't be removed from here."
    home = os.path.expanduser("~")
    guarded = {home} | {os.path.join(home, d) for d in
                        (".local", ".local/share", ".config", "Documents", "Desktop", "Downloads", "Pictures",
                         "Videos", "Music")}
    if full in guarded:
        return "That's a core folder of your home folder. Remove what's inside it instead."
    return None


UNIT_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "systemd", "user")
CRON_TAG = "# hoard-daily-scan"
SCHEDULE_NOTE = "Runs in the background through a systemd user timer (or cron), even when Hoard is closed. With systemd, a scan missed while the computer was off runs at the next start."


def _systemd_ok():
    if not shutil.which("systemctl"):
        return False
    return subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, env=_env()).returncode == 0


def _sd_quote(a):
    return '"' + a.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _crontab(lines=None):
    if lines is None:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, env=_env())
        return r.stdout.splitlines() if r.returncode == 0 else []
    return subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", capture_output=True, text=True, env=_env())


def schedule_install(hour, minute, argv, elevated=False):
    schedule_remove()
    if _systemd_ok():
        os.makedirs(UNIT_DIR, exist_ok=True)
        with open(os.path.join(UNIT_DIR, "hoard-scan.service"), "w") as f:
            f.write("[Unit]\nDescription=Hoard daily disk scan\n\n[Service]\nType=oneshot\nNice=10\n"
                    "IOSchedulingClass=idle\nExecStart=" + " ".join(_sd_quote(a) for a in argv) + "\n")
        with open(os.path.join(UNIT_DIR, "hoard-scan.timer"), "w") as f:
            f.write(f"[Unit]\nDescription=Run Hoard's daily disk scan\n\n[Timer]\n"
                    f"OnCalendar=*-*-* {hour:02d}:{minute:02d}:00\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, env=_env())
        r = subprocess.run(["systemctl", "--user", "enable", "--now", "hoard-scan.timer"],
                           capture_output=True, text=True, env=_env())
        return (True, None) if r.returncode == 0 else (False, r.stderr.strip())
    if shutil.which("crontab"):
        cmd = " ".join(quote_sh(a) for a in argv)
        r = _crontab([ln for ln in _crontab() if CRON_TAG not in ln] + [f"{minute} {hour} * * * {cmd} {CRON_TAG}"])
        return (True, None) if r.returncode == 0 else (False, r.stderr.strip())
    return False, "Neither systemd nor cron is available to schedule the scan."


def quote_sh(a):
    import shlex
    return shlex.quote(a)


def schedule_remove():
    timer = os.path.join(UNIT_DIR, "hoard-scan.timer")
    if os.path.exists(timer):
        subprocess.run(["systemctl", "--user", "disable", "--now", "hoard-scan.timer"], capture_output=True, env=_env())
        for n in ("hoard-scan.timer", "hoard-scan.service"):
            try:
                os.remove(os.path.join(UNIT_DIR, n))
            except OSError:
                pass
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, env=_env())
    if shutil.which("crontab"):
        lines = _crontab()
        if any(CRON_TAG in ln for ln in lines):
            _crontab([ln for ln in lines if CRON_TAG not in ln])
    return True, None


def schedule_status():
    if os.path.exists(os.path.join(UNIT_DIR, "hoard-scan.timer")):
        return {"installed": True, "how": "systemd user timer hoard-scan.timer"}
    if shutil.which("crontab") and any(CRON_TAG in ln for ln in _crontab()):
        return {"installed": True, "how": "cron"}
    return {"installed": False, "how": ""}


def notify(title, body):
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-a", "Hoard", "-i", "drive-harddisk", title, body],
                           capture_output=True, timeout=15, env=_env())
        except Exception:  # noqa: BLE001
            pass


HIDDEN_NOTE = ("Space the system reports as used but the scan can't see: other users' files, root-only "
               "folders, reserved filesystem blocks and space held by deleted-but-open files.")


def _du(path):
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_blocks * 512
            except OSError:
                pass
    return total


def _trash_dir():
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "Trash")


def bin_size():
    return _du(_trash_dir())


def empty_bin():
    if shutil.which("gio"):
        r = subprocess.run(["gio", "trash", "--empty"], capture_output=True, text=True, env=_env())
        if r.returncode == 0:
            return True, None
    for sub in ("files", "info", "expunged"):
        d = os.path.join(_trash_dir(), sub)
        if not os.path.isdir(d):
            continue
        for n in os.listdir(d):
            p = os.path.join(d, n)
            try:
                shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
            except OSError:
                pass
    return True, None


def cleanup_specs():
    H = os.path.expanduser("~")
    C = os.environ.get("XDG_CACHE_HOME") or f"{H}/.cache"
    return [
        {"id": "bin", "group": "Trash", "title": "Trash", "desc": "Things you've already deleted. Emptying it frees the space for good.",
         "action": "empty_bin"},
        {"id": "thumbs", "group": "System", "title": "Thumbnail cache", "desc": "Picture previews; rebuilt as you browse.",
         "action": "clear", "paths": [f"{C}/thumbnails"]},
        {"id": "browsers", "group": "Browsers", "title": "Browser caches", "desc": "Cached pages and images. You stay signed in.",
         "action": "clear", "paths": [f"{C}/google-chrome", f"{C}/chromium", f"{C}/BraveSoftware", f"{C}/microsoft-edge",
                                      f"{C}/mozilla/firefox/*/cache2"]},
        {"id": "npm", "group": "Developer", "title": "npm, pip, uv and Yarn caches", "desc": "Downloaded packages; fetched again when needed.",
         "action": "clear", "paths": [f"{H}/.npm/_cacache", f"{C}/pip", f"{C}/uv", f"{C}/yarn"]},
        {"id": "gradle", "group": "Developer", "title": "Go, Gradle and Cargo caches", "desc": "Rebuilt on the next build.",
         "action": "clear", "paths": [f"{C}/go-build", f"{H}/.gradle/caches", f"{H}/.cargo/registry/cache"]},
        {"id": "hf", "group": "Developer", "title": "Downloaded AI models (Hugging Face)", "desc": "Pick the models you no longer use.",
         "action": "stage_children", "paths": [f"{C}/huggingface/hub"]},
        {"id": "node_modules", "group": "Your files", "title": "node_modules in inactive projects",
         "desc": "Projects you haven't changed for 90 days. Run npm install to get them back.", "action": "stage_files", "finder": "node_modules"},
        {"id": "downloads", "group": "Your files", "title": "Old installers and archives in Downloads",
         "desc": "Packages, disk images and archives older than 30 days.", "action": "stage_files", "finder": "downloads"},
        {"id": "journal", "group": "System", "title": "System journal logs", "desc": "Keeps the newest 200 MB of logs.",
         "action": "command", "command": "sudo journalctl --vacuum-size=200M", "admin": True, "paths": ["/var/log/journal"]},
        {"id": "apt", "group": "System", "title": "Package download cache", "desc": "Downloaded .deb files that are already installed.",
         "action": "command", "command": "sudo apt clean", "admin": True, "paths": ["/var/cache/apt/archives"]},
        {"id": "flatpak", "group": "System", "title": "Unused Flatpak runtimes", "desc": "Runtimes no installed app needs anymore.",
         "action": "command", "command": "flatpak uninstall --unused", "admin": False},
    ]


def installed_apps():
    import hashlib
    out = []
    if shutil.which("dpkg-query"):
        r = subprocess.run(["dpkg-query", "-W", "-f=${Package}\t${Installed-Size}\t${Version}\t${Status}\n"],
                           capture_output=True, text=True, env=_env())
        for line in r.stdout.splitlines():
            p = line.split("\t")
            if len(p) == 4 and p[3].endswith("installed") and p[1].isdigit() and int(p[1]) >= 20 * 1024:
                out.append({"id": hashlib.sha1(("deb" + p[0]).encode()).hexdigest()[:12], "name": p[0], "version": p[2],
                            "publisher": "apt", "location": "", "estimate": int(p[1]) * 1024, "date": "",
                            "uninstall": "command", "cmd": f"sudo apt remove {p[0]}"})
    if shutil.which("flatpak"):
        r = subprocess.run(["flatpak", "list", "--app", "--columns=application,name,version,size"],
                           capture_output=True, text=True, env=_env())
        for line in r.stdout.splitlines():
            p = line.split("\t")
            if len(p) >= 2:
                out.append({"id": hashlib.sha1(("fp" + p[0]).encode()).hexdigest()[:12], "name": p[1] or p[0],
                            "version": p[2] if len(p) > 2 else "", "publisher": "Flatpak", "location": "",
                            "estimate": 0, "date": "", "uninstall": "command", "cmd": f"flatpak uninstall {p[0]}"})
    if shutil.which("snap"):
        r = subprocess.run(["snap", "list"], capture_output=True, text=True, env=_env())
        for line in r.stdout.splitlines()[1:]:
            p = line.split()
            if p:
                out.append({"id": hashlib.sha1(("snap" + p[0]).encode()).hexdigest()[:12], "name": p[0],
                            "version": p[1] if len(p) > 1 else "", "publisher": "Snap", "location": "",
                            "estimate": 0, "date": "", "uninstall": "command", "cmd": f"sudo snap remove {p[0]}"})
    return out


def run_uninstall(app):
    return False, "Run the command shown in a terminal."


def app_browsers():
    from . import which_all
    return which_all(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                      "microsoft-edge", "microsoft-edge-stable", "brave-browser", "vivaldi"])
