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


def app_browsers():
    from . import which_all
    return which_all(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                      "microsoft-edge", "microsoft-edge-stable", "brave-browser", "vivaldi"])
