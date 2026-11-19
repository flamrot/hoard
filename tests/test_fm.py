"""Tests for the Files view (hoard/fm.py) and the system-critical guard (plat.critical).
Run:  python -m unittest discover -s tests -v
Works only inside temporary folders and a temporary data folder; nothing outside them is changed. Paths like
C:\\Windows are only ever *planned* (and refused), never acted on."""
import os
import sys
import tempfile
import time
import unittest

TMP = tempfile.mkdtemp(prefix="hoard-fm-tests-")
os.environ["HOARD_DATA_DIR"] = os.path.join(TMP, "data")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hoard"))

import core  # noqa: E402
import fm  # noqa: E402
import plat  # noqa: E402
import server  # noqa: E402

WIN = sys.platform == "win32"
# test_hoard.py may have been imported first and chosen the data folder: use whichever is in effect
DATA_PARENT = os.path.dirname(core.DATA_DIR)


def write(path, data=b"x", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def read(path):
    with open(path, "rb") as f:
        return f.read()


def make_junction(link, target):
    """A directory link: a junction on Windows (no admin needed), a symlink elsewhere."""
    if WIN:
        import _winapi
        _winapi.CreateJunction(target, link)
    else:
        os.symlink(target, link)


def wait(jid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        j = fm.JOBS[jid]
        if j.state in ("done", "cancelled", "failed"):
            return j
        time.sleep(0.01)
    raise AssertionError(f"job {jid} still {fm.JOBS[jid].state}")


def wait_question(jid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        j = fm.JOBS[jid]
        if j.question:
            return j.question
        if j.state in ("done", "cancelled", "failed"):
            raise AssertionError("job finished without asking")
        time.sleep(0.01)
    raise AssertionError("no question")


class other_disk:
    """Make every move take the copy-then-remove path, as between two disks."""
    def __enter__(self):
        self.orig = fm._same_disk_dirs, fm._same_disk
        fm._same_disk_dirs = lambda a, b: False
        fm._same_disk = lambda a, b: False

    def __exit__(self, *exc):
        fm._same_disk_dirs, fm._same_disk = self.orig


class protect:
    """Protect temp paths through the real rules (plat.TEST_PROTECT) for the duration of a with-block."""
    def __init__(self, trees=(), items=()):
        self.trees, self.items = list(trees), list(items)

    def __enter__(self):
        plat.TEST_PROTECT["trees"] += self.trees
        plat.TEST_PROTECT["items"] += self.items
        plat.reset_rules()
        return self

    def __exit__(self, *exc):
        for p in self.trees:
            plat.TEST_PROTECT["trees"].remove(p)
        for p in self.items:
            plat.TEST_PROTECT["items"].remove(p)
        plat.reset_rules()


def plan(op, **body):
    p = fm.fm_plan(dict(op=op, **body))
    assert "error" not in p, p
    return p


def run(op, conflict="ask", exclude=(), confirm=False, **body):
    p = plan(op, **body)
    r = fm.fm_start({"plan": p["plan"], "conflict": conflict, "exclude": list(exclude), "confirm": confirm})
    assert "job" in r, r
    return wait(r["job"])


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(dir=TMP)


class Listing(Base):
    def setUp(self):
        super().setUp()
        old = time.time() - 5000
        os.makedirs(os.path.join(self.d, "b_dir"))
        os.makedirs(os.path.join(self.d, "A_dir"))
        write(os.path.join(self.d, "z.txt"), b"z" * 10, old + 300)
        write(os.path.join(self.d, "a.mp4"), b"a" * 300, old + 100)
        write(os.path.join(self.d, "m.bin"), b"m" * 50, old + 200)
        write(os.path.join(self.d, ".hid"), b"h")

    def names(self, **kw):
        r = fm.fm_list(dict(path=self.d, **kw))
        self.assertNotIn("error", r)
        return [x["n"] for x in r["rows"]]

    def test_default_listing_folders_first_by_name(self):
        r = fm.fm_list({"path": self.d})
        self.assertEqual([x["n"] for x in r["rows"]], ["A_dir", "b_dir", "a.mp4", "m.bin", "z.txt"])
        self.assertEqual(r["path"], os.path.realpath(self.d))
        self.assertEqual(r["parent"], os.path.dirname(os.path.realpath(self.d)))
        self.assertEqual(r["crumbs"][-1]["path"], os.path.realpath(self.d))
        row = next(x for x in r["rows"] if x["n"] == "a.mp4")
        self.assertEqual((row["k"], row["s"], row["c"], row["p"]), ("f", 300, 0, None))
        self.assertTrue(row["id"][1])                          # a real file id, also on Windows
        self.assertEqual(row["id"][1], os.lstat(os.path.join(self.d, "a.mp4")).st_ino)
        self.assertIsNone(next(x for x in r["rows"] if x["n"] == "A_dir")["s"])   # not scanned: unknown

    def test_hidden_and_sorting(self):
        self.assertIn(".hid", self.names(hidden=True))
        self.assertEqual(self.names(sort="size", desc=True, foldersFirst=False)[:3], ["a.mp4", "m.bin", "z.txt"])
        self.assertEqual(self.names(sort="size", desc=False, foldersFirst=False)[:3], ["z.txt", "m.bin", "a.mp4"])
        self.assertEqual(self.names(sort="size", foldersFirst=False)[3:], ["A_dir", "b_dir"])   # unknown sizes last
        self.assertEqual(self.names(sort="mtime")[2:], ["a.mp4", "m.bin", "z.txt"])
        self.assertEqual(self.names(sort="ext", desc=True)[2:], ["z.txt", "a.mp4", "m.bin"])

    def test_branch_view_lists_every_file_below(self):
        write(os.path.join(self.d, "A_dir", "deep", "x.jpg"), b"1")
        write(os.path.join(self.d, "b_dir", ".secret", "y.jpg"), b"1")
        r = fm.fm_list({"path": self.d, "branch": True})
        names = sorted(x["n"] for x in r["rows"])
        self.assertEqual(names, sorted(["a.mp4", "m.bin", "z.txt", os.path.join("A_dir", "deep", "x.jpg")]))
        self.assertTrue(all(x["k"] == "f" for x in r["rows"]))
        self.assertFalse(r["truncated"])
        r = fm.fm_list({"path": self.d, "branch": True, "hidden": True})
        self.assertIn(os.path.join("b_dir", ".secret", "y.jpg"), [x["n"] for x in r["rows"]])

    def test_folder_sizes_come_from_the_scan(self):
        v = server.location_volume(self.d)
        server.VOLS[v["id"]] = v
        server.STATE.pop(v["id"], None)
        n = core.Node("root")
        c = core.Node("A_dir")
        c.size = 12345
        c.cats[0] = 12345
        n.dirs.append(c)
        n.size = 12345
        core.save_snapshot(v["id"], n, 1000, root=self.d)
        try:
            r = fm.fm_list({"path": self.d})
            self.assertEqual(r["scanned"], 1000)
            row = next(x for x in r["rows"] if x["n"] == "A_dir")
            self.assertEqual((row["s"], row["c"]), (12345, 0))
            self.assertIsNone(next(x for x in r["rows"] if x["n"] == "b_dir")["s"])
        finally:
            server.VOLS.pop(v["id"], None)

    def test_missing_and_bad_paths(self):
        self.assertIn("error", fm.fm_list({"path": os.path.join(self.d, "nope")}))
        self.assertIn("error", fm.fm_list({"path": os.path.join(self.d, "z.txt")}))
        self.assertEqual(fm.fm_list({"path": "relative/path"}), {"error": "Bad path."})
        if WIN:
            self.assertEqual(fm.fm_list({"path": "\\\\.\\PhysicalDrive0"}), {"error": "Bad path."})
            self.assertEqual(fm.fm_read({"path": os.path.join(self.d, "z.txt:stream")}), {"error": "Bad path."})


class Critical(Base):
    def test_profile_core_folders_and_hoard_data(self):
        docs = plat.documents_dir()
        self.assertIn("your Documents folder", plat.critical(docs))
        self.assertIn("your user folder", plat.critical(os.path.expanduser("~")))
        self.assertIsNone(plat.critical(os.path.join(docs, "some file.txt")))       # what's inside stays manageable
        self.assertIn("own data", plat.critical(core.DATA_DIR))
        self.assertIn("contains", plat.critical(DATA_PARENT))                        # holds Hoard's data folder
        self.assertIsNone(plat.critical(self.d))
        self.assertIsNone(plat.critical(os.path.join(self.d, "anything")))

    @unittest.skipUnless(WIN, "Windows rules")
    def test_windows_rules(self):
        C = plat.critical
        sysroot = os.environ.get("SystemRoot", "C:\\Windows")
        drive = os.path.splitdrive(sysroot)[0]
        for p in (sysroot, sysroot + "\\System32\\drivers", sysroot.upper() + ".", "\\\\?\\" + sysroot,
                  sysroot + "\\..\\" + os.path.basename(sysroot), drive + "\\", "Q:\\", drive + "\\Program Files",
                  drive + "\\PROGRA~1", "Q:\\Program Files (x86)", drive + "\\pagefile.sys", "Q:\\hiberfil.sys",
                  drive + "\\$Recycle.Bin", drive + "\\Users", drive + "\\ProgramData"):
            self.assertTrue(C(p), p)
        self.assertIn("Windows needs this folder", C(sysroot))
        self.assertIn("whole drive", C("Q:\\"))
        for p in (drive + "\\Program Files\\Some App", drive + "\\ProgramData\\Some App", "Q:\\Games",
                  "Q:\\stuff\\pagefile.sys"):
            self.assertIsNone(C(p), p)
        self.assertIn("contains", C(os.path.dirname(sys.executable)))             # Hoard runs from there

    @unittest.skipIf(WIN, "macOS/Linux rules")
    def test_posix_rules(self):
        C = plat.critical
        for p in ("/", "/usr", "/usr/bin/env", "/etc/hosts", "/bin", "/var", "/opt", "/dev", "/usr/local"):
            self.assertTrue(C(p), p)
        self.assertIsNone(C("/usr/local/some-tool"))
        self.assertIsNone(C("/opt/some-app"))

    def test_plans_refuse_critical_items(self):
        home = os.path.expanduser("~")
        docs = plat.documents_dir()
        targets = [docs, home, core.DATA_DIR, DATA_PARENT]
        if WIN:
            sysroot = os.environ.get("SystemRoot", "C:\\Windows")
            drive = os.path.splitdrive(sysroot)[0]
            targets += [sysroot, drive + "\\", drive + "\\Program Files", drive + "\\pagefile.sys"]
        targets = [t for t in targets if os.path.lexists(t)]
        for op in ("trash", "delete", "move"):
            for t in targets:
                body = {"sources": [t]}
                if op == "move":
                    body["dest"] = self.d
                p = plan(op, **body)
                self.assertEqual(p["items"], [], (op, t))
                self.assertTrue(p["blocked"][0]["reason"].startswith("Protected"), (op, t))
                # starting anyway is refused unless the blocked items are left out, and then nothing is left
                self.assertIn("error", fm.fm_start({"plan": p["plan"], "confirm": True}))
                self.assertIn("nothing left", fm.fm_start({"plan": p["plan"], "exclude": [t], "confirm": True})["error"])
        for t in targets:
            p = plan("rename", sources=[t], name="renamed")
            self.assertEqual(p["items"], [], t)
            self.assertTrue(p["blocked"][0]["reason"].startswith("Protected"))

    def test_overwriting_a_critical_item_is_refused(self):
        home = os.path.expanduser("~")
        docs_name = os.path.basename(plat.documents_dir())
        os.makedirs(os.path.join(self.d, docs_name))
        p = plan("copy", sources=[os.path.join(self.d, docs_name)], dest=os.path.dirname(plat.documents_dir()))
        self.assertEqual(p["items"], [])
        self.assertIn("Protected", p["blocked"][0]["reason"])
        write(os.path.join(self.d, "f.txt"))
        p = plan("copy", sources=[os.path.join(self.d, "f.txt")], dest=self.d, name=os.path.basename(core.DATA_DIR))
        self.assertEqual(len(p["items"]), 1)                   # same name elsewhere: fine
        p = plan("copy", sources=[os.path.join(self.d, "f.txt")], dest=DATA_PARENT, name=os.path.basename(core.DATA_DIR))
        self.assertEqual(p["items"], [])                       # would replace Hoard's data folder
        self.assertIn("own data", p["blocked"][0]["reason"])
        if WIN:
            drive = os.path.splitdrive(os.environ.get("SystemRoot", "C:\\Windows"))[0] + "\\"
            write(os.path.join(self.d, "pagefile.sys"))
            p = plan("copy", sources=[os.path.join(self.d, "pagefile.sys")], dest=drive)
            self.assertEqual(p["items"], [])
            self.assertIn("Windows needs this file", p["blocked"][0]["reason"])
            p = plan("mkdir", dest=os.environ.get("SystemRoot", "C:\\Windows"), name="hoard-test")
            self.assertEqual(p["items"], [])                   # nothing gets written inside Windows
        p = plan("rename", sources=[os.path.join(self.d, "f.txt")], name="ok.txt")
        self.assertEqual(len(p["items"]), 1)
        self.assertEqual(p["blocked"], [])

    def test_server_trash_any_refuses_critical_items(self):
        # a location whose own rules (plat.protected) allow these, so only plat.critical stands in the way
        v = server.location_volume(DATA_PARENT)
        server.VOLS[v["id"]] = v
        server.STATE.pop(v["id"], None)
        outer = server.location_volume(os.path.dirname(DATA_PARENT))
        server.VOLS[outer["id"]] = outer
        server.STATE.pop(outer["id"], None)
        try:
            ok, err = server.trash_any(v["id"], [os.path.basename(core.DATA_DIR)])
            self.assertFalse(ok)
            self.assertIn("own data", err)
            ok, err = server.trash_any(outer["id"], [os.path.basename(DATA_PARENT)])
            self.assertFalse(ok)
            self.assertIn("contains", err)
            self.assertTrue(os.path.isdir(core.DATA_DIR))
        finally:
            server.VOLS.pop(v["id"], None)
            server.VOLS.pop(outer["id"], None)


class Links(Base):
    def setUp(self):
        super().setUp()
        self.outside = tempfile.mkdtemp(dir=TMP)
        write(os.path.join(self.outside, "precious.txt"), b"keep me")
        self.work = os.path.join(self.d, "work")
        write(os.path.join(self.work, "own.txt"), b"mine")
        self.link = os.path.join(self.work, "link")
        make_junction(self.link, self.outside)

    def test_listed_as_link_and_never_entered(self):
        r = fm.fm_list({"path": self.work})
        self.assertEqual({x["n"]: x["k"] for x in r["rows"]}, {"link": "l", "own.txt": "f"})
        r = fm.fm_list({"path": self.d, "branch": True})
        self.assertEqual(sorted(x["n"] for x in r["rows"]), sorted([os.path.join("work", "own.txt"), os.path.join("work", "link")]))
        j = wait(fm.fm_find({"start": self.d, "name": "*.txt"})["job"])
        self.assertEqual([x["n"] for x in j.result["rows"]], [os.path.join("work", "own.txt")])
        self.assertIn("error", fm.fm_read({"path": self.link}))

    def test_delete_removes_the_link_not_the_target(self):
        j = run("delete", sources=[self.work], confirm=True)
        self.assertEqual(j.state, "done", j.errors)
        self.assertFalse(os.path.lexists(self.work))
        self.assertEqual(read(os.path.join(self.outside, "precious.txt")), b"keep me")

    def test_copy_and_move_carry_the_link_itself(self):
        dest = tempfile.mkdtemp(dir=TMP)
        j = run("copy", sources=[self.work], dest=dest)
        self.assertEqual(j.state, "done", j.errors)
        copied = os.path.join(dest, "work", "link")
        self.assertTrue(plat.is_link(os.lstat(copied)))
        self.assertEqual(os.listdir(self.outside), ["precious.txt"])
        dest2 = tempfile.mkdtemp(dir=TMP)
        j = run("move", sources=[self.work], dest=dest2)
        self.assertEqual(j.state, "done", j.errors)
        self.assertTrue(plat.is_link(os.lstat(os.path.join(dest2, "work", "link"))))
        self.assertEqual(read(os.path.join(self.outside, "precious.txt")), b"keep me")

    def test_writes_never_go_through_a_link_in_the_destination(self):
        # merging a folder into one whose subfolder is now a link: the link is a conflict, never written through
        src = os.path.join(self.d, "src")
        write(os.path.join(src, "work", "link", "new.txt"), b"new")
        j = run("copy", sources=[os.path.join(src, "work")], dest=self.d, conflict="skip")
        self.assertEqual(j.state, "done", j.errors)
        self.assertEqual(os.listdir(self.outside), ["precious.txt"])


class Operations(Base):
    def test_copy_file_and_folder_preserving_times(self):
        old = time.time() - 10 * 86400
        write(os.path.join(self.d, "src", "a.txt"), b"hello", old)
        write(os.path.join(self.d, "src", "sub", "b.txt"), b"world", old)
        dest = os.path.join(self.d, "dest")
        os.makedirs(dest)
        j = run("copy", sources=[os.path.join(self.d, "src")], dest=dest)
        self.assertEqual(j.state, "done", j.errors)
        self.assertEqual(read(os.path.join(dest, "src", "sub", "b.txt")), b"world")
        self.assertAlmostEqual(os.path.getmtime(os.path.join(dest, "src", "a.txt")), old, delta=2)
        self.assertEqual((j.total_n, j.total_b, j.done_b), (2, 10, 10))
        self.assertFalse([n for n in os.listdir(dest) if n.endswith(".part")])
        self.assertTrue(j.journal)

    def test_copy_into_same_folder_keeps_both(self):
        write(os.path.join(self.d, "a.txt"), b"1")
        j = run("copy", sources=[os.path.join(self.d, "a.txt")], dest=self.d)
        self.assertEqual(j.state, "done", j.errors)
        self.assertEqual(sorted(os.listdir(self.d)), ["a (2).txt", "a.txt"])

    def test_folder_cannot_go_into_itself(self):
        os.makedirs(os.path.join(self.d, "f", "inner"))
        for op in ("copy", "move"):
            p = plan(op, sources=[os.path.join(self.d, "f")], dest=os.path.join(self.d, "f", "inner"))
            self.assertEqual(p["items"], [])
            self.assertIn("into itself", p["blocked"][0]["reason"])

    def _conflict_setup(self):
        src = write(os.path.join(self.d, "src", "a.txt"), b"new content", time.time())
        dst = write(os.path.join(self.d, "dst", "a.txt"), b"old", time.time() - 1000)
        return src, dst

    def test_conflict_skip_keep_and_rules(self):
        src, dst = self._conflict_setup()
        p = plan("copy", sources=[src], dest=os.path.dirname(dst))
        self.assertEqual(len(p["conflicts"]), 1)
        self.assertEqual((p["conflicts"][0]["srcSize"], p["conflicts"][0]["dstSize"]), (11, 3))
        run("copy", sources=[src], dest=os.path.dirname(dst), conflict="skip")
        self.assertEqual(read(dst), b"old")
        run("copy", sources=[src], dest=os.path.dirname(dst), conflict="keep")
        self.assertEqual(read(os.path.join(self.d, "dst", "a (2).txt")), b"new content")
        self.assertEqual(read(dst), b"old")
        os.utime(src, (time.time() - 5000, time.time() - 5000))         # source is older now
        run("copy", sources=[src], dest=os.path.dirname(dst), conflict="newer")
        self.assertEqual(read(dst), b"old")
        run("copy", sources=[src], dest=os.path.dirname(dst), conflict="differs")      # sizes differ
        self.assertEqual(read(dst), b"new content")                    # replaced; the old one went to the bin

    def test_conflict_question_and_answers(self):
        src, dst = self._conflict_setup()
        p = plan("copy", sources=[src], dest=os.path.dirname(dst))
        jid = fm.fm_start({"plan": p["plan"], "conflict": "ask"})["job"]
        q = wait_question(jid)
        self.assertEqual((q["kind"], q["dst"]), ("conflict", dst))
        self.assertIn("already exists", q["message"])
        self.assertIn("error", fm.fm_decide({"job": jid, "answer": "retry"}))       # not one of the choices
        self.assertEqual(fm.fm_decide({"job": jid, "answer": "skip"}), {"ok": True})
        j = wait(jid)
        self.assertEqual((j.state, j.counts["skipped"]), ("done", 1))
        self.assertEqual(read(dst), b"old")

    def test_move_and_rename_conflicts(self):
        src, dst = self._conflict_setup()
        j = run("move", sources=[src], dest=os.path.dirname(dst), conflict="keep")
        self.assertEqual(j.state, "done", j.errors)
        self.assertFalse(os.path.exists(src))
        self.assertEqual(read(os.path.join(self.d, "dst", "a (2).txt")), b"new content")
        p = plan("rename", sources=[os.path.join(self.d, "dst", "a (2).txt")], name="a.txt")
        self.assertEqual(p["items"], [])
        self.assertIn("already exists", p["blocked"][0]["reason"])
        p = plan("move", sources=[dst], dest=os.path.dirname(dst))
        self.assertIn("already in that folder", p["blocked"][0]["reason"])
        for bad in ("a/b", "", "..") + (("con.txt", "x?", "dot.") if WIN else ()):
            self.assertIn("error", fm.fm_plan({"op": "rename", "sources": [dst], "name": bad}), bad)
        self.assertIn("at most one", fm.fm_plan({"op": "copy", "sources": [dst], "dest": self.d, "pattern": "*-*"})["error"])

    def test_rename_many_handles_swaps(self):
        a = write(os.path.join(self.d, "a.txt"), b"A")
        b = write(os.path.join(self.d, "b.txt"), b"B")
        j = run("rename_many", renames=[[a, "b.txt"], [b, "a.txt"]])
        self.assertEqual(j.state, "done", j.errors)
        self.assertEqual((read(a), read(b)), (b"B", b"A"))
        p = plan("rename_many", renames=[[a, "c.txt"], [b, "c.txt"]])
        self.assertEqual(len(p["items"]), 1)
        self.assertIn("Two items", p["blocked"][0]["reason"])
        wait(fm.fm_undo({"journal": j.journal})["job"])
        self.assertEqual((read(a), read(b)), (b"A", b"B"))

    def test_changed_between_plan_and_run_is_skipped(self):
        f = write(os.path.join(self.d, "f.txt"), b"original")
        p = plan("trash", sources=[f])
        new = write(os.path.join(self.d, "new.tmp"), b"something else")    # a different file takes its place
        os.remove(f)
        os.rename(new, f)
        j = wait(fm.fm_start({"plan": p["plan"]})["job"])
        self.assertIn("changed since you chose it", j.errors[0]["message"])
        self.assertEqual(read(f), b"something else")
        self.assertEqual(j.counts["done"], 0)

    def test_delete_needs_confirmation_and_plans_expire(self):
        f = write(os.path.join(self.d, "f.txt"))
        p = plan("delete", sources=[f])
        self.assertFalse(p["undoable"])
        self.assertIn("confirmation", fm.fm_start({"plan": p["plan"]})["error"])
        self.assertTrue(os.path.exists(f))
        fm.PLANS[p["plan"]]["time"] -= fm.PLAN_TTL + 1
        self.assertIn("expired", fm.fm_start({"plan": p["plan"], "confirm": True})["error"])
        j = run("delete", sources=[f], confirm=True)
        self.assertEqual(j.state, "done")
        self.assertFalse(os.path.exists(f))
        self.assertIsNone(j.journal)

    def test_mkdir_and_mkfile(self):
        j = run("mkdir", dest=self.d, name="New folder")
        self.assertTrue(os.path.isdir(os.path.join(self.d, "New folder")))
        p = plan("mkdir", dest=self.d, name="New folder")
        self.assertIn("already exists", p["blocked"][0]["reason"])
        run("mkfile", dest=self.d, name="note.txt")
        self.assertEqual(read(os.path.join(self.d, "note.txt")), b"")
        self.assertTrue(j.journal)

    def test_cancel_mid_copy_leaves_no_partial_file(self):
        write(os.path.join(self.d, "src", "big.bin"), os.urandom(3 << 20))
        dest = os.path.join(self.d, "dest")
        os.makedirs(dest)
        orig = fm.Job.add_bytes

        def add_then_cancel(job, n):                  # cancel as soon as the first chunk is written
            orig(job, n)
            if job.done_b > 0:
                job.cancel = True
        fm.Job.add_bytes = add_then_cancel
        try:
            j = run("copy", sources=[os.path.join(self.d, "src", "big.bin")], dest=dest)
        finally:
            fm.Job.add_bytes = orig
        self.assertEqual(j.state, "cancelled")
        self.assertEqual(os.listdir(dest), [])        # neither big.bin nor a .part file
        self.assertIn("Stopped.", j.summary)

    def test_pause_and_resume(self):
        write(os.path.join(self.d, "src", "a.bin"), os.urandom(3 << 20))
        dest = os.path.join(self.d, "dest")
        os.makedirs(dest)
        orig = fm.Job.add_bytes

        def add_then_pause(job, n):
            orig(job, n)
            if job.done_b == fm.CHUNK:
                job._go.clear()
        fm.Job.add_bytes = add_then_pause
        try:
            p = plan("copy", sources=[os.path.join(self.d, "src", "a.bin")], dest=dest)
            jid = fm.fm_start({"plan": p["plan"]})["job"]
            end = time.time() + 10
            while fm.JOBS[jid].state != "paused" and time.time() < end:
                time.sleep(0.01)
            self.assertEqual(fm.JOBS[jid].state, "paused")
            self.assertFalse(os.path.exists(os.path.join(dest, "a.bin")))
            fm.fm_control({"job": jid, "action": "resume"})
            j = wait(jid)
        finally:
            fm.Job.add_bytes = orig
        self.assertEqual(j.state, "done")
        self.assertEqual(os.path.getsize(os.path.join(dest, "a.bin")), 3 << 20)


class Undo(Base):
    def undo(self, journal):
        r = fm.fm_undo({"journal": journal})
        self.assertIn("job", r, r)
        return wait(r["job"])

    def test_undo_rename(self):
        f = write(os.path.join(self.d, "old.txt"), b"1")
        j = run("rename", sources=[f], name="new.txt")
        self.assertTrue(os.path.exists(os.path.join(self.d, "new.txt")))
        u = self.undo(j.journal)
        self.assertEqual(u.state, "done", u.errors)
        self.assertEqual(os.listdir(self.d), ["old.txt"])
        self.assertIn("already", fm.fm_undo({"journal": j.journal})["error"])
        hist = fm.fm_jobs({})["history"]
        self.assertTrue(any(h["id"] == j.id and h["undo"] is None for h in hist))

    def test_undo_move(self):
        f = write(os.path.join(self.d, "a", "f.txt"), b"1")
        os.makedirs(os.path.join(self.d, "b"))
        j = run("move", sources=[f], dest=os.path.join(self.d, "b"))
        self.assertFalse(os.path.exists(f))
        u = self.undo(j.journal)
        self.assertEqual(u.state, "done", u.errors)
        self.assertEqual(read(f), b"1")
        self.assertEqual(os.listdir(os.path.join(self.d, "b")), [])

    def test_move_between_disks_copies_then_removes_and_undoes(self):
        write(os.path.join(self.d, "a", "f", "x.txt"), b"x")
        write(os.path.join(self.d, "a", "f", "sub", "y.txt"), b"yy")
        os.makedirs(os.path.join(self.d, "b"))
        with other_disk():                                     # take the copy-then-remove path
            j = run("move", sources=[os.path.join(self.d, "a", "f")], dest=os.path.join(self.d, "b"))
            self.assertEqual(j.state, "done", j.errors)
            self.assertFalse(os.path.exists(os.path.join(self.d, "a", "f")))
            self.assertEqual(read(os.path.join(self.d, "b", "f", "sub", "y.txt")), b"yy")
            u = self.undo(j.journal)
        self.assertEqual(u.errors, [])
        self.assertEqual(read(os.path.join(self.d, "a", "f", "sub", "y.txt")), b"yy")
        self.assertEqual(os.listdir(os.path.join(self.d, "b")), [])

    def test_undo_copy_moves_unchanged_copies_to_the_trash(self):
        src = write(os.path.join(self.d, "src", "c.txt"), b"copy me")
        dest = os.path.join(self.d, "dest")
        os.makedirs(dest)
        j = run("copy", sources=[src], dest=dest)
        self.assertTrue(os.path.exists(os.path.join(dest, "c.txt")))
        u = self.undo(j.journal)                                 # a file this test created, in a temp folder
        self.assertEqual(u.errors, [])
        self.assertEqual(os.listdir(dest), [])
        self.assertTrue(os.path.exists(src))

    def test_undo_mkdir_only_when_still_empty(self):
        j = run("mkdir", dest=self.d, name="made")
        self.assertEqual(self.undo(j.journal).errors, [])
        self.assertFalse(os.path.exists(os.path.join(self.d, "made")))
        j = run("mkdir", dest=self.d, name="made")
        write(os.path.join(self.d, "made", "work.txt"))
        u = self.undo(j.journal)
        self.assertIn("isn't empty", u.errors[0]["message"])
        self.assertTrue(os.path.exists(os.path.join(self.d, "made", "work.txt")))

    def test_undo_never_overwrites_newer_work(self):
        f = write(os.path.join(self.d, "old.txt"), b"1")
        j = run("rename", sources=[f], name="new.txt")
        write(f, b"someone made a new old.txt")
        u = self.undo(j.journal)
        self.assertTrue(u.errors)
        self.assertEqual(read(f), b"someone made a new old.txt")
        self.assertTrue(os.path.exists(os.path.join(self.d, "new.txt")))

    def test_undo_of_changed_item_is_refused(self):
        f = write(os.path.join(self.d, "a.txt"), b"1")
        j = run("rename", sources=[f], name="b.txt")
        b = os.path.join(self.d, "b.txt")
        os.rename(write(os.path.join(self.d, "c.tmp"), b"2"), b + ".new")
        os.remove(b)
        os.rename(b + ".new", b)
        u = self.undo(j.journal)
        self.assertEqual(u.errors[0]["message"], fm.UNDO_CHANGED)
        self.assertFalse(os.path.exists(f))

    def test_trash_and_put_back(self):
        f = write(os.path.join(self.d, "trash-me.txt"), b"bin")    # a file this test created, in a temp folder
        j = run("trash", sources=[f])
        self.assertEqual(j.state, "done", j.errors)
        self.assertFalse(os.path.exists(f))
        entry = next(e for e in fm._journal_read() if e["id"] == j.journal)
        if entry["actions"][0]["where"] is None:
            self.skipTest("this system doesn't show where trashed items go")
        u = self.undo(j.journal)
        self.assertEqual(u.errors, [])
        self.assertEqual(read(f), b"bin")


class FindAndRead(Base):
    def setUp(self):
        super().setUp()
        write(os.path.join(self.d, "a.txt"), b"there is a needle here")
        write(os.path.join(self.d, "sub", "b.log"), b"x" * (fm.CHUNK - 3) + b"NEEDLE across the chunk edge")
        write(os.path.join(self.d, "sub", "c.bin"), b"nothing")
        write(os.path.join(self.d, ".hidden", "d.txt"), b"needle")

    def find(self, **kw):
        j = wait(fm.fm_find(dict(start=self.d, **kw))["job"])
        self.assertEqual(j.state, "done", j.errors)
        return sorted(r["n"] for r in j.result["rows"])

    def test_find_by_name(self):
        self.assertEqual(self.find(name="*.txt;*.log"), ["a.txt", os.path.join("sub", "b.log")])
        self.assertEqual(self.find(name="C.BIN"), [os.path.join("sub", "c.bin")])      # plain text: contains
        self.assertEqual(self.find(name="sub"), ["sub"])
        self.assertEqual(self.find(name="*.txt", hidden=True), sorted(["a.txt", os.path.join(".hidden", "d.txt")]))

    def test_find_by_content(self):
        self.assertEqual(self.find(text="needle"), ["a.txt", os.path.join("sub", "b.log")])
        self.assertEqual(self.find(text="needle", case=True), ["a.txt"])
        self.assertEqual(self.find(text="NE+DLE a", regex=True, case=True), [os.path.join("sub", "b.log")])
        self.assertEqual(self.find(name="*.bin", text="needle"), [])
        self.assertIn("error", fm.fm_find({"start": self.d, "text": "(", "regex": True}))

    def test_read_bounds(self):
        f = write(os.path.join(self.d, "big.bin"), os.urandom(3 << 20))
        import base64
        r = fm.fm_read({"path": f, "offset": 0, "length": 10 << 20})
        self.assertEqual(len(base64.b64decode(r["data"])), fm.MAX_READ)
        self.assertEqual((r["size"], r["eof"]), (3 << 20, False))
        r = fm.fm_read({"path": f, "offset": (3 << 20) - 5, "length": 100})
        self.assertEqual((len(base64.b64decode(r["data"])), r["eof"]), (5, True))
        r = fm.fm_read({"path": f, "offset": 10 << 20, "length": 100})
        self.assertEqual((r["data"], r["eof"]), ("", True))
        self.assertIn("error", fm.fm_read({"path": f, "offset": -1, "length": 10}))
        self.assertIn("error", fm.fm_read({"path": f, "offset": "x"}))
        self.assertIn("error", fm.fm_read({"path": self.d}))
        self.assertIn("error", fm.fm_read({"path": os.path.join(self.d, "missing")}))

    def test_sizes_job(self):
        j = wait(fm.fm_sizes({"paths": [os.path.join(self.d, "sub")]})["job"])
        size = j.result["sizes"][os.path.join(self.d, "sub")]
        self.assertGreaterEqual(size, fm.CHUNK)

    def test_prefs_round_trip(self):
        r = fm.fm_prefs({"set": {"hotlist": [{"name": "Temp", "path": self.d}], "density": "comfortable",
                                 "bogus": 1, "hidden": "yes"}})
        self.assertEqual(r["prefs"]["hotlist"][0]["path"], self.d)
        self.assertNotIn("bogus", r["prefs"])
        self.assertIs(r["prefs"]["hidden"], False)                  # wrong type ignored
        self.assertEqual(fm.fm_prefs({})["prefs"]["density"], "comfortable")
        self.assertTrue(os.path.exists(os.path.join(core.DATA_DIR, "fm.json")))


class Hardening(Base):
    """The review findings: aliases, recursion, undo, interrupted jobs, no-replace renames, reads, busy()."""

    def setUp(self):
        super().setUp()
        self.d = os.path.realpath(self.d)

    # ---- 1. identity-based protection through an alias
    def test_protected_tree_reached_through_a_link_or_second_path(self):
        tree_dir = os.path.join(self.d, "system")
        write(os.path.join(tree_dir, "core.dll"), b"keep")
        alias = os.path.join(self.d, "alias")
        make_junction(alias, tree_dir)
        with protect(trees=[tree_dir]):
            via = os.path.join(alias, "core.dll")
            self.assertIn("test tree", plat.critical(via))
            for op in ("trash", "delete", "move"):
                body = {"sources": [via]}
                if op == "move":
                    body["dest"] = self.d
                p = plan(op, **body)
                self.assertEqual(p["items"], [], op)
            p = plan("copy", sources=[write(os.path.join(self.d, "new.dll"), b"x")], dest=alias)
            self.assertEqual(p["items"], [])                        # nothing written into it either
            v = server.location_volume(self.d)
            server.VOLS[v["id"]] = v
            try:
                ok, err = server.trash_any(v["id"], ["system", "core.dll"])
                self.assertFalse(ok)
                self.assertIn("test tree", err)
            finally:
                server.VOLS.pop(v["id"], None)
        self.assertEqual(read(os.path.join(tree_dir, "core.dll")), b"keep")

    def test_protected_item_reached_through_a_hard_link(self):
        item = write(os.path.join(self.d, "a", "boot.ini"), b"keep")
        alias = os.path.join(self.d, "b", "renamed.txt")
        os.makedirs(os.path.dirname(alias))
        os.link(item, alias)                                 # same file, different path: only the identity matches
        with protect(items=[item]):
            self.assertIn("test item", plat.critical(alias))
            self.assertEqual(plan("trash", sources=[alias])["items"], [])
            self.assertEqual(plan("rename", sources=[alias], name="x.txt")["items"], [])

    @unittest.skipUnless(WIN, "subst drives are Windows-only")
    def test_protected_tree_through_a_subst_drive(self):
        import string
        import subprocess
        tree_dir = os.path.join(self.d, "subst-tree")
        write(os.path.join(tree_dir, "sys", "kernel.bin"), b"keep")
        letter = next((L for L in reversed(string.ascii_uppercase[3:]) if not os.path.exists(L + ":\\")), None)
        if letter is None:
            self.skipTest("no free drive letter")
        r = subprocess.run(["subst", letter + ":", tree_dir], capture_output=True)
        if r.returncode != 0:
            self.skipTest("subst isn't available")
        try:
            with protect(trees=[tree_dir]):
                via = letter + ":\\sys\\kernel.bin"
                self.assertTrue(plat.critical(via))
                self.assertTrue(plat.critical(letter + ":\\sys"))
                for op in ("trash", "delete"):
                    self.assertEqual(plan(op, sources=[via])["items"], [], op)
                self.assertEqual(plan("mkdir", dest=letter + ":\\sys", name="x")["items"], [])
        finally:
            subprocess.run(["subst", letter + ":", "/d"], capture_output=True)
        self.assertEqual(read(os.path.join(tree_dir, "sys", "kernel.bin")), b"keep")

    @unittest.skipUnless(WIN, "Windows shares")
    def test_administrative_and_loopback_shares_are_refused(self):
        f = write(os.path.join(self.d, "share.txt"), b"keep")
        drive, rest = os.path.splitdrive(f)
        for via in ("\\\\localhost\\" + drive[0] + "$" + rest, "\\\\127.0.0.1\\" + drive[0] + "$" + rest,
                    "\\\\?\\UNC\\localhost\\" + drive[0] + "$" + rest, "\\\\fileserver\\ADMIN$\\x"):
            self.assertIn("administrative", plat.critical(via), via)
        self.assertIn("administrative", plat.tree_problem("\\\\localhost\\" + drive[0] + "$\\"))
        self.assertTrue(os.path.exists(f))

    def test_unresolvable_paths_fail_closed(self):
        orig = plat.canonical

        def broken(path, follow=False):
            raise OSError(5, "can't tell")
        plat.canonical = broken
        try:
            self.assertEqual(plat.critical(os.path.join(self.d, "x")), plat.UNSURE)
            self.assertEqual(plat.tree_problem(self.d), plat.UNSURE)
        finally:
            plat.canonical = orig

    # ---- 2. recursive operations check every child
    def test_protected_child_inside_a_selected_folder(self):
        item = write(os.path.join(self.d, "elsewhere", "precious.bin"), b"keep")
        folder = os.path.join(self.d, "folder")
        write(os.path.join(folder, "plain.txt"), b"1")
        os.makedirs(os.path.join(folder, "sub"))
        os.link(item, os.path.join(folder, "sub", "hard.bin"))
        with protect(items=[item]):
            j = run("delete", sources=[folder], confirm=True)         # path rules don't see the alias inside
            self.assertTrue(any("test item" in e["message"] for e in j.errors), j.errors)
            self.assertFalse(os.path.exists(os.path.join(folder, "plain.txt")))
            self.assertTrue(os.path.exists(os.path.join(folder, "sub", "hard.bin")))
            dest = os.path.join(self.d, "dest")
            os.makedirs(dest)
            with other_disk():
                j = run("move", sources=[folder], dest=dest)
            self.assertTrue(any("test item" in e["message"] for e in j.errors), j.errors)
            self.assertTrue(os.path.exists(os.path.join(folder, "sub", "hard.bin")))   # the original stays
        self.assertEqual(read(item), b"keep")

    def test_merge_never_replaces_a_protected_child(self):
        item = write(os.path.join(self.d, "elsewhere", "settings.ini"), b"keep")
        dst = os.path.join(self.d, "dst", "app")
        os.makedirs(dst)
        os.link(item, os.path.join(dst, "settings.ini"))
        write(os.path.join(self.d, "src", "app", "settings.ini"), b"new")
        write(os.path.join(self.d, "src", "app", "other.txt"), b"2")
        with protect(items=[item]):
            j = run("copy", sources=[os.path.join(self.d, "src", "app")], dest=os.path.join(self.d, "dst"),
                    conflict="replace")
        self.assertTrue(any("test item" in e["message"] for e in j.errors), j.errors)
        self.assertEqual(read(os.path.join(dst, "settings.ini")), b"keep")
        self.assertEqual(read(os.path.join(dst, "other.txt")), b"2")

    def test_protection_added_after_planning_is_enforced(self):
        folder = os.path.join(self.d, "f")
        write(os.path.join(folder, "x.txt"), b"1")
        p = plan("trash", sources=[folder])
        self.assertEqual(len(p["items"]), 1)
        with protect(items=[os.path.join(folder, "x.txt")]):        # now f contains a protected item
            j = wait(fm.fm_start({"plan": p["plan"]})["job"])
        self.assertIn("contains", j.errors[0]["message"])
        self.assertTrue(os.path.exists(os.path.join(folder, "x.txt")))

    # ---- 3. undo validates like a forward operation
    def test_undo_refused_when_a_parent_was_swapped_for_a_junction(self):
        a, b = os.path.join(self.d, "a"), os.path.join(self.d, "b")
        f = write(os.path.join(a, "f.txt"), b"1")
        os.makedirs(b)
        j = run("move", sources=[f], dest=b)
        target = os.path.join(self.d, "elsewhere")
        os.makedirs(target)
        os.rmdir(a)
        make_junction(a, target)                                       # the original folder is now a link
        u = wait(fm.fm_undo({"journal": j.journal})["job"])
        self.assertTrue(u.errors)
        self.assertEqual(os.listdir(target), [])                       # nothing written through the junction
        self.assertTrue(os.path.exists(os.path.join(b, "f.txt")))
        entry = next(e for e in fm._journal_read() if e["id"] == j.journal)
        self.assertFalse(entry["undone"])                              # still retryable
        os.rmdir(a) if WIN else os.unlink(a)
        u = wait(fm.fm_undo({"journal": j.journal})["job"])           # fixed: the retry works
        self.assertEqual(u.errors, [])
        self.assertEqual(read(f), b"1")

    def test_undo_refuses_to_restore_into_a_protected_place(self):
        f = write(os.path.join(self.d, "a", "f.txt"), b"1")
        j = run("rename", sources=[f], name="g.txt")
        with protect(trees=[os.path.join(self.d, "a")]):
            u = wait(fm.fm_undo({"journal": j.journal})["job"])
        self.assertIn("test tree", u.errors[0]["message"])
        self.assertTrue(os.path.exists(os.path.join(self.d, "a", "g.txt")))

    def test_failed_undo_stays_retryable(self):
        f = write(os.path.join(self.d, "old.txt"), b"1")
        j = run("rename", sources=[f], name="new.txt")
        write(f, b"blocker")
        u = wait(fm.fm_undo({"journal": j.journal})["job"])
        self.assertTrue(u.errors)
        entry = next(e for e in fm._journal_read() if e["id"] == j.journal)
        self.assertFalse(entry["undone"])
        self.assertTrue(any(h["id"] == j.id and h["undo"] == j.journal for h in fm.fm_jobs({})["history"]))
        os.remove(f)
        u = wait(fm.fm_undo({"journal": j.journal})["job"])
        self.assertEqual(u.errors, [])
        self.assertEqual(read(f), b"1")
        self.assertIn("already", fm.fm_undo({"journal": j.journal})["error"])

    # ---- 4. multi-rename cancelled midway
    def _rename_files(self, n=4):
        files = [write(os.path.join(self.d, f"f{i}.txt"), str(i).encode()) for i in range(n)]
        return files, [[f, f"g{i}.txt"] for i, f in enumerate(files)]

    def _cancel_after(self, calls):
        """Cancel the running job after `calls` renames (staging or finishing)."""
        orig = fm.safefs.Dir.rename_to
        count = [0]

        def rename_to(d, *a, **kw):
            orig(d, *a, **kw)
            count[0] += 1
            if count[0] == calls:
                for j in fm.JOBS.values():
                    if j.state in ("running", "waiting", "paused"):
                        j.cancel = True
        fm.safefs.Dir.rename_to = rename_to
        return orig

    def test_multi_rename_cancelled_while_staging_restores_every_name(self):
        files, renames = self._rename_files()
        orig = self._cancel_after(2)
        try:
            j = run("rename_many", renames=renames)
        finally:
            fm.safefs.Dir.rename_to = orig
        self.assertEqual(j.state, "cancelled")
        self.assertEqual(sorted(os.listdir(self.d)), [f"f{i}.txt" for i in range(4)])
        self.assertFalse([r for r in fm._pending_read() if r.get("job") == j.id])

    def test_multi_rename_cancelled_while_finishing_leaves_no_temp_names(self):
        files, renames = self._rename_files()
        orig = self._cancel_after(5)                        # 4 staged, 1 finished
        try:
            j = run("rename_many", renames=renames)
        finally:
            fm.safefs.Dir.rename_to = orig
        self.assertEqual(j.state, "cancelled")
        self.assertEqual(sorted(os.listdir(self.d)), ["f1.txt", "f2.txt", "f3.txt", "g0.txt"])
        u = wait(fm.fm_undo({"journal": j.journal})["job"])            # the finished one is undoable
        self.assertEqual(u.errors, [])
        self.assertEqual(sorted(os.listdir(self.d)), [f"f{i}.txt" for i in range(4)])

    def test_recovery_after_a_crash(self):
        # a staged rename and a half-written copy left behind by a job that never finished
        write(os.path.join(self.d, ".hoard-deadbeef0001-0.renaming"), b"mine")
        write(os.path.join(self.d, "deep", ".hoard-deadbeef0001-7.part"), b"half")
        write(os.path.join(self.d, "deep", ".hoard-otherjob-1.part"), b"not ours")
        fm._pending_add({"job": "deadbeef0001", "kind": "rename",
                         "items": [[self.d, "mine.txt", ".hoard-deadbeef0001-0.renaming", "x.txt"]]})
        fm._pending_add({"job": "deadbeef0001", "kind": "parts", "dirs": [self.d]})
        os.makedirs(fm.RUNDIR, exist_ok=True)
        with open(os.path.join(fm.RUNDIR, "deadbeef0002.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"id": "deadbeef0002", "op": "move", "time": 1, "summary": "Moving 2 items"}\n')
            f.write('{"t": "move", "from": "a", "to": "b", "id": [1, 2, false]}\n')
        notes = fm.recover()
        self.assertEqual(read(os.path.join(self.d, "mine.txt")), b"mine")
        self.assertFalse(os.path.exists(os.path.join(self.d, "deep", ".hoard-deadbeef0001-7.part")))
        self.assertTrue(os.path.exists(os.path.join(self.d, "deep", ".hoard-otherjob-1.part")))
        self.assertTrue(any("interrupted" in n.lower() or "closed" in n for n in notes), notes)
        entry = next(e for e in fm._journal_read() if e["id"] == "deadbeef0002")
        self.assertEqual(len(entry["actions"]), 1)
        self.assertFalse(os.path.exists(os.path.join(fm.RUNDIR, "deadbeef0002.jsonl")))
        self.assertIn("notices", fm.fm_jobs({}))

    # ---- 5. cross-disk folder move cancelled midway
    def test_cross_disk_move_cancelled_midway_is_fully_undoable(self):
        src = os.path.join(self.d, "src", "album")
        for i in range(5):
            write(os.path.join(src, "sub" if i % 2 else "", f"p{i}.jpg"), bytes([i]) * 10)
        dest = os.path.join(self.d, "dest")
        os.makedirs(dest)
        orig = fm.Job.add_bytes

        def add_then_cancel(job, n):
            orig(job, n)
            if job.done_b >= 30:
                job.cancel = True
        fm.Job.add_bytes = add_then_cancel
        try:
            with other_disk():
                j = run("move", sources=[src], dest=dest)
        finally:
            fm.Job.add_bytes = orig
        self.assertEqual(j.state, "cancelled")
        self.assertTrue(j.journal)
        with other_disk():
            u = wait(fm.fm_undo({"journal": j.journal})["job"])
        self.assertEqual(u.errors, [])
        got = sorted(os.path.relpath(os.path.join(r, f), src) for r, _d, fs in os.walk(src) for f in fs)
        self.assertEqual(got, sorted(os.path.join("sub", f"p{i}.jpg") if i % 2 else f"p{i}.jpg" for i in range(5)))
        self.assertEqual(os.listdir(dest), [])

    # ---- 6. undo of a copied folder keeps edits and additions
    def test_undo_of_copied_folder_keeps_edited_and_added_files(self):
        src = os.path.join(self.d, "src", "proj")
        write(os.path.join(src, "a.txt"), b"a")
        write(os.path.join(src, "sub", "b.txt"), b"b")
        dest = os.path.join(self.d, "dest")
        os.makedirs(dest)
        j = run("copy", sources=[src], dest=dest)
        copy = os.path.join(dest, "proj")
        time.sleep(0.01)
        with open(os.path.join(copy, "sub", "b.txt"), "ab") as f:
            f.write(b" edited")
        write(os.path.join(copy, "notes.txt"), b"new work")
        u = wait(fm.fm_undo({"journal": j.journal})["job"])
        self.assertFalse(os.path.exists(os.path.join(copy, "a.txt")))          # unchanged copy: undone
        self.assertEqual(read(os.path.join(copy, "sub", "b.txt")), b"b edited")
        self.assertEqual(read(os.path.join(copy, "notes.txt")), b"new work")
        msgs = " ".join(e["message"] for e in u.errors)
        self.assertIn("changed", msgs)
        self.assertIn("added", msgs)

    def test_undo_of_untouched_copied_folder_removes_it_whole(self):
        src = os.path.join(self.d, "src", "proj")
        write(os.path.join(src, "sub", "b.txt"), b"b")
        dest = os.path.join(self.d, "dest")
        os.makedirs(dest)
        j = run("copy", sources=[src], dest=dest)
        u = wait(fm.fm_undo({"journal": j.journal})["job"])
        self.assertEqual(u.errors, [])
        self.assertEqual(os.listdir(dest), [])
        self.assertTrue(next(e for e in fm._journal_read() if e["id"] == j.journal)["undone"])

    # ---- 7. renames never replace
    def test_rename_noreplace(self):
        a = write(os.path.join(self.d, "a.txt"), b"a")
        b = write(os.path.join(self.d, "b.txt"), b"b")
        with self.assertRaises(FileExistsError):
            plat.rename_noreplace(a, b)
        os.makedirs(os.path.join(self.d, "d1"))
        os.makedirs(os.path.join(self.d, "d2"))
        with self.assertRaises(OSError):
            plat.rename_noreplace(os.path.join(self.d, "d1"), os.path.join(self.d, "d2"))
        self.assertEqual((read(a), read(b)), (b"a", b"b"))
        plat.rename_noreplace(a, os.path.join(self.d, "c.txt"))
        self.assertEqual(read(os.path.join(self.d, "c.txt")), b"a")

    def test_copy_never_overwrites_a_file_created_meanwhile(self):
        src = write(os.path.join(self.d, "src", "doc.txt"), b"copied")
        dest = os.path.join(self.d, "dest")
        os.makedirs(dest)
        orig = fm.Job.add_bytes

        def someone_saves(job, n):                     # another app saves doc.txt while Hoard copies
            orig(job, n)
            if n > 0 and not os.path.exists(os.path.join(dest, "doc.txt")):
                write(os.path.join(dest, "doc.txt"), b"newer work")
        fm.Job.add_bytes = someone_saves
        try:
            p = plan("copy", sources=[src], dest=dest)
            jid = fm.fm_start({"plan": p["plan"]})["job"]
            q = wait_question(jid)
            self.assertEqual(q["kind"], "error")
            fm.fm_decide({"job": jid, "answer": "skip"})
            wait(jid)
        finally:
            fm.Job.add_bytes = orig
        self.assertEqual(read(os.path.join(dest, "doc.txt")), b"newer work")
        self.assertEqual(os.listdir(dest), ["doc.txt"])               # and no .part left

    # ---- 8./10. reads never go through links
    def test_reads_through_a_linked_ancestor_are_refused(self):
        real = os.path.join(self.d, "real")
        write(os.path.join(real, "secret.txt"), b"s")
        link = os.path.join(self.d, "link")
        make_junction(link, real)
        self.assertNotIn("error", fm.fm_read({"path": os.path.join(real, "secret.txt")}))
        r = fm.fm_read({"path": os.path.join(link, "secret.txt")})
        self.assertIn("link", r["error"])
        with self.assertRaises(fm.FmError):
            fm.open_for_read(os.path.join(link, "secret.txt"))

    # ---- 9. busy() covers paused and waiting jobs
    def test_busy_while_waiting_or_paused(self):
        self.assertFalse(fm.busy())
        src = write(os.path.join(self.d, "src", "a.txt"), b"new")
        write(os.path.join(self.d, "dst", "a.txt"), b"old")
        p = plan("copy", sources=[src], dest=os.path.join(self.d, "dst"))
        jid = fm.fm_start({"plan": p["plan"]})["job"]
        wait_question(jid)
        self.assertEqual(fm.JOBS[jid].state, "waiting")
        self.assertTrue(fm.busy())
        fm.fm_control({"job": jid, "action": "pause"})
        self.assertTrue(fm.busy())
        fm.fm_control({"job": jid, "action": "resume"})
        fm.fm_decide({"job": jid, "answer": "skip"})
        wait(jid)
        self.assertFalse(fm.busy())


class Server(unittest.TestCase):
    def test_endpoints_and_assets_are_wired(self):
        for name in ("roots", "list", "check", "plan", "start", "jobs", "control", "decide", "undo", "sizes",
                     "find", "read", "prefs"):
            self.assertTrue(callable(getattr(server.Handler, "api_fm_" + name)), name)
        self.assertEqual(server.Handler.ASSET_TYPES["fm.js"], "text/javascript; charset=utf-8")
        self.assertEqual(server.Handler.ASSET_TYPES["fm.css"], "text/css; charset=utf-8")
        r = server.Handler.api_fm_roots(None, {})
        self.assertEqual(r["sep"], os.sep)
        self.assertTrue(r["roots"])

    def test_fmfile_streams_plain_files_only(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        d = tempfile.mkdtemp(dir=TMP)
        f = write(os.path.join(d, "v.txt"), b"0123456789")
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}/fmfile?t={server.TOKEN}&p="
        from urllib.parse import quote
        try:
            req = urllib.request.Request(base + quote(f), headers={"Range": "bytes=2-4"})
            with urllib.request.urlopen(req) as r:
                self.assertEqual((r.status, r.read()), (206, b"234"))
                self.assertEqual(r.headers["Content-Security-Policy"], "sandbox")
            for bad in (d, os.path.join(d, "missing")):
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(base + quote(bad))
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base.replace(server.TOKEN, "wrong") + quote(f))
        finally:
            srv.shutdown()
            srv.server_close()


def tearDownModule():
    # stop anything still running; the temp folder stays (other test modules may share its data folder)
    for j in list(fm.JOBS.values()):
        if j.state in ("queued", "running", "paused", "waiting"):
            j.cancel = True


if __name__ == "__main__":
    unittest.main()
