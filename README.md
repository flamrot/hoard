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
- **Windows:** a native window (WebView2 via pywebview), so Hoard can be pinned to the taskbar. Without WebView2 it falls back to an Edge app window.
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
- **Removal list**: drag blocks onto **Removal list** (or press **S**, or use **Mark for removal**) to collect things to remove, review the list and total, then move them all to the Recycle Bin/Trash in one go. Nothing is deleted until you confirm there.
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
Hoard opens in **Simple mode**: how full the drive is, one-click safe cleanup of caches and temporary files (the Recycle Bin is a separate, deliberate step), what kinds of files use the space, the biggest folders, big files of yours untouched for a year (never system, program or app files), and what grew. **Show on map** or **Explore the full map** opens the map with a **Back to Simple view** button; switch Simple off to start in the map. The map's **Show** control (Big items / More / Everything) starts at Big items, a few dozen large blocks.

### Files (key 5): a dual-pane file manager
A Midnight Commander-style file manager inside Hoard, with the best of Total Commander, Far and friends:
- **Two panes:**
  - tabs per pane (Alt+T/W) and a breadcrumb you can click or type into (Alt+C);
  - **folder sizes straight from the last scan**, with size bars and **Biggest first**;
  - Full/Brief listings, hidden files, branch view (Ctrl+B: every file below, flat).
- **MC keys:**
  - **F3** view (text/hex, images, video, audio, PDF), **F5** copy, **F6** move (Shift+F6 rename);
  - **F7** new folder (Shift+F7 new file), **F8** to the Recycle Bin/Trash (Shift+F8 delete for good, with a typed confirmation);
  - **Insert/Space** tag, **+ − \*** select by mask, **Ctrl+S** quick filter;
  - **Ctrl+U** swap, **Alt+O/I** sync the other pane, **Ctrl+\\** saved locations, **Alt+Left/Right** history, **Ctrl+Q** quick view;
  - **Alt+F7** find (name and contents), **Ctrl+M** multi-rename with a live preview, **Ctrl+Shift+P** command palette, **F1** all keys.
- **Jobs:** copies and moves run as jobs, with progress, speed, pause, resume and cancel, and ask what to do on a name clash (Replace keeps the old one in the bin). **Ctrl+Z** undoes renames, moves, trash, new items and unchanged copies; it never overwrites newer work. An interrupted job is recovered the next time Hoard starts.
- **Compare and sync** (Ctrl+X then D): both panes line up file by file (only left/right, newer, different, same; quick by size and date or thorough by content), then **Review sync…** shows a direction per file, a dry run, and applies it as an undoable job. Nothing is deleted unless you tick it, and then only to the Recycle Bin/Trash.
- **Archives:** Enter opens a .zip/.tar/.tar.gz/.tgz/.tar.bz2/.tar.xz like a read-only folder; F3 views members, F5 extracts, **Alt+F9** extracts all, **Alt+F5** packs the tagged items into a .zip. Archives with unsafe paths, links or bomb-like compression are refused.
- **Checksums** (menu or palette): MD5, SHA-1, SHA-256; copy, save `checksums.sha256`, or verify against a checksum file or a pasted value.
- **Editor (F4):** text files up to 5 MB with line numbers, search/replace (regex), go to line, and wrap; it keeps the file's encoding (UTF-8 with or without BOM, UTF-16, Latin-1) and every line's own line ending. Saving keeps the file's permissions and attributes, puts the previous version in the Recycle Bin/Trash, and can be undone.
- **System-critical files can never be deleted, moved, renamed or overwritten:** not from Files, the map, the removal list or cleanup. Protection is checked by file identity, so another name for the same folder (a junction, `subst` drive, mapped drive, `\\localhost\C$`, a bind mount) doesn't get around it. Protected rows show a lock and the reason. Links are never followed.

### Keyboard
On the map: **arrow keys** move between blocks, **Enter** opens a folder, **Backspace** goes up, **S** marks the selection for removal, **Space** previews a file, **Del** moves it to the Recycle Bin/Trash, **Shift+F10** opens the options menu. **1–4** switch views, **Ctrl+F** searches. In the side lists, **Enter** opens a folder and the arrow keys move between the tabs.

### Automatic helpers (Settings)
- **Low-space warnings** when a drive drops below a % or GB you choose (after the daily check, or from the tray).
- **Clean up automatically**, weekly or daily: only the cache/temp targets you tick, never the Recycle Bin or your own files.
- **Tray icon** (Windows) / **menu bar icon** (macOS): free space and today's growth at a glance, warnings, quick open/check.
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
hoard/assets/        Lottie animations (startup, loading, scanning, done) and the dotLottie player
hoard/Hoard.pyw      launcher used for the packaged apps
packaging/           build scripts, icons, .desktop file, smoke test
```

### Logo and animations
- The logo is `packaging/icons/hoard.svg`. `python packaging/make_icons.py packaging/icons/hoard.svg` rebuilds `hoard.ico` and the PNGs (it renders with Edge/Chrome, no Python packages needed).
- The animations are generated from the logo's geometry: `python packaging/make_animations.py` writes `hoard/assets/*.json`.
- They play through [dotLottie web](https://github.com/LottieFiles/dotlottie-web) (MIT, `hoard/assets/dotlottie-LICENSE.txt`), served from the app itself, so nothing is fetched from the internet. With "reduce motion" turned on in the OS, each animation shows a still frame instead.
