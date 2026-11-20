"""Unit tests for Hoard's risky parts: paths, deletion guards, cleanup, staging, snapshots, comparisons,
duplicates, cloud listings, updates and the uninstaller.  Run:  python -m unittest discover -s tests -v
Uses a temporary data folder, so real saved scans and settings are never touched."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

TMP = tempfile.mkdtemp(prefix="hoard-tests-")
os.environ["HOARD_DATA_DIR"] = os.path.join(TMP, "data")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hoard"))

import cleanup  # noqa: E402
import cloud  # noqa: E402
import core  # noqa: E402
import dupes  # noqa: E402
import plat  # noqa: E402
import reports  # noqa: E402
import server  # noqa: E402
import updates  # noqa: E402
import apps  # noqa: E402

MB = 1 << 20


def write(path, size, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(size))
    if mtime:
        os.utime(path, (mtime, mtime))


def tree(spec, name="root"):
    """Build a Node tree: {"dir": {...}, "file.ext": size}."""
    n = core.Node(name)
    for k, v in spec.items():
        if isinstance(v, dict):
            c = tree(v, k)
            n.dirs.append(c)
            n.size += c.size
            n.nfiles += c.nfiles
        else:
            cat = core.cat_of(k)
            n.files.append((k, v, cat, 1_700_000_000))
            n.size += v
            n.nfiles += 1
            n.cats[cat] += v
    return n


class Paths(unittest.TestCase):
    def test_clean_parts_accepts_normal_names(self):
        self.assertEqual(server.clean_parts(["Users", "me", "file name (1).txt"]), ["Users", "me", "file name (1).txt"])

    def test_clean_parts_rejects_escapes(self):
        for bad in (["..", "Windows"], ["a", "."], ["a/b"], ["a\\b"], [""], ["x\0"], "notalist", [3]):
            with self.assertRaises(ValueError, msg=bad):
                server.clean_parts(bad)

    @unittest.skipUnless(sys.platform == "win32", "Windows rules")
    def test_windows_streams_rejected(self):
        with self.assertRaises(ValueError):
            server.clean_parts(["file.txt:hidden"])

    @unittest.skipUnless(sys.platform == "win32", "Windows rules")
    def test_windows_protected(self):
        sysd = os.environ.get("SystemDrive", "C:") + "\\"
        P = plat.protected
        self.assertTrue(P(sysd, ["Users"], sysd + "Users"))                                  # top level of C:
        self.assertTrue(P(sysd, ["Windows", "System32"], sysd + "Windows\\System32"))       # Windows folder
        home = os.path.expanduser("~")
        parts = home[3:].split("\\")
        self.assertTrue(P(sysd, parts, home))                                               # the profile itself
        self.assertTrue(P(sysd, parts + ["AppData"], home + "\\AppData"))
        self.assertIsNone(P(sysd, parts + ["Downloads", "x.zip"], home + "\\Downloads\\x.zip"))
        other = "Q:\\" if sysd.upper() != "Q:\\" else "R:\\"
        self.assertIsNone(P(other, ["Games"], other + "Games"))                            # top level of a data drive
        self.assertTrue(P(other, [], other))
        self.assertTrue(P(other, ["System Volume Information"], other + "System Volume Information"))


class Cleanup(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(dir=TMP)

    def test_too_broad(self):
        self.assertTrue(cleanup._too_broad(os.path.expanduser("~")))
        self.assertTrue(cleanup._too_broad(os.path.abspath(os.sep)))
        if os.environ.get("LOCALAPPDATA"):
            self.assertTrue(cleanup._too_broad(os.environ["LOCALAPPDATA"]))
        self.assertFalse(cleanup._too_broad(os.path.join(self.d, "cache")))

    def test_clear_keeps_root_and_new_files(self):
        old = time.time() - 10 * 86400
        write(os.path.join(self.d, "c", "old.tmp"), 1000, old)
        write(os.path.join(self.d, "c", "sub", "old2.tmp"), 2000, old)
        write(os.path.join(self.d, "c", "new.tmp"), 3000)
        st = cleanup.clear([os.path.join(self.d, "c")], older=time.time() - 86400)
        self.assertEqual(st["files"], 2)
        self.assertTrue(os.path.isdir(os.path.join(self.d, "c")))            # the folder itself stays
        self.assertTrue(os.path.exists(os.path.join(self.d, "c", "new.tmp")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "c", "sub")))    # emptied subfolder removed

    def test_clear_refuses_home(self):
        st = cleanup.clear([os.path.expanduser("~")])
        self.assertEqual(st["files"], 0)
        self.assertEqual(st["skipped"], 1)

    @unittest.skipIf(sys.platform == "win32", "symlinks need privileges on Windows")
    def test_clear_never_follows_links(self):
        outside = os.path.join(self.d, "outside")
        write(os.path.join(outside, "precious.txt"), 10)
        os.makedirs(os.path.join(self.d, "c2"))
        os.symlink(outside, os.path.join(self.d, "c2", "link"))
        cleanup.clear([os.path.join(self.d, "c2")])
        self.assertTrue(os.path.exists(os.path.join(outside, "precious.txt")))

    def test_expand_dedupes_nested(self):
        os.makedirs(os.path.join(self.d, "a", "b"))
        got = cleanup.expand([os.path.join(self.d, "a"), os.path.join(self.d, "a", "b"), os.path.join(self.d, "nope")])
        self.assertEqual(got, [os.path.normpath(os.path.join(self.d, "a"))])

    def test_run_rejects_unknown_ids(self):
        self.assertIn("error", cleanup.run("../../etc"))

    def test_node_modules_finder(self):
        old = int(time.time()) - 200 * 86400
        proj = tree({"proj": {"node_modules": {"big.js": 30 * MB}, "index.js": 10}})
        p = proj.child("proj")
        p.own, p.child("node_modules").mtime = old, int(time.time())
        found, rescan = cleanup.find_node_modules([(self.d, proj)])
        self.assertEqual([f["name"] for f in found], ["proj/node_modules"])
        p.own = int(time.time())                                     # project touched recently -> not suggested
        self.assertEqual(cleanup.find_node_modules([(self.d, proj)])[0], [])


class Snapshots(unittest.TestCase):
    def test_roundtrip_and_legacy(self):
        t = tree({"a": {"x.mp4": 5 * MB}, "y.txt": 10})
        t.cloud, t.mtime = 123, 456
        back = core._dec(json.loads(json.dumps(core._enc(t))))
        self.assertEqual((back.size, back.cloud, back.mtime), (t.size, 123, 456))
        legacy = ["r", 10, 1, [0] * 9, 0, 0, [["f.bin", 10, 8]], []]   # version 1: no dates, 3-field files
        n = core._dec(legacy)
        self.assertEqual(n.files, [("f.bin", 10, 8, 0)])
        self.assertEqual(n.mtime, 0)

    def test_new_ts_always_increases(self):
        vid = "unit-ts"
        t = tree({"f": 1})
        a = core.new_ts(vid)
        core.save_snapshot(vid, t, a)
        b = core.new_ts(vid)
        self.assertGreater(b, a)
        core.save_snapshot(vid, t, b)
        core.prune(vid, 1)                    # never fewer than 2 kept
        self.assertEqual(len(core.list_snaps(vid)), 2)

    def test_bin_is_not_ai_model(self):
        self.assertEqual(core.cat_of("data.bin"), core.OTHER)
        self.assertEqual(core.cat_of("model.safetensors"), 7)


class Changes(unittest.TestCase):
    def test_growth_points_at_the_cause(self):
        prev = tree({"Users": {"me": {"Downloads": {"a.zip": 10 * MB}}}})
        cur = tree({"Users": {"me": {"Downloads": {"a.zip": 10 * MB, "big.iso": 900 * MB}}}})
        s = {"cur": cur, "prev": prev, "prev_partial": False, "changes": None}
        ch = server.all_changes(s)
        self.assertEqual(ch[0][1], ("Users", "me", "Downloads", "big.iso"))     # the file, not every parent
        self.assertNotIn(("Users",), [c[1] for c in ch])

    def test_view_folds_small_and_ignored(self):
        t = tree({"big": {"x.mp4": 100 * MB}, "tiny": {"t.txt": 10}, "skip": {"s.mp4": 50 * MB}})
        v = server.view_json(t, None, False, 2, 1 * MB, (), {("skip",)})
        names = [c["n"] for c in v["ch"]]
        self.assertIn("big", names)
        self.assertNotIn("skip", names)
        self.assertEqual(v["ign"], 50 * MB)
        self.assertTrue(any(c["k"] == "o" for c in v["ch"]))       # "tiny" folded into "smaller items"


def add_vol(path, legacy=None):
    """Register a folder as a volume, the way an added location is (without touching real settings)."""
    os.makedirs(path, exist_ok=True)
    v = server.location_volume(path)
    if legacy is not None:
        v["legacy"] = legacy
    server.VOLS[v["id"]] = v
    server.STATE.pop(v["id"], None)
    return v


class Staging(unittest.TestCase):
    def setUp(self):
        core.write_staged([])
        self.root = tempfile.mkdtemp(dir=TMP)
        self.v = add_vol(self.root)
        write(os.path.join(self.root, "a", "b", "c.txt"), 10)
        write(os.path.join(self.root, "a", "z"), 10)

    def test_parent_replaces_children_and_children_are_skipped(self):
        vid = self.v["id"]
        server.staged_add([{"vol": vid, "path": ["a", "b", "c.txt"], "size": 1}])
        server.staged_add([{"vol": vid, "path": ["a"], "size": 5}])
        self.assertEqual([i["path"] for i in core.read_staged()], [["a"]])
        self.assertEqual(server.staged_add([{"vol": vid, "path": ["a", "z"], "size": 1}]), 0)

    def test_bad_paths_never_staged(self):
        self.assertEqual(server.staged_add([{"vol": self.v["id"], "path": ["..", "Windows"], "size": 1}]), 0)
        self.assertEqual(core.read_staged(), [])

    def test_missing_items_and_unknown_volumes_never_staged(self):
        self.assertEqual(server.staged_add([{"vol": self.v["id"], "path": ["nope.txt"], "size": 1}]), 0)
        self.assertEqual(server.staged_add([{"vol": "no-such-volume", "path": ["a"], "size": 1}]), 0)


def make_junction(link, target):
    """A directory link: a junction on Windows (no admin needed), a symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(target, link)
    else:
        os.symlink(target, link)


