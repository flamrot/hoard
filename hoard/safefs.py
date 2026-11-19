"""Handle-bound file operations for everything that changes the disk on the user's behalf (Files view, removals).

A `Dir` is a folder opened through a verified chain from the root of its disk:
  POSIX:   every folder opened with O_NOFOLLOW relative to the one above it; all work on its contents uses that
           descriptor (unlinkat, renameat2, openat …), so swapping a folder or anything above it for a link later
           changes nothing about where Hoard acts;
  Windows: every folder from the drive root down held open without delete sharing, so none of them can be
           renamed, removed or swapped for a junction while Hoard works inside (paths through them stay valid).
Each child is lstat-ed and checked right before it is touched: same identity as expected, same disk as its
folder (never into another mounted disk), not a link when entered, and not protected (plat.child_problem).
"""
import errno
import os
import stat

import plat

FD_OK = os.name != "nt" and os.scandir in os.supports_fd and \
    {os.open, os.stat, os.unlink, os.rmdir, os.mkdir, os.readlink, os.symlink} <= os.supports_dir_fd
DIRFLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)

CHANGED = "This changed since you chose it, so Hoard left it alone."
LINK = "Part of this path is a link, or it changed while Hoard was working, so Hoard left it alone."
OTHER_DISK = "Another disk is mounted here. Hoard doesn't go into it."


class Unsafe(Exception):
    """Refused for safety: protected, a link, another disk, or not the item that was chosen. Never retried."""


def ident(st):
    return (st.st_dev, st.st_ino)


def _same(st, expect):
    return expect is None or (st.st_ino != 0 and ident(st) == tuple(expect[:2]))


