"""Build every app icon from one SVG.

    python packaging/make_icons.py packaging/icons/hoard.svg

Writes packaging/icons/hoard.png (1024 px, for macOS .icns and Linux),
hoard-256.png, and hoard.ico (16-256 px, PNG-compressed entries).
Rasterises with a headless Chromium browser (Edge, Chrome or Chromium), so it needs no Python packages.
"""
import os
import shutil
import struct
import subprocess
import sys
import tempfile

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def browser():
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"):
        if os.path.exists(p):
            return p
    for n in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        if shutil.which(n):
            return shutil.which(n)
    sys.exit("No Chromium-based browser found to render the SVG.")


def render(exe, svg_path, size, out, tmp):
    page = os.path.join(tmp, f"r{size}.html")
    uri = "file:///" + os.path.abspath(svg_path).replace("\\", "/")
    with open(page, "w", encoding="utf-8") as f:
        f.write(f'<!doctype html><style>html,body{{margin:0;background:transparent;overflow:hidden}}'
                f'img{{display:block;width:{size}px;height:{size}px}}</style><img src="{uri}">')
    subprocess.run([exe, "--headless=new", "--hide-scrollbars", "--force-device-scale-factor=1",
                    "--default-background-color=00000000", f"--user-data-dir={os.path.join(tmp, 'profile')}",
                    f"--window-size={size},{size}", f"--screenshot={out}",
                    "file:///" + page.replace("\\", "/")], capture_output=True, timeout=60)
    if not os.path.exists(out):
        sys.exit(f"Rendering {size}px failed.")


def write_ico(pngs, out):
    """ICO with PNG-encoded images (supported since Windows Vista)."""
    data = [open(p, "rb").read() for _, p in pngs]
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries = b""
    for (size, _), blob in zip(pngs, data):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    with open(out, "wb") as f:
        f.write(header + entries + b"".join(data))


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    svg = sys.argv[1]
    icons = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
    exe = browser()
    tmp = tempfile.mkdtemp(prefix="hoard-icons-")
    try:
        render(exe, svg, 1024, os.path.join(icons, "hoard.png"), tmp)
        render(exe, svg, 256, os.path.join(icons, "hoard-256.png"), tmp)
        pngs = []
        for s in ICO_SIZES:
            p = os.path.join(tmp, f"i{s}.png")
            render(exe, svg, s, p, tmp)
            pngs.append((s, p))
        write_ico(pngs, os.path.join(icons, "hoard.ico"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("wrote hoard.png, hoard-256.png and hoard.ico in", icons)


if __name__ == "__main__":
    main()