class Safety(unittest.TestCase):
    """The review findings about deleting the wrong thing."""

    def setUp(self):
        core.write_staged([])
        self.root = tempfile.mkdtemp(dir=TMP)
        self.v = add_vol(self.root)

    def test_volume_ids_never_collide(self):
        a, b = os.path.normcase(r"D:\archive one"), os.path.normcase(r"D:\archive-one")
        self.assertEqual(plat.legacy_vol_id("dir", a), plat.legacy_vol_id("dir", b))    # the old bug
        self.assertNotEqual(plat.vol_id("dir", a), plat.vol_id("dir", b))
        self.assertNotEqual(plat.vol_id("cloud", "team drive"), plat.vol_id("cloud", "team-drive"))
        long = "x" * 80
        self.assertNotEqual(plat.vol_id("dir", long + "1"), plat.vol_id("dir", long + "2"))

    def test_migration_moves_scans_only_to_their_real_owner(self):
        one, two = os.path.join(TMP, "mig", "archive one"), os.path.join(TMP, "mig", "archive-one")
        legacy = "dir-legacy-shared"
        va, vb = add_vol(one, legacy), add_vol(two, legacy)
        core.save_snapshot(legacy, tree({"photos": {"a.jpg": 5}}), 1000, root=one)
        s = core.load_settings()
        s["schedule"]["volumes"] = [legacy]
        core.save_settings(s)
        moved = core.migrate_ids([va, vb])
        self.assertEqual(moved, {legacy: va["id"]})                 # the scan says it came from "archive one"
        self.assertEqual(core.list_snaps(va["id"]), [1000])
        self.assertEqual(core.list_snaps(vb["id"]), [])
        self.assertEqual(core.load_settings()["schedule"]["volumes"], [va["id"]])
        # two claimants and no recorded root: nobody gets it
        core.save_snapshot("dir-legacy-2", tree({"x.bin": 5}), 1000)
        vc, vd = add_vol(os.path.join(TMP, "mig", "c"), "dir-legacy-2"), add_vol(os.path.join(TMP, "mig", "d"), "dir-legacy-2")
        self.assertEqual(core.migrate_ids([vc, vd]), {})
        self.assertEqual(core.list_snaps("dir-legacy-2"), [1000])

    def test_removal_refused_when_the_scan_is_of_another_folder(self):
        write(os.path.join(self.root, "photos", "a.jpg"), 10)
        core.save_snapshot(self.v["id"], tree({"photos": {"a.jpg": 10}}), 1000, root=os.path.join(TMP, "elsewhere"))
        server.STATE.pop(self.v["id"], None)
        ok, err = server.trash_any(self.v["id"], ["photos", "a.jpg"])
        self.assertFalse(ok)
        self.assertIn("different folder", err)
        self.assertTrue(os.path.exists(os.path.join(self.root, "photos", "a.jpg")))

    def test_old_unverifiable_scans_need_a_rescan_before_removing(self):
        v = add_vol(tempfile.mkdtemp(dir=TMP), legacy="dir-old")
        write(os.path.join(v["root"], "a.bin"), 10)
        core.save_snapshot(v["id"], tree({"a.bin": 10}), 1000)          # no root recorded (older version)
        ok, err = server.trash_any(v["id"], ["a.bin"])
        self.assertFalse(ok)
        self.assertIn("again", err)

    def test_marked_item_replaced_by_something_else_is_left_alone(self):
        p = os.path.join(self.root, "installer.iso")
        write(p, 10)
        self.assertEqual(server.staged_add([{"vol": self.v["id"], "path": ["installer.iso"], "size": 10}]), 1)
        os.remove(p)
        os.makedirs(p)                                     # a folder with unrelated data now has that name
        write(os.path.join(p, "precious.doc"), 10)
        r = server.staged_trash()
        self.assertEqual(r["done"], 0)
        self.assertIn("changed since you marked it", r["failed"][0]["error"])
        self.assertTrue(os.path.exists(os.path.join(p, "precious.doc")))

    def test_marked_file_replaced_by_another_file_is_left_alone(self):
        p = os.path.join(self.root, "video.mp4")
        write(p, 10)
        server.staged_add([{"vol": self.v["id"], "path": ["video.mp4"], "size": 10}])
        keep = os.path.join(self.root, "keep.tmp")
        write(keep, 10)                                    # hold the old file id so it can't be reused
        os.remove(p)
        write(p, 20)
        r = server.staged_trash()
        self.assertEqual(r["done"], 0)
        self.assertTrue(os.path.exists(p))

    def test_never_removes_or_previews_through_a_link(self):
        outside = tempfile.mkdtemp(dir=TMP)
        write(os.path.join(outside, "large.bin"), 10)
        os.makedirs(os.path.join(self.root, "cache"))
        write(os.path.join(self.root, "cache", "large.bin"), 10)
        shutil.rmtree(os.path.join(self.root, "cache"))
        make_junction(os.path.join(self.root, "cache"), outside)       # swapped after the scan
        self.assertTrue(plat.linked_ancestor(self.root, ["cache", "large.bin"]))
        ok, err = server.trash_any(self.v["id"], ["cache", "large.bin"])
        self.assertFalse(ok)
        self.assertIn("link", err)
        self.assertTrue(os.path.exists(os.path.join(outside, "large.bin")))

    def test_pinned_folder_cant_be_swapped_and_links_arent_pinned(self):
        d = os.path.join(self.root, "pinme")
        os.makedirs(d)
        link = os.path.join(self.root, "alink")
        make_junction(link, d)
        self.assertIsNone(plat.pin_dir(link))
        self.assertIsNone(plat.pin_dir(d, expect=12345))
        tok = plat.pin_dir(d)
        self.assertIsNotNone(tok)
        try:
            if sys.platform == "win32":
                with self.assertRaises(OSError):
                    os.rename(d, d + "-moved")                # held open: can't be renamed or replaced
        finally:
            plat.unpin_dir(tok)
        os.rename(d, d + "-moved")

    def test_cleanup_still_clears_and_skips_links(self):
        c = os.path.join(self.root, "Cache")
        write(os.path.join(c, "sub", "old.tmp"), 10)
        outside = tempfile.mkdtemp(dir=TMP)
        write(os.path.join(outside, "keep.txt"), 10)
        make_junction(os.path.join(c, "link"), outside)
        st = cleanup.clear([c])
        self.assertEqual(st["files"], 1)
        self.assertFalse(os.path.exists(os.path.join(c, "sub")))
        self.assertTrue(os.path.exists(os.path.join(outside, "keep.txt")))

    def test_simple_mode_only_suggests_your_own_files(self):
        root = "C:\\" if sys.platform == "win32" else "/"
        managed = [["$Recycle.Bin", "S-1-5", "$RX.bin"], ["Program Files", "Epic Games", "a.pak"],
                   ["Users", "me", "AppData", "Local", "Google", "Chrome", "weights.bin"],
                   ["Games", "steamapps", "common", "x.pak"], ["Users", "me", ".cache", "big.bin"], ["pagefile.sys"]]
        for parts in managed:
            self.assertTrue(plat.managed(root, parts), parts)
        self.assertFalse(plat.managed(root, ["Users", "me", "Videos", "old holiday.mkv"]))
        self.assertFalse(plat.managed(root, ["Users", "me", "Downloads", "setup.iso"]))

    def test_personal_search_hides_managed_and_placeholder_dates(self):
        n = core.Node("root")
        old = time.time() - 400 * 86400
        for d, f, mt in (("Videos", "old.mkv", old), ("AppData", "weights.bin", old), ("Downloads", "ancient.iso", 315532800)):
            c = core.Node(d)
            c.files.append((f, 900 * MB, core.cat_of(f), mt))
            c.size = 900 * MB
            n.dirs.append(c)
            n.size += c.size
        # a data folder outside any system location (the test's temp folder is inside AppData, which is managed)
        root = "E:\\Data" if sys.platform == "win32" else "/srv/data"
        v = dict(server.location_volume(TMP), id="dir-personal-test", root=root)
        server.VOLS[v["id"]] = v
        core.save_snapshot(v["id"], n, 2000, root=root)
        b = {"drive": v["id"], "path": [], "kind": "file", "olderDays": 365, "minSize": 500 * MB}
        everything = server.Handler.api_search(None, b)["items"]
        mine = server.Handler.api_search(None, dict(b, personal=True))["items"]
        self.assertEqual(len(everything), 3)
        self.assertEqual([i["p"] for i in mine], [["Videos", "old.mkv"]])

    def test_concurrent_saves_get_their_own_timestamps(self):
        import threading
        vid = "dir-concurrent"
        out = []
        ts = [threading.Thread(target=lambda: out.append(core.commit_scan(vid, tree({"a.bin": 5}), "/x", 30))) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(set(out)), 4)
        self.assertEqual(sorted(core.list_snaps(vid)), sorted(out))
        for t in out:
            tr, meta = core.load_snapshot(vid, t)
            self.assertEqual(meta["root"], "/x")
        self.assertFalse([n for n in os.listdir(core.vol_dir(vid)) if n.endswith(".tmp")])

    def test_update_only_installs_the_app_itself(self):
        assets = [{"name": "hoard-mft.exe", "browser_download_url": "https://github.com/a"},
                  {"name": "Hoard.exe.sha256", "browser_download_url": "https://github.com/b"},
                  {"name": "Hoard.exe", "browser_download_url": "https://github.com/c"},
                  {"name": "Hoard-1.2.0-macos-arm64.dmg", "browser_download_url": "https://github.com/d"},
                  {"name": "Hoard-1.2.0-macos-x86_64.dmg", "browser_download_url": "https://github.com/e"},
                  {"name": "Hoard-1.2.0-x86_64.AppImage", "browser_download_url": "https://github.com/f"}]
        name, _ = updates._pick_asset(assets)
        want = {"windows": "Hoard.exe", "linux": "Hoard-1.2.0-x86_64.AppImage"}.get(plat.NAME)
        if want:
            self.assertEqual(name, want)
        else:
            self.assertTrue(name.startswith("Hoard-1.2.0-macos-"))
        self.assertEqual(updates._pick_asset([{"name": "hoard-mft.exe", "browser_download_url": "x"}]), (None, None))


