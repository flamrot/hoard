"""Things Hoard does on its own: low-space alerts and scheduled auto-clean (run after the daily scan
and, on Windows, from the tray icon)."""
import json
import os
import shutil
import time

import cleanup
import core
import plat

STATE = os.path.join(core.DATA_DIR, "alerts.json")
GB = 1 << 30


def low_space(total, free, rule):
    """True when a drive is fuller than the rule allows: under `percent` % free or under `gb` GB free."""
    if not rule.get("enabled") or not total:
        return False
    pct = free / total * 100
    return (rule.get("percent") and pct < float(rule["percent"])) or (rule.get("gb") and free < float(rule["gb"]) * GB)


def _state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def check_low_space(vols, settings, now=None, notify=True):
    """Notify (at most once a day per drive) about drives that are getting full. Returns the warnings.
    `notify` is True (system notification), False (just report), or a function(title, body)."""
    send = notify if callable(notify) else plat.notify
    now = now or time.time()
    rule = settings.get("lowSpace") or {}
    st = _state()
    out = []
    for v in vols:
        if v.get("type") not in ("fixed", "removable"):
            continue
        try:
            u = shutil.disk_usage(v["root"])
        except OSError:
            continue
        if not low_space(u.total, u.free, rule):
            continue
        out.append({"vol": v["id"], "name": v["name"], "free": u.free, "total": u.total})
        if notify and now - st.get(v["id"], 0) > 20 * 3600:
            send(f"{v['name']} is almost full",
                        f"Only {core.human(u.free)} free ({u.free / u.total * 100:.0f}%). Open Hoard to see what's taking the space.")
            st[v["id"]] = now
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except OSError:
        pass
    return out


AUTO_SAFE = None


def auto_clean_choices():
    """Cleanup targets that may run unattended: caches/temp folders that are cleared, never your own files."""
    return [s for s in plat.cleanup_specs() if s["action"] == "clear" and not s.get("review")]


def auto_clean_due(cfg, now=None):
    if not cfg.get("enabled"):
        return False
    gap = 86400 if cfg.get("every") == "day" else 7 * 86400
    return (now or time.time()) - float(cfg.get("last") or 0) >= gap - 3600


def run_auto_clean(settings, now=None, notify=True):
    cfg = settings.get("autoClean") or {}
    if not auto_clean_due(cfg, now):
        return None
    allowed = {s["id"] for s in auto_clean_choices()}
    freed, done = 0, []
    for cid in cfg.get("ids", []):
        if cid not in allowed:               # only ever the safe cache/temp targets
            continue
        r = cleanup.run(cid)
        if r.get("ok"):
            freed += r.get("freed", 0)
            done.append(cid)
    s = core.load_settings()
    s["autoClean"]["last"] = now or time.time()
    core.save_settings(s)
    core.append_report({"time": int(now or time.time()), "autoClean": True, "freed": freed, "ids": done,
                        "name": "Auto-clean", "vol": "", "total": 0})
    if notify and freed >= 100 << 20:
        plat.notify(f"Hoard cleared {core.human(freed)}", "Caches and temporary files, as set in Settings → Clean up automatically.")
    return {"freed": freed, "ids": done}