class Dir:
    def __init__(self, path, st, fd=None, pin=None, parent=None, owns_parent=False, guarded=True):
        self.path, self.st, self.fd, self.pin = path, st, fd, pin
        self.parent, self.owns_parent, self.guarded = parent, owns_parent, guarded

    # ---- opening
    @classmethod
    def open(cls, path, guard=True, expect=None, strict=False):
        """Open the folder at absolute `path`. Its real location is resolved first (links above it followed),
        unless `strict`: then any link on the way is refused (viewer reads). guard: refuse folders inside a
        protected tree (nothing may be written there) and anything that can't be resolved."""
        if strict:
            real = os.path.abspath(plat.strip_long(path))
        else:
            try:
                real = plat.canonical(path, follow=True)
            except OSError:
                raise Unsafe(plat.UNSURE)
        if guard:
            why = plat.tree_problem(real)
            if why:
                raise Unsafe(why)
        comps = plat._components(real)
        why = None
        if FD_OK:
            fd = os.open(comps[0], DIRFLAGS)
            try:
                st = os.fstat(fd)
                why = plat.id_tree_step(st, why)
                for pre in comps[1:]:
                    try:
                        nfd = os.open(os.path.basename(pre), DIRFLAGS, dir_fd=fd)
                    except OSError as ex:
                        if ex.errno in (errno.ELOOP, errno.ENOTDIR, getattr(errno, "EMLINK", -1)):
                            raise Unsafe(LINK)
                        raise
                    os.close(fd)
                    fd = nfd
                    st = os.fstat(fd)
                    why = plat.id_tree_step(st, why)
            except BaseException:
                os.close(fd)
                raise
            d = cls(real, st, fd=fd, guarded=guard)
        else:
            d = None
            try:
                for pre in comps:
                    pin = plat.pin_dir(pre)
                    if pin is None:
                        if not os.path.lexists(pre):
                            raise FileNotFoundError(errno.ENOENT, "It's no longer there.", pre)
                        raise Unsafe(LINK)
                    st = os.lstat(pre)
                    d = cls(pre, st, pin=pin, parent=d, owns_parent=True, guarded=guard)
                    why = plat.id_tree_step(st, why)
            except BaseException:
                if d is not None:
                    d.close()
                raise
        if guard and why:
            d.close()
            raise Unsafe(why)
        if not _same(d.st, expect):
            d.close()
            raise Unsafe(CHANGED)
        return d

    def sub(self, name, expect=None, guard=None, owns_parent=False):
        """Enter a child folder: never a link, never another disk, the expected one, and (guarded) not protected."""
        guard = self.guarded if guard is None else guard
        st = self.check(name, expect, guard)
        if plat.is_link(st) or not stat.S_ISDIR(st.st_mode):
            raise Unsafe(LINK)
        if self.fd is not None:
            try:
                nfd = os.open(name, DIRFLAGS, dir_fd=self.fd)
            except OSError as ex:
                if ex.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise Unsafe(LINK)
                raise
            fst = os.fstat(nfd)
            if ident(fst) != ident(st):
                os.close(nfd)
                raise Unsafe(CHANGED)
            return Dir(self.child(name), fst, fd=nfd, parent=self, owns_parent=owns_parent, guarded=guard)
        pin = plat.pin_dir(self.child(name), st.st_ino or None)
        if pin is None:
            raise Unsafe(CHANGED)
        now = os.lstat(self.child(name))
        if ident(now) != ident(st):
            plat.unpin_dir(pin)
            raise Unsafe(CHANGED)
        return Dir(self.child(name), now, pin=pin, parent=self, owns_parent=owns_parent, guarded=guard)

    def close(self):
        if self.fd is not None and self.fd >= 0:
            os.close(self.fd)
            self.fd = -1                                  # closed (still "descriptor mode")
        if self.pin is not None:
            plat.unpin_dir(self.pin)
            self.pin = None
        if self.owns_parent and self.parent is not None:
            self.parent.close()
            self.parent = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- looking
    def child(self, name):
        return os.path.join(self.path, name)

    def lstat(self, name):
        if self.fd is not None:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        return os.lstat(self.child(name))

    def exists(self, name):
        try:
            self.lstat(name)
            return True
        except FileNotFoundError:
            return False

    def names(self):
        if self.fd is not None:
            with os.scandir(self.fd) as it:
                return sorted(e.name for e in it)
        return sorted(os.listdir(self.path))

    def check(self, name, expect=None, guard=None):
        """lstat a child, refusing it if it isn't the expected one, is on another disk, or (guarded) protected."""
        guard = self.guarded if guard is None else guard
        st = self.lstat(name)
        if not _same(st, expect):
            raise Unsafe(CHANGED)
        if st.st_dev != self.st.st_dev:
            raise Unsafe(OTHER_DISK)
        if guard:
            why = plat.child_problem(self.path, name, st)
            if why:
                raise Unsafe(why)
        return st

    def guard_new(self, name):
        """A name about to be created here must not be a protected one (e.g. pagefile.sys at a drive root)."""
        if self.guarded:
            why = plat.child_problem(self.path, name)
            if why:
                raise Unsafe(why)

    # ---- changing
    def unlink(self, name, expect):
        st = self.check(name, expect)
        if stat.S_ISDIR(st.st_mode) and not plat.is_link(st):
            raise IsADirectoryError(errno.EISDIR, "That's a folder.", self.child(name))
        if self.fd is not None:
            os.unlink(name, dir_fd=self.fd)
            return st
        p = self.child(name)
        if plat.is_link(st) and getattr(st, "st_file_attributes", 0) & 0x10:
            os.rmdir(p)                                   # a junction / folder symlink: removes the link only
            return st
        try:
            os.unlink(p)
        except PermissionError:
            if plat.NAME != "windows" or plat.is_link(st):
                raise
            os.chmod(p, stat.S_IWRITE)                    # Windows read-only flag
            os.unlink(p)
        return st

    def rmdir(self, name, expect):
        st = self.check(name, expect)
        if self.fd is not None:
            os.rmdir(name, dir_fd=self.fd)
        else:
            os.rmdir(self.child(name))
        return st

    def mkdir(self, name):
        self.guard_new(name)
        if self.fd is not None:
            os.mkdir(name, 0o777, dir_fd=self.fd)
        else:
            os.mkdir(self.child(name))
        return self.lstat(name)

    def create(self, name):
        """A new file (fails if anything has that name), as a binary writable file object."""
        self.guard_new(name)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW
        if self.fd is not None:
            fd = os.open(name, flags, 0o666, dir_fd=self.fd)
        else:
            fd = os.open(self.child(name), flags, 0o666)
        return os.fdopen(fd, "wb")

    def open_read(self, name, expect=None):
        """Open a plain file for reading without following a link; the opened file must be the one checked."""
        st = self.check(name, expect, guard=False)
        if plat.is_link(st) or not stat.S_ISREG(st.st_mode):
            raise Unsafe("That isn't a plain file.")
        flags = os.O_RDONLY | NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(name, flags, dir_fd=self.fd) if self.fd is not None else os.open(self.child(name), flags)
        fst = os.fstat(fd)
        if ident(fst) != ident(st) or not stat.S_ISREG(fst.st_mode):
            os.close(fd)
            raise Unsafe(CHANGED)
        return os.fdopen(fd, "rb"), fst

    def rename_to(self, name, other, new, expect=None, guard_src=None):
        """Move child `name` to `other`/`new` (same disk), never replacing anything there."""
        self.check(name, expect, guard_src)
        other.guard_new(new)
        if self.fd is not None:
            plat.rename_noreplace(name, new, self.fd, other.fd)
        else:
            plat.rename_noreplace(self.child(name), other.child(new))

    def utime(self, name, ns):
        if self.fd is not None and os.utime in os.supports_dir_fd:
            os.utime(name, ns=ns, dir_fd=self.fd, follow_symlinks=False)
        elif self.fd is not None:                         # no utimensat with a folder handle: only for items
            os.utime(self.child(name), ns=ns,             # Hoard just created and verified, without following
                     follow_symlinks=os.utime not in os.supports_follow_symlinks)
        else:
            os.utime(self.child(name), ns=ns)

    def readlink(self, name):
        return os.readlink(name, dir_fd=self.fd) if self.fd is not None else os.readlink(self.child(name))

    def make_link(self, target, name, src_st):
        """Recreate a link (never what it points to)."""
        self.guard_new(name)
        if self.fd is not None:
            os.symlink(target, name, dir_fd=self.fd)
        elif getattr(src_st, "st_reparse_tag", 0) == 0xA0000003:
            import _winapi
            _winapi.CreateJunction(plat.strip_long(target), self.child(name))
        else:
            os.symlink(target, self.child(name), target_is_directory=bool(getattr(src_st, "st_file_attributes", 0) & 0x10))

    def trash(self, name, expect=None):
        """Send a child to the Recycle Bin / Trash. The OS takes a path, so the path is bound to what was checked:
        on Windows the folders above are held; on POSIX the path must still lead to this very folder and item.
        Returns (ok, error, where)."""
        st = self.check(name, expect)
        p = self.child(name)
        if self.fd is not None:
            try:
                same = ident(os.stat(self.path)) == ident(self.st) and ident(os.lstat(p)) == ident(st)
            except OSError:
                same = False
            if not same:
                raise Unsafe(CHANGED)
        return plat.trash_receipt(p)