class Duplicates(unittest.TestCase):
    def test_finds_identical_not_same_size(self):
        d = tempfile.mkdtemp(dir=TMP)
        data = os.urandom(3 * MB)
        for n in ("a.bin", "b.bin"):
            with open(os.path.join(d, n), "wb") as f:
                f.write(data)
        with open(os.path.join(d, "c.bin"), "wb") as f:            # same size, different bytes
            f.write(os.urandom(3 * MB))
        files = [{"path": os.path.join(d, n), "p": [n], "size": 3 * MB, "m": 0, "c": 8} for n in ("a.bin", "b.bin", "c.bin")]
        job = {"running": True, "phase": "", "done": 0, "total": 0, "groups": None, "cancel": False,
               "cancelled": False, "error": None}
        dupes.run(job, files)
        self.assertEqual(len(job["groups"]), 1)
        self.assertEqual(sorted(f["p"][0] for f in job["groups"][0]["files"]), ["a.bin", "b.bin"])


class Cloud(unittest.TestCase):
    def test_lsjson_parser(self):
        lines = "\n".join([
            "[",
            '{"Path":"Photos","Name":"Photos","Size":-1,"ModTime":"2024-01-01T10:00:00Z","IsDir":true},',
            '{"Path":"Photos/trip.mp4","Name":"trip.mp4","Size":3000000000,"ModTime":"2024-05-31T16:15:57.034468261+01:00","IsDir":false},',
            '{"Path":"Docs/Plan","Name":"Plan","Size":-1,"ModTime":"2025-01-01T00:00:00Z","IsDir":false},',
            '{"Path":"Docs/sub/n.txt","Name":"n.txt","Size":500,"ModTime":"2025-01-01T00:00:00Z","IsDir":false}',
            "]"])

        class Fake:
            def __init__(self, *a, **k):
                self.stdout, self.stderr, self.returncode = io.StringIO(lines), io.StringIO(""), 0

            def wait(self):
                return 0

            def kill(self):
                pass
        real_popen, real_exe = subprocess.Popen, cloud.exe
        subprocess.Popen, cloud.exe = Fake, (lambda: "rclone")
        try:
            t = cloud.scan_remote({"root": "g:", "name": "Google Drive"}, core.Scan("x", 0))
        finally:
            subprocess.Popen, cloud.exe = real_popen, real_exe
        self.assertEqual(t.size, 3_000_000_500)
        self.assertEqual(t.child("Photos").files[0][3], 1717168557)
        self.assertEqual(t.child("Docs").child("sub").size, 500)

    def test_untrashable_cloud_refuses(self):
        ok, err = cloud.trash({"trashable": False, "name": "S3"}, ["x"], False)
        self.assertFalse(ok)


