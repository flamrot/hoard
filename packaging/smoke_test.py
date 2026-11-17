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

    tmp = tempfile.mkdtemp(prefix="hoard-smoke-")
    target, data = os.path.join(tmp, "target"), os.path.join(tmp, "data")
    os.makedirs(data)
    write(os.path.join(target, "sub", "big.bin"), 3 * MB)
    write(os.path.join(target, "sub", "clip.mp4"), 2 * MB)
    write(os.path.join(target, "sub", "notes.txt"), 2000)
    write(os.path.join(target, "models", "model.safetensors"), int(1.5 * MB))

    port = free_port()
    env = dict(os.environ, HOARD_SCAN_DIR=target, HOARD_DATA_DIR=data)
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
        if p["files"] != 4:
            fail(f"expected 4 files, saw {p['files']}")
        v = api("view", {"drive": vid, "path": []})["view"]
        names = {c["n"] for c in v.get("ch", [])}
        if not {"sub", "models"} <= names:
            fail(f"unexpected children: {names}")
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

        if do_trash:
            r = api("recycle", {"drive": vid, "path": ["sub", "big.bin"]})
            if not r.get("ok"):
                fail("trash failed: " + str(r.get("error")))
            if os.path.exists(os.path.join(target, "sub", "big.bin")):
                fail("file still exists after trash")
            print("trash: moved sub/big.bin to the", d["trashName"])
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