def open_parent(path, guard=True, strict=False):
    """(Dir of the folder holding `path`, the item's name)."""
    folder, name = os.path.split(os.path.abspath(plat.strip_long(path)))
    if not name:
        raise Unsafe(plat.critical(path) or plat.UNSURE)
    return Dir.open(folder, guard=guard, strict=strict), name


def ensure_dir(path, strict=False):
    """Open the folder at path, creating missing folders one at a time through verified handles (never through
    a link). strict: the path must already be the real one (recorded by Hoard): any link on it is refused.
    Returns a Dir that keeps its chain held until closed."""
    real = os.path.abspath(plat.strip_long(path)) if strict else plat.canonical(path, follow=True)
    missing = []
    head = real
    while not os.path.lexists(head):
        head, name = os.path.split(head)
        if not name:
            raise FileNotFoundError(errno.ENOENT, "That drive isn't there.", path)
        missing.insert(0, name)
    d = Dir.open(head, guard=True, strict=strict)
    try:
        for name in missing:
            try:
                d.mkdir(name)
            except FileExistsError:
                pass
            d = d.sub(name, owns_parent=True)
    except BaseException:
        d.close()
        raise
    return d


def open_file_for_read(path):
    """The viewer's reads (fm_read, /fmfile): a plain file, reached without any link on the way, verified after
    opening. Returns (file object, fstat)."""
    d, name = open_parent(path, guard=False, strict=True)
    try:
        return d.open_read(name)
    finally:
        d.close()