class Updates(unittest.TestCase):
    def test_version_compare(self):
        self.assertGreater(updates._ver("v1.10.0"), updates._ver("1.9.9"))
        self.assertEqual(updates._ver("1.1"), (1, 1, 0))

    def test_only_github_https_trusted(self):
        self.assertTrue(updates._trusted("https://github.com/flamrot/hoard/releases/download/v1/Hoard.exe"))
        self.assertFalse(updates._trusted("http://github.com/x"))
        self.assertFalse(updates._trusted("https://github.com.evil.example/x"))
        self.assertFalse(updates._trusted(None))

    def test_apply_refuses_without_checksum(self):
        with open(updates.CACHE, "w") as f:
            json.dump({"newer": True, "latest": "9.9.9", "asset": "https://github.com/x/Hoard.exe", "checksum": None}, f)
        real = getattr(sys, "frozen", None)
        sys.frozen = True
        try:
            if plat.NAME == "windows":
                self.assertIn("checksum", updates.apply().get("error", ""))
        finally:
            if real is None:
                del sys.frozen

    def test_uninstall_only_touches_hoard_folders(self):
        self.assertFalse(updates.safe_to_remove(os.path.expanduser("~")))
        self.assertFalse(updates.safe_to_remove("Programs\\Hoard"))                 # relative
        self.assertFalse(updates.safe_to_remove(os.path.abspath(os.sep) + "Hoard"))  # too shallow
        deep = os.path.join(TMP, "a", "b", "Hoard")
        os.makedirs(deep, exist_ok=True)
        self.assertTrue(updates.safe_to_remove(deep))


