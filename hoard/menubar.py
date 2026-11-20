"""macOS menu bar icon (Hoard --tray): free space at a glance, low-space alerts, quick open / check.

The macOS counterpart of tray.py: an NSStatusItem in a process with no Dock icon, refreshed every 10 minutes.
Starts at login through a LaunchAgent. Uses PyObjC, which ships with pywebview on macOS.
"""
import fcntl
import os
import plistlib
import signal
import subprocess

import automation
import core
import plat

EVERY_S = 10 * 60
LABEL = "app.hoard.menubar"
AGENT = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
LOCK = os.path.join(core.DATA_DIR, "menubar.lock")


# Hoard's logo (packaging/icons/hoard.svg) without its tile: the four blocks, in the SVG's 64-unit coordinates
LOGO = [(11, 10, 15, 44, 4), (38, 10, 15, 21, 4), (38, 34, 15, 20, 4), (28.5, 26, 7, 12, 2.5)]


def _draw_logo(rect):
    from AppKit import NSBezierPath, NSColor
    s = 16 / 44                                       # the blocks span 42 x 44 units: fit them in 16 pt
    ox, oy = (18 - 42 * s) / 2 - 11 * s, (18 - 44 * s) / 2 - 10 * s
    NSColor.blackColor().setFill()
    for x, y, w, h, r in LOGO:
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            ((ox + x * s, oy + y * s), (w * s, h * s)), r * s, r * s).fill()
    return True


def _disks():
    return [v for v in plat.list_volumes() if v["type"] in ("fixed", "removable")]


def run():
    try:
        import objc
        from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSImage, NSMenu, NSMenuItem,
                            NSStatusBar, NSVariableStatusItemLength)
        from Foundation import NSObject, NSTimer
    except ImportError:
        print("The menu bar icon needs PyObjC (pip install pywebview).")
        return 1
    os.makedirs(core.DATA_DIR, exist_ok=True)
    lock = open(LOCK, "a+")                            # held while running: one menu bar icon is enough
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()

    class Bar(NSObject):
        def setUp(self):
            self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
            img = NSImage.imageWithSize_flipped_drawingHandler_((18, 18), True, _draw_logo)
            img.setTemplate_(True)                    # one colour that follows the light/dark menu bar
            img.setAccessibilityDescription_("Hoard")
            self.item.button().setImage_(img)
            self.refresh_(None)
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(EVERY_S, self, "refresh:", None, True)

        @objc.python_method
        def add(self, menu, title, action=None):
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
            if action:
                mi.setTarget_(self)
            else:
                mi.setEnabled_(False)
            menu.addItem_(mi)

        @objc.typedSelector(b"v@:@")
        def refresh_(self, _timer):
            try:
                vols, report = _disks(), core.read_report()
                text = automation.tooltip_text(vols, report)
                self.item.button().setToolTip_(text)
                menu = NSMenu.alloc().init()
                for line in text.removeprefix("Hoard · ").split(" · "):
                    self.add(menu, line)
                menu.addItem_(NSMenuItem.separatorItem())
                self.add(menu, "Open Hoard", "open:")
                self.add(menu, "Check my disks now", "scan:")
                menu.addItem_(NSMenuItem.separatorItem())
                self.add(menu, "Hide this icon", "hide:")
                self.item.setMenu_(menu)
                automation.check_low_space(vols, core.load_settings())
            except Exception as ex:  # noqa: BLE001
                core.log_error("menu bar", str(ex))

        @objc.typedSelector(b"v@:@")
        def open_(self, _sender):
            subprocess.Popen(plat.self_command())

        @objc.typedSelector(b"v@:@")
        def scan_(self, _sender):
            subprocess.Popen(plat.self_command() + ["--scan"])
            plat.notify("Checking your disks", "Hoard is scanning in the background. Open it to see the results.")

        @objc.typedSelector(b"v@:@")
        def hide_(self, _sender):
            NSApplication.sharedApplication().terminate_(None)

        # This process is "Hoard" to macOS, so opening Hoard from Finder or the Dock while the icon runs
        # only reopens this process: open the window then.
        @objc.typedSelector(b"Z@:@Z")
        def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _visible):
            self.open_(None)
            return False

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)     # no Dock icon, no app menu
    bar = Bar.alloc().init()
    app.setDelegate_(bar)
    bar.setUp()
    app.run()
    return 0


def _running_pid():
    try:
        with open(LOCK, encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # we got the lock, so nobody holds it: not running
            return None
    except BlockingIOError:
        with open(LOCK, encoding="utf-8") as f:
            return int(f.read().strip() or 0) or None
    except (OSError, ValueError):
        return None


def set_autostart(on):
    """Show the icon at login (this account only), and show or remove it now."""
    try:
        if on:
            os.makedirs(os.path.dirname(AGENT), exist_ok=True)
            with open(AGENT, "wb") as f:
                plistlib.dump({"Label": LABEL, "ProgramArguments": plat.self_command() + ["--tray"],
                               "RunAtLoad": True, "ProcessType": "Interactive"}, f)
        elif os.path.exists(AGENT):
            os.remove(AGENT)
    except OSError as ex:
        return False, ex.strerror or str(ex)
    pid = _running_pid()
    if on and not pid:
        subprocess.Popen(plat.self_command() + ["--tray"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not on and pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    return True, None
