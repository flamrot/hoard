# Hoard

See what's taking up disk space. Hoard scans a disk and draws it as a treemap, so the biggest folders and files are the biggest blocks. Every scan is saved, so it opens instantly, and the **What grew** tab shows exactly what got bigger since the previous scan. Anything you remove goes to the Recycle Bin or Trash.

Runs on **Windows**, **macOS** and **Linux**.

## Download / install

| OS | File | Notes |
|---|---|---|
| Windows 10/11 | `Hoard.exe` | Single file, no install. The first run may show "Windows protected your PC" because the exe isn't code-signed: **More info → Run anyway**. |
| macOS 12+ | `Hoard-<version>-macos-arm64.dmg` (Apple Silicon) or `-x86_64.dmg` (Intel; also runs on Apple Silicon) | Drag Hoard to Applications. It isn't notarized, so the first time **right-click → Open** (or run `xattr -dr com.apple.quarantine /Applications/Hoard.app`). To scan everything, give it **Full Disk Access** in System Settings → Privacy & Security. |
| Linux (any distro) | `Hoard-<version>-x86_64.AppImage` | `chmod +x Hoard-*.AppImage` and run it. Needs glibc 2.35+ (Ubuntu 22.04 or newer). |
| Debian / Ubuntu | `hoard_<version>_amd64.deb` | `sudo apt install ./hoard_*.deb`, then start **Hoard** from the app menu or run `hoard`. |

### Where the window comes from
- **macOS:** a native window (WKWebView via pywebview).
- **Windows:** an Edge app window (Edge ships with Windows).
- **Linux:** a Chrome, Chromium, Edge, Brave or Vivaldi app window if one is installed, otherwise your default browser. If `pywebview` with its GTK backend is installed system-wide and you run from source, you get a native window.

The app only listens on `127.0.0.1` and every request needs a per-launch token. Nothing is uploaded.