class Automation(unittest.TestCase):
    def test_low_space_rule(self):
        import automation
        GB = 1 << 30
        rule = {"enabled": True, "percent": 10, "gb": 20}
        self.assertTrue(automation.low_space(500 * GB, 30 * GB, rule))     # 6 % free
        self.assertTrue(automation.low_space(100 * GB, 15 * GB, rule))     # 15 % but under 20 GB
        self.assertFalse(automation.low_space(500 * GB, 200 * GB, rule))
        self.assertFalse(automation.low_space(500 * GB, 1 * GB, {"enabled": False}))

    def test_auto_clean_due_and_only_safe_targets(self):
        import automation
        now = time.time()
        self.assertFalse(automation.auto_clean_due({"enabled": False, "last": 0}, now))
        self.assertTrue(automation.auto_clean_due({"enabled": True, "every": "week", "last": now - 8 * 86400}, now))
        self.assertFalse(automation.auto_clean_due({"enabled": True, "every": "week", "last": now - 2 * 86400}, now))
        self.assertTrue(automation.auto_clean_due({"enabled": True, "every": "day", "last": now - 86400}, now))
        ids = {s["id"] for s in automation.auto_clean_choices()}
        self.assertNotIn("bin", ids)            # never empties the Recycle Bin on its own
        self.assertNotIn("downloads", ids)      # never touches your own files
        self.assertNotIn("node_modules", ids)

    def test_low_space_notifies_once_a_day(self):
        import automation
        sent = []
        vols = [{"id": "T", "name": "T:", "type": "fixed", "root": TMP}]
        rule = {"lowSpace": {"enabled": True, "percent": 100, "gb": 0}}      # always "full"
        t0 = 1_800_000_000.0
        automation.check_low_space(vols, rule, now=t0, notify=lambda t, b: sent.append(t))
        automation.check_low_space(vols, rule, now=t0 + 3600, notify=lambda t, b: sent.append(t))   # an hour later: quiet
        self.assertEqual(len(sent), 1)
        automation.check_low_space(vols, rule, now=t0 + 25 * 3600, notify=lambda t, b: sent.append(t))  # next day: again
        self.assertEqual(len(sent), 2)


