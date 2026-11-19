"""CSV export and the standalone HTML report."""
import csv
import html
import os
import time

from core import human


def unique_path(folder, base, ext):
    os.makedirs(folder, exist_ok=True)
    p = os.path.join(folder, f"{base}.{ext}")
    n = 2
    while os.path.exists(p):
        p = os.path.join(folder, f"{base} ({n}).{ext}")
        n += 1
    return p


def safe_cell(v):
    """Keep spreadsheet apps from treating text as a formula."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def write_csv(path, columns, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(columns)
        for r in rows:
            w.writerow([safe_cell(c) for c in r])
    return path


def jpeg_size(data):
    """(width, height) from a JPEG's start-of-frame marker."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    raise ValueError("not a JPEG")


def report_pdf(title, subtitle, sections, jpeg=None):
    """A small, dependency-free PDF: title, the map (JPEG), then tables. sections = [(heading, [(left, right)])]."""
    W, H, M = 595, 842, 42                       # A4 in points, margin

    def pdf_text(s):
        s = str(s).encode("cp1252", "replace").decode("cp1252")
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    def fit(s, size, width):                     # Helvetica averages ~0.5 em per character
        maxc = max(4, int(width / (size * 0.5)))
        return s if len(s) <= maxc else "…" + s[-(maxc - 1):]

    pages, ops, y = [], [], H - M

    def text(x, yy, s, size=10, bold=False, gray=0.0):
        ops.append(f"BT /{'F2' if bold else 'F1'} {size} Tf {gray:.2f} g {x:.1f} {yy:.1f} Td ({pdf_text(s)}) Tj ET")

    def new_page():
        nonlocal ops, y
        pages.append(ops)
        ops, y = [], H - M

    text(M, y - 18, title, 20, True)
    y -= 36
    text(M, y - 12, subtitle, 10, gray=0.35)
    y -= 28
    img = None
    if jpeg:
        iw, ih = jpeg_size(jpeg)
        dw = W - 2 * M
        dh = min(dw * ih / iw, 380)
        dw = dh * iw / ih
        ops.append(f"q {dw:.1f} 0 0 {dh:.1f} {M:.1f} {y - dh:.1f} cm /Im1 Do Q")
        y -= dh + 22
        img = (iw, ih)
    for heading, rows in sections:
        if y < M + 60:
            new_page()
        text(M, y - 12, heading, 13, True)
        y -= 24
        if not rows:
            text(M, y - 10, "Nothing to show.", 10, gray=0.45)
            y -= 18
        for left, right in rows:
            if y < M + 16:
                new_page()
            text(M, y - 10, fit(left, 9.5, W - 2 * M - 90), 9.5)
            text(W - M - 80, y - 10, right, 9.5, True)
            y -= 15
        y -= 10
    pages.append(ops)

    objs = []                                    # 1 catalog, 2 pages, 3/4 fonts, 5 image, then page/content pairs

    def add(b):
        objs.append(b if isinstance(b, bytes) else b.encode("latin-1"))
        return len(objs)
    add("")                                      # placeholders, filled below
    add("")
    add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    if img:
        add(f"<< /Type /XObject /Subtype /Image /Width {img[0]} /Height {img[1]} /ColorSpace /DeviceRGB "
            f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n".encode() + jpeg + b"\nendstream")
    else:
        add("<< >>")
    kids = []
    for p_ops in pages:
        content = "\n".join(p_ops).encode("cp1252", "replace")
        c = add(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
        res = "/XObject << /Im1 5 0 R >>" if img else ""
        kids.append(add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] /Contents {c} 0 R "
                        f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> {res} >> >>"))
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, b in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + b + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def report_html(title, vol_name, when, total, prev_when=None, delta=None, changes=(), folders=(), files=()):
    e = html.escape
    day = lambda t: time.strftime("%a %d %b %Y, %H:%M", time.localtime(t))   # noqa: E731

    def table(head, rows):
        if not rows:
            return "<p class='muted'>Nothing to show.</p>"
        body = "".join("<tr>" + "".join(f"<td{' class=n' if i else ''}>{e(str(c))}</td>" for i, c in enumerate(r)) + "</tr>"
                       for r in rows)
        return f"<table><thead><tr>{''.join(f'<th>{e(h)}</th>' for h in head)}</tr></thead><tbody>{body}</tbody></table>"

    summary = f"<b>{e(human(total))}</b> scanned on {e(day(when))}"
    if prev_when and delta is not None:
        sign = "▲" if delta >= 0 else "▼"
        summary += f" · {sign} {e(human(abs(delta)))} since {e(day(prev_when))}"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(title)}</title>
<style>
body{{font:14px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif;margin:32px auto;max-width:900px;padding:0 16px;color:#111;background:#fcfcfb}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:15px;margin:28px 0 8px}}
.muted{{color:#777}} table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #e3e2dc}}
th{{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#777}} td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
td:first-child{{word-break:break-all}}
@media (prefers-color-scheme: dark){{body{{background:#1a1a19;color:#eee}} th,td{{border-color:#333}}}}
</style></head><body>
<h1>{e(title)}</h1><p class="muted">{summary}</p>
<h2>What grew</h2>{table(["Folder or file", "Change", "Size now"], [(p, ("+" if d >= 0 else "−") + human(abs(d)), human(s)) for d, p, s in changes])}
<h2>Largest folders</h2>{table(["Folder", "Size"], [(p, human(s)) for p, s in folders])}
<h2>Largest files</h2>{table(["File", "Size"], [(p, human(s)) for p, s in files])}
<p class="muted">Made by Hoard.</p></body></html>"""
