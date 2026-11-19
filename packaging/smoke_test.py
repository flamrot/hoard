"""End-to-end smoke test for a Hoard build (or the source).

    python packaging/smoke_test.py dist/Hoard.exe
    python packaging/smoke_test.py dist/Hoard.app/Contents/MacOS/Hoard --trash
    python packaging/smoke_test.py python3 hoard/server.py

Starts the app headless on a temporary folder, scans it twice (checking the "what grew" comparison),
reads the views, and with --trash moves a test file to the Trash/Recycle Bin.
Uses its own data folder, so real saved scans are not touched.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

MB = 1 << 20


def fail(msg):
    print("SMOKE TEST FAILED:", msg)
    sys.exit(1)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        chunk = os.urandom(MB)
        for _ in range(size // MB):
            f.write(chunk)
        f.write(os.urandom(size % MB))


def main():
    args = [a for a in sys.argv[1:] if a != "--trash"]
    do_trash = "--trash" in sys.argv
    if not args:
        fail("usage: smoke_test.py <app command...> [--trash]")
    if os.path.isfile(args[0]):
        args[0] = os.path.abspath(args[0])      # Windows won't start "dist/Hoard.exe" written with a forward slash

    tmp = tempfile.mkdtemp(prefix="hoard-smoke-")
    target, data = os.path.join(tmp, "target"), os.path.join(tmp, "data")
    os.makedirs(data)
    write(os.path.join(target, "sub", "big.bin"), 3 * MB)
    write(os.path.join(target, "sub", "clip.mp4"), 2 * MB)
    write(os.path.join(target, "sub", "notes.txt"), 2000)
    write(os.path.join(target, "models", "model.safetensors"), int(1.5 * MB))
    os.makedirs(os.path.join(target, "copies"), exist_ok=True)
    shutil.copyfile(os.path.join(target, "sub", "clip.mp4"), os.path.join(target, "copies", "clip copy.mp4"))
    old = time.time() - 400 * 86400                      # one file "not touched in over a year"
    os.utime(os.path.join(target, "models", "model.safetensors"), (old, old))
    exports = os.path.join(tmp, "exports")

    port = free_port()
    env = dict(os.environ, HOARD_SCAN_DIR=target, HOARD_DATA_DIR=data, HOARD_EXPORT_DIR=exports)
    proc = subprocess.Popen(args + ["--no-window", "--port", str(port)], env=env)
    base = f"http://127.0.0.1:{port}"
    try:
        html = None
        for _ in range(120):
            if proc.poll() is not None:
                fail(f"app exited early with code {proc.returncode}")
            try:
                html = urllib.request.urlopen(base + "/", timeout=2).read().decode()
                break
            except OSError:
                time.sleep(0.5)
        if not html:
            fail("UI never came up")
        token = re.search(r'const TOKEN = "([^"]+)"', html).group(1)
        assert "<title>Hoard</title>" in html

        # bundled animations and their player are served locally; nothing else is reachable that way
        for name in ("startup", "loading", "scanning", "done"):
            anim = json.loads(urllib.request.urlopen(f"{base}/assets/{name}.json", timeout=10).read())
            if not anim.get("layers") or anim.get("op", 0) <= 0:
                fail(f"animation {name} looks broken")
        r = urllib.request.urlopen(base + "/assets/dotlottie-player.wasm", timeout=10)
        if r.headers.get("Content-Type") != "application/wasm" or r.read(4) != b"\0asm":
            fail("animation player WASM not served correctly")
        for bad in ("/assets/../server.py", "/assets/ui.html", "/assets/%2e%2e/server.py"):
            try:
                urllib.request.urlopen(base + bad, timeout=10)
                fail(f"{bad} should not be served")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    fail(f"{bad} gave {e.code}, expected 404")
        print("animations and player: served; other paths refused")

        def api(name, body=None):
            req = urllib.request.Request(base + "/api/" + name, data=json.dumps(body or {}).encode(),
                                         headers={"X-Token": token, "Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=60).read())

        def scan(vid):
            assert api("scan", {"drive": vid}).get("ok")
            for _ in range(240):
                p = api("progress")
                if not p["running"]:
                    if p.get("error"):
                        fail("scan error: " + p["error"])
                    return p
                time.sleep(0.25)
            fail("scan did not finish")

        d = api("drives")
        print(f"os={d['os']} trash={d['trashName']} reveal={d['revealName']}")
        for v in d["drives"]:
            print(f"  volume {v['id']!r:28} {v['root']!r:40} {v['type']:9} {v['used'] / 2**30:8.1f} GB used")
        real = [v for v in d["drives"] if v["type"] != "folder"]
        if not real:
            fail("no real volumes listed")
        folder = next(v for v in d["drives"] if v["type"] == "folder")
        vid = folder["id"]

        p = scan(vid)
        print(f"scan 1: {p['files']} files, {p['bytes'] / MB:.1f} MB")
        if p["files"] != 5:
            fail(f"expected 5 files, saw {p['files']}")
        v = api("view", {"drive": vid, "path": []})["view"]
        names = {c["n"] for c in v.get("ch", [])}
        if not {"sub", "models"} <= names:
            fail(f"unexpected children: {names}")

        # search, types, ages, growth index
        hits = api("search", {"drive": vid, "path": [], "q": "clip"})["items"]
        if sorted(h["p"][-1] for h in hits) != ["clip copy.mp4", "clip.mp4"]:
            fail(f"search for 'clip' found {[h['p'] for h in hits]}")
        old_hits = api("search", {"drive": vid, "path": [], "olderDays": 365, "kind": "file"})["items"]
        if [h["p"][-1] for h in old_hits] != ["model.safetensors"]:
            fail(f"'not changed for a year' found {[h['p'] for h in old_hits]}")
        types = {t["ext"]: t for t in api("types", {"drive": vid, "path": []})["items"]}
        if types.get("mp4", {}).get("n") != 2:
            fail(f"types: {types}")
        ages = api("ages", {"drive": vid, "path": []})["hist"]
        if ages[4] < MB or ages[0] < 5 * MB:
            fail(f"age histogram looks wrong: {ages}")
        if len(api("trend", {"drive": vid, "path": ["sub"]})["points"]) != 1:
            fail("trend should have one point after one scan")
        print("search, types, ages, trend: ok")

        # preview streaming with a byte range
        req = urllib.request.Request(f"{base}/file?t={token}&v={vid}&p=" + urllib.request.quote(json.dumps(["sub", "clip.mp4"])),
                                     headers={"Range": "bytes=0-99"})
        r = urllib.request.urlopen(req, timeout=10)
        if r.status != 206 or len(r.read()) != 100:
            fail("preview range request failed")

        # duplicates
        j = api("dupes_start", {"drive": vid, "path": [], "minMB": 1})
        for _ in range(120):
            st = api("dupes_status")
            if not st["running"]:
                break
            time.sleep(0.25)
        groups = st.get("groups") or []
        if len(groups) != 1 or sorted(f["p"][-1] for f in groups[0]["files"]) != ["clip copy.mp4", "clip.mp4"]:
            fail(f"duplicates: {groups}")
        print(f"duplicates: found {len(groups)} group ({groups[0]['wasted'] / MB:.1f} MB extra)")

        # cleanup suggestions run to completion (measuring only; nothing is cleared)
        api("cleanup_start")
        for _ in range(240):
            cs = api("cleanup_status")
            if not cs["running"]:
                break
            time.sleep(0.25)
        if cs["running"] or not cs["items"] or any(i.get("pending") for i in cs["items"]):
            fail("cleanup analysis didn't finish")
        print(f"cleanup: {len(cs['items'])} suggestions measured")

        # ignore list hides a folder from the view
        api("ignore", {"drive": vid, "path": ["copies"], "on": True})
        if "copies" in {c["n"] for c in api("view", {"drive": vid, "path": []})["view"].get("ch", [])}:
            fail("ignored folder still shown")
        api("ignore", {"drive": vid, "path": ["copies"], "on": False})

        # export
        out = api("export_csv", {"name": "smoke", "columns": ["a", "b"], "rows": [["=cmd", 1]]})["file"]
        with open(out, encoding="utf-8-sig") as f:
            if "'=cmd" not in f.read():
                fail("CSV export didn't neutralise a formula")
        if not os.path.exists(api("export_report", {"drive": vid, "path": []})["file"]):
            fail("report not written")
        pdf = api("export_pdf", {"drive": vid, "path": []})["file"]
        with open(pdf, "rb") as f:
            if not f.read(8).startswith(b"%PDF"):
                fail("PDF export isn't a PDF")
        png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        if not os.path.exists(api("export_image", {"name": "smoke", "data": png})["file"]):
            fail("map image not written")
        print("ignore, CSV and HTML export: ok")

        # owner & permissions, extra locations
        info = api("info", {"drive": vid, "path": ["sub"]})
        if not info.get("info", {}).get("owner") or not info["info"].get("perms"):
            fail(f"owner/permissions missing: {info}")
        loc = api("add_location", {"path": os.path.join(target, "models")})
        if loc.get("error") or loc["id"] not in {v["id"] for v in api("drives")["drives"]}:
            fail(f"adding a location failed: {loc}")
        if api("remove_location", {"drive": loc["id"]}).get("error"):
            fail("removing a location failed")
        print(f"owner ({info['info']['owner']}), permissions and locations: ok")
        if v["s"] < 6 * MB:
            fail(f"total too small: {v['s']}")
        files = api("files", {"drive": vid, "path": []})["items"]
        if files[0]["p"] != ["sub", "big.bin"]:
            fail(f"largest file should be sub/big.bin, got {files[0]['p']}")
        cats = {f["p"][-1]: f["c"] for f in files}
        if cats.get("clip.mp4") != 0 or cats.get("model.safetensors") != 7:
            fail(f"categories wrong: {cats}")

        write(os.path.join(target, "sub", "grew.bin"), 60 * MB)
        scan(vid)
        ch = api("changes", {"drive": vid, "path": []})
        grew = [i["p"] for i in ch["items"]]
        if ["sub", "grew.bin"] not in grew:
            fail(f"'what grew' should list sub/grew.bin, got {grew}")
        print("scan 2: change detected:", grew)

        hist = api("history", {"drive": vid})["items"]
        if len(hist) != 2:
            fail(f"expected 2 saved scans, got {len(hist)}")
        api("set_baseline", {"drive": vid, "ts": hist[1]["ts"]})

        # the daily schedule runs the app with --scan; do the same, then check the open app picks it up
        time.sleep(1.1)
        r = subprocess.run(args + ["--scan", vid], env=env, timeout=180)
        if r.returncode != 0:
            fail(f"--scan exited with {r.returncode}")
        hist = api("history", {"drive": vid})["items"]
        if len(hist) != 3:
            fail(f"background scan didn't save a snapshot (have {len(hist)})")
        if api("open_drive", {"drive": vid})["time"] != hist[0]["ts"]:
            fail("open app didn't switch to the background scan")
        rep = api("settings")["report"]
        if not rep or rep[0]["vol"] != vid or "delta" not in rep[0]:
            fail(f"background scan wasn't reported: {rep[:1]}")
        print(f"background scan: saved, picked up, reported (history {len(hist)} scans)")

        # staging area: add, dedupe, remove (and with --trash, move to the Trash)
        st = api("stage_add", {"drive": vid, "items": [{"path": ["sub", "big.bin"], "size": 3 * MB, "kind": "f", "c": 8},
                                                       {"path": ["sub"], "size": 5 * MB, "kind": "d", "c": 8}]})
        if [i["path"] for i in st["items"]] != [["sub"]]:
            fail(f"staging should keep only the parent folder: {st['items']}")
        api("stage_clear")
        api("stage_add", {"drive": vid, "items": [{"path": ["sub", "big.bin"], "size": 3 * MB, "kind": "f", "c": 8}]})
        if do_trash:
            r = api("stage_trash")
            if r.get("done") != 1 or r.get("failed"):
                fail("staged trash failed: " + str(r.get("failed")))
            if os.path.exists(os.path.join(target, "sub", "big.bin")):
                fail("file still exists after trash")
            print("staging + trash: moved sub/big.bin to the", d["trashName"])
        else:
            api("stage_clear")
            print("staging: ok (trash not tested; pass --trash)")
        guard = api("recycle", {"drive": real[0]["id"], "path": []})
        if not guard.get("error"):
            fail("deleting a whole disk must be refused")
        print("SMOKE TEST PASSED")
    finally:
        proc.kill()
        proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