class Exports(unittest.TestCase):
    JPEG = bytes.fromhex("ffd8ffc000110800100020") + b"\x03" + b"\x00" * 9 + bytes.fromhex("ffd9")

    def test_jpeg_size(self):
        self.assertEqual(reports.jpeg_size(self.JPEG), (32, 16))

    def test_pdf_is_well_formed(self):
        rows = [(f"C:\\Users\\me\\folder {i} (copy)", "1.2 GB") for i in range(120)]       # forces a 2nd page
        pdf = reports.report_pdf("Hoard: C:", "273 GB · scanned today", [("Largest folders", rows), ("Empty", [])], self.JPEG)
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"/DCTDecode", pdf)
        self.assertGreaterEqual(pdf.count(b"/Type /Page "), 2)
        start = int(pdf.rsplit(b"startxref", 1)[1].split()[0])
        self.assertTrue(pdf[start:].startswith(b"xref"))


class CloudDupes(unittest.TestCase):
    def test_groups_by_size_and_checksum(self):
        lines = "\n".join([
            "[",
            '{"Path":"a/x.mp4","Size":5000000,"ModTime":"2024-01-01T00:00:00Z","Hashes":{"md5":"aa"}},',
            '{"Path":"b/x copy.mp4","Size":5000000,"ModTime":"2024-02-01T00:00:00Z","Hashes":{"md5":"aa"}},',
            '{"Path":"c/other.mp4","Size":5000000,"ModTime":"2024-01-01T00:00:00Z","Hashes":{"md5":"bb"}},',
            '{"Path":"d/small.txt","Size":10,"ModTime":"2024-01-01T00:00:00Z","Hashes":{"md5":"cc"}}',
            "]"])

        class Fake:
            def __init__(self, *a, **k):
                self.stdout, self.stderr, self.returncode = io.StringIO(lines), io.StringIO(""), 0

            def wait(self):
                return 0

            def kill(self):
                pass
        real_popen, real_exe = subprocess.Popen, cloud.exe
        subprocess.Popen, cloud.exe = Fake, (lambda: "rclone")
        try:
            job = cloud.dupes_start({"id": "cloud-g", "root": "g:"}, [], 1 << 20)
            for _ in range(100):
                if not job["running"]:
                    break
                time.sleep(0.02)
        finally:
            subprocess.Popen, cloud.exe = real_popen, real_exe
        self.assertIsNone(job["error"])
        self.assertEqual(len(job["groups"]), 1)
        self.assertEqual([f["p"] for f in job["groups"][0]["files"]], [["a", "x.mp4"], ["b", "x copy.mp4"]])
        self.assertEqual(job["groups"][0]["files"][0]["path"], "g:a/x.mp4")