## Using it
- Click a block to select it. **Double-click** a folder to zoom in. **Backspace** goes up a level.
- Right-click for **Open**, **Show in Explorer/Finder/Files**, **Copy path** and **Move to Recycle Bin/Trash** (**Del**, or **⌘⌫** on macOS).
- Click a type under **What's in here** to highlight it on the map.
- System folders and the top level of the system disk can't be deleted from Hoard.
- A running scan can be **hidden**: it continues in the header while you keep using the map.
- **Settings** (gear icon): scan chosen disks automatically every day at a set time, get a notification when a disk grows by more than a set amount, choose how many scans to keep, and pick light/dark/system appearance.
- Every scan is kept (up to your limit). Use the **vs** picker in the toolbar to compare with any earlier scan.
- **Staging area**: drag blocks onto **Staged** (or press **S**) to collect things to remove, review the list and total, then move them all to the Recycle Bin/Trash in one go.
- **Clean up** (key 2): measures caches, temp files, crash dumps, shader caches, browser caches, package-manager caches, the Recycle Bin, old installers in Downloads and `node_modules` in inactive projects. Caches are cleared directly; your own files are only ever staged.
- **Duplicates** (key 3): finds files with identical contents (size → sampled bytes → full hash) in the folder open on the map; stage every extra copy keeping the newest or oldest.
- **Apps** (key 4): installed apps by size, an Uninstall button (the app's own uninstaller on Windows, Trash on macOS, the command on Linux), and **Related folders** to find what an app leaves behind.
- **Search** (Ctrl+F) with filters for kind, size, type, extension and "not changed for…"; a **Types** tab totals space by file extension.
- **Age view**: color the map by when files were last modified to spot things untouched for a year or more.
- **Growth chart** for any folder across saved scans, **size alerts** per folder, an **ignore list**, **quick preview** (Space) for images, video, audio and text, and **export** of any list as CSV or the folder as an HTML report.

### More places to map
- **Folders and network shares**: the **+** next to your disks adds any folder or `\\server\share` as its own map.
- **Google Drive** (and OneDrive, Dropbox, Box, S3, SharePoint… through [rclone](https://rclone.org)): **+ → Connect Google Drive** installs rclone if needed and opens Google's sign-in. Hoard shows your quota (including Gmail and Photos), maps every file without downloading anything, and can move files to Drive's own trash and empty it. Other clouds: run `rclone config` once and they appear automatically.
- **Online-only files** (OneDrive, iCloud, Google Drive for desktop placeholders) are counted separately, so the map shows what's really on the disk.

### Simple mode and the map's detail level
Hoard opens in **Simple mode**: how full the drive is, one-click safe cleanup, what kinds of files use the space, the biggest folders, big files untouched for a year, and what grew. Switch it off for the full map; the map's **Detail** control (Low / Medium / High) starts at Low, a few dozen large blocks.

### Automatic helpers (Settings)
- **Low-space warnings** when a drive drops below a % or GB you choose (after the daily check, or from the tray).
- **Clean up automatically**, weekly or daily: only the cache/temp targets you tick, never the Recycle Bin or your own files.
- **Tray icon** (Windows): free space and today's growth on hover, warnings, quick open/check.
- **“Open in Hoard”** in the right-click menu of File Explorer (Windows) or Nautilus/Nemo/Caja/Dolphin (Linux).
- **Export** the map as a PNG, or a PDF report with the map and the what-grew / largest folders / largest files tables.
- **Duplicates in Google Drive** and other clouds use the checksums the cloud already stores, so nothing is downloaded.

### Fast scan (Windows)
Settings → **Fast scan** reads the NTFS file table directly (like WizTree) with a small Rust helper, `native/` → `hoard-mft.exe`. A whole drive takes seconds instead of a minute. Windows asks for administrator permission each time; decline and Hoard scans normally. Daily scheduled scans use the normal scanner. Building it needs Rust (`winget install Rustlang.Rustup`); without Rust the build simply leaves it out.

### Owner & permissions
Right-click anything → **Owner & permissions…** shows the owner, attributes and who can do what, and for folders, which accounts own the space inside.

### Daily scans
The daily scan is registered with the operating system, so it runs even when Hoard is closed:
Windows Task Scheduler ("Hoard daily scan"), a launchd agent on macOS (`app.hoard.scan`), or a systemd user timer on Linux (`hoard-scan.timer`, with cron as a fallback). It runs `Hoard --scan`, which scans, saves, prunes old scans and notifies, then exits. Turning it off in Settings removes it.

Saved scans live in `%LOCALAPPDATA%\Hoard` (Windows), `~/Library/Application Support/Hoard` (macOS), or `~/.local/share/hoard` (Linux).

## Run from source
Python 3.10+ is all you need:
```
python hoard/server.py            # opens the app window
python hoard/server.py --no-window --port 8765   # headless; open http://127.0.0.1:8765/
```

## Build
Each package must be built on its own OS (PyInstaller doesn't cross-compile).

| OS | Command | Output |
|---|---|---|
| Windows | `powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1` | `dist\Hoard.exe` |
| macOS | `./packaging/build_macos.sh` | `dist/Hoard.app`, `dist/Hoard-<version>-macos-<arch>.dmg` |
| Linux | `./packaging/build_linux.sh` (build on Ubuntu 22.04 for the widest support) | `dist/Hoard-<version>-x86_64.AppImage`, `dist/hoard_<version>_amd64.deb` |

Set `VERSION=1.2.3` to stamp a version. To check a build end to end (scans a temp folder twice, checks "What grew", and with `--trash` deletes a test file):
```
python packaging/smoke_test.py dist/Hoard.exe --trash
```

### GitHub Actions
`.github/workflows/build.yml` builds and smoke-tests all three on GitHub's Windows, macOS and Ubuntu runners. Run it from the Actions tab, or push a tag such as `v1.0.0` to also publish a GitHub Release with every file attached.

## Layout
```
hoard/server.py      scanner, saved scans, comparisons, local HTTP API
hoard/ui.html        the whole interface (one file, no dependencies)
hoard/plat/          everything OS-specific: disks, Trash, reveal, protected paths, window
hoard/Hoard.pyw      launcher used for the packaged apps
packaging/           build scripts, icons, .desktop file, smoke test
```
