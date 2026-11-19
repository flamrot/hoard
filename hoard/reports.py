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