class Desktop(unittest.TestCase):
    def test_tray_tooltip(self):
        import automation
        vols = [{"id": "C", "name": "C:", "root": os.environ.get("SystemDrive", "C:") + "\\" if sys.platform == "win32" else "/"}]
        tip = automation.tooltip_text(vols, [{"vol": "C", "time": time.time(), "delta": 2 << 30}])
        self.assertTrue(tip.startswith("Hoard"))
        self.assertIn("+2.0 GB today", tip)
        self.assertLessEqual(len(tip), 127)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux file-manager scripts")
    def test_linux_context_menu(self):
        os.environ["XDG_DATA_HOME"] = os.path.join(TMP, "xdg")
        os.environ["HOME_SAVE"] = os.environ.get("HOME", "")
        ok, err = plat.context_menu_install(["/opt/hoard/hoard"])
        self.assertTrue(ok, err)
        self.assertTrue(plat.context_menu_status())
        with open(os.path.join(TMP, "xdg", "nautilus", "scripts", "Open in Hoard")) as f:
            self.assertIn("--open", f.read())
        plat.context_menu_remove()


class Misc(unittest.TestCase):
    def test_csv_formula_neutralised(self):
        self.assertEqual(reports.safe_cell("=HYPERLINK(1)"), "'=HYPERLINK(1)")
        self.assertEqual(reports.safe_cell(-5), -5)

    def test_app_folder_matching(self):
        app = {"name": "Visual Studio Code", "publisher": "Microsoft Corporation"}
        toks = apps.tokens(app)
        self.assertTrue(apps.matches("Code", {"code"} | toks, app) or apps.matches("visualstudiocode", toks, app))
        self.assertFalse(apps.matches("Temp", toks, app))


if __name__ == "__main__":
    unittest.main()
