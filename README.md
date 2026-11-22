<p align="center">
  <img src="packaging/icons/hoard-256.png" width="112" alt="Hoard logo">
</p>

<h1 align="center">Hoard</h1>

<p align="center">
  <b>See what's taking up your disk space, and clean it up safely.</b><br>
  A fast disk-space treemap, cleanup tool and dual-pane file manager for Windows, macOS and Linux.
</p>

<p align="center">
  <a href="https://github.com/flamrot/hoard/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/flamrot/hoard?label=download"></a>
  <a href="https://github.com/flamrot/hoard/actions/workflows/build.yml"><img alt="Build" src="https://github.com/flamrot/hoard/actions/workflows/build.yml/badge.svg"></a>
  <img alt="Platforms" src="https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-informational">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

---

Hoard scans a disk and draws it as a **treemap**: the biggest folders and files are the biggest blocks. Every scan is saved, so it opens instantly next time, and **What grew** shows exactly what got bigger since the last scan. When you remove something it goes to the **Recycle Bin or Trash**, and Hoard **never** touches system-critical files.

Everything runs on your own computer. Nothing is uploaded, and there are no accounts, ads or telemetry.

## Highlights

- 🗺️ **Treemap of any disk, folder or network share**, with saved scans and *What grew* comparisons against any earlier scan.
- 🧹 **Clean up**: caches, temp files, crash dumps, browser and package-manager caches, old installers, stale `node_modules`. Caches are cleared directly; your own files are only ever staged for you to review.
- 👯 **Duplicates**: files with identical contents, checked by size, then sampled bytes, then a full hash.
- 📦 **Apps**: installed apps by size, with their uninstallers and the folders they leave behind.
- 🗂️ **Files**: a Midnight Commander-style dual-pane file manager with folder sizes from the scan, jobs you can undo, compare and sync, archives, checksums and a text editor.
- ☁️ **Cloud drives**: map Google Drive (and OneDrive, Dropbox, S3 and more through [rclone](https://rclone.org)) without downloading anything.
- ⏰ **Automatic**: daily scans, low-space and growth alerts, scheduled safe cleanup, and a tray or menu-bar icon.
- 🛡️ **Safe by design**: system-critical files can never be deleted, moved, renamed or overwritten, and links are never followed.

## Download

Get the latest version from the **[Releases page](https://github.com/flamrot/hoard/releases/latest)**.

| System | File | Notes |
|---|---|---|
| **Windows** 10 / 11 | `Hoard.exe` | A single file with no installer. The exe isn't code-signed yet, so Windows may show *"Windows protected your PC"*: click **More info → Run anyway**. |
| **macOS** 12+ (Apple Silicon) | `Hoard-<version>-macos-arm64.dmg` | Drag Hoard to Applications. It isn't notarized yet, so open it the first time with **right-click → Open**. To scan everything, give it **Full Disk Access** in System Settings → Privacy & Security. |
| **macOS** 12+ (Intel) | `Hoard-<version>-macos-x86_64.dmg` | As above. This build also runs on Apple Silicon. |
| **Linux** (any distro) | `Hoard-<version>-x86_64.AppImage` | `chmod +x Hoard-*.AppImage`, then run it. Needs glibc 2.35+ (Ubuntu 22.04 or newer). |
| **Debian / Ubuntu** | `hoard_<version>_amd64.deb` | `sudo apt install ./hoard_*.deb`, then start **Hoard** from the app menu. |

Each file has a `.sha256` checksum next to it. Hoard's built-in updater only installs downloads that match it.

## Features

### The map
- Click a block to select it, **double-click** a folder to zoom in, and press **Backspace** to go up.
- Right-click for **Open**, **Show in Explorer/Finder**, **Copy path**, **Owner & permissions…** and **Move to Recycle Bin/Trash**.
- Color the map by **file type** or by **age** to spot things untouched for a year or more.
- **Search** with filters for kind, size, type, extension and "not changed for…", plus a **Types** tab that totals space by extension.
- **Growth chart** for any folder across saved scans, **size alerts** per folder, an **ignore list**, and **quick preview** (Space) for images, video, audio and text.
- **Export** any list as CSV, or the map as a PNG or a PDF report.

### Simple mode
Hoard opens in a calm overview:
- how full the drive is, with one-click safe cleanup;
- what kinds of files use the space and the biggest folders;
- big files of yours you haven't touched in a year (never system, program or app files);
- what grew.

From there, **Explore the full map** opens the treemap.

### Removal list
Drag blocks onto the **Removal list** (or press **S**) to collect things to remove, then review the list and its total. Nothing is removed until you confirm, and then it goes to the Recycle Bin or Trash.

### Files: a dual-pane file manager (key 5)
Inspired by Midnight Commander, Total Commander and Far:

- **Panes:** two panes with tabs and editable breadcrumbs, **folder sizes straight from the scan**, size bars and *Biggest first*.
- **MC keys:**
  - **F3** view, **F4** edit, **F5** copy, **F6** move/rename;
  - **F7** new folder, **F8** to the Recycle Bin/Trash;
  - **Insert** to tag, **Ctrl+U** to swap panes, **Alt+F7** to find, **Ctrl+M** for multi-rename with a live preview.
- **Jobs:**
  - progress, speed, pause, resume and cancel, with a clear question when names clash;
  - **Ctrl+Z** undoes renames, moves, trash and copies, and never overwrites newer work;
  - an interrupted job is recovered the next time Hoard starts.
- **Compare and sync:** line up both panes file by file, review a direction for each file, do a dry run, then apply it as an undoable job.
- **Archives:** browse .zip and .tar(.gz/.bz2/.xz) like folders, extract, and pack. Unsafe paths, links and zip bombs are refused.
- **Checksums:** MD5, SHA-1 and SHA-256; create, copy, save and verify.
- **Editor:** files up to 5 MB, with search/replace (regex), go to line and wrap. It keeps each file's encoding, line endings and permissions, and saves can be undone.

Press **F1** in Files for every key.

### More places to map
- **Folders and network shares:** the **+** next to your disks adds any folder or `\\server\share`.
- **Google Drive and other clouds:** **+ → Connect Google Drive** sets up rclone and opens Google's sign-in. Hoard shows your quota and maps every file without downloading anything. Other clouds appear after a one-time `rclone config`.
- **Online-only files** (OneDrive, iCloud and Google Drive placeholders) are counted separately, so the map shows what's really on the disk.

### Automatic helpers (Settings)
- **Daily scans**, registered with the OS so they run even when Hoard is closed: Task Scheduler on Windows, launchd on macOS, a systemd user timer on Linux.
- **Low-space warnings** and **growth alerts**.
- **Scheduled cleanup** of only the cache and temp targets you choose, never the Recycle Bin or your own files.
- A **tray icon** (Windows) or **menu-bar icon** (macOS) with free space and today's growth.
- **"Open in Hoard"** in the right-click menu of File Explorer and the common Linux file managers.
- **Fast scan** (Windows): reads the NTFS file table directly, so a whole drive takes seconds. It asks for administrator permission each time.

<details>
<summary><b>Keyboard shortcuts (map)</b></summary>

| Key | Action |
|---|---|
| Arrow keys | Move between blocks |
| Enter / Backspace | Open a folder / go up |
| Space | Quick preview |
| S | Add to the removal list |
| Del (⌘⌫ on macOS) | Move to the Recycle Bin/Trash |
| Shift+F10 | Options menu |
| 1–5 | Map, Clean up, Duplicates, Apps, Files |
| Ctrl+F | Search |

</details>

## Safety and privacy

- **System-critical files are off-limits.** The Windows, macOS and Linux system folders, boot files, program folders, your profile's core folders and Hoard's own data can never be deleted, moved, renamed or overwritten, from anywhere in the app.
  - Protection is checked by **file identity**, not just by path, so another route to the same folder doesn't get around it: a junction, `subst` drive, mapped drive, `\\localhost\C$` or bind mount.
  - When Hoard can't be sure, it refuses.
- **Nothing is deleted outright** unless you ask for it explicitly. Removals go to the Recycle Bin or Trash, and file-manager jobs can be undone.
- **Links are never followed** when removing, and an item that changed since you chose it is left alone.
- **Local only.** The interface is served on `127.0.0.1` with a per-launch token. Scans stay on your computer, in:
  - `%LOCALAPPDATA%\Hoard` (Windows)
  - `~/Library/Application Support/Hoard` (macOS)
  - `~/.local/share/hoard` (Linux)

## Run from source

All you need is Python 3.10 or newer, with no packages to install:

```sh
python hoard/server.py                            # opens the app window
python hoard/server.py --no-window --port 8765    # headless; open http://127.0.0.1:8765/
```

The optional Rust helpers in `native/` (fast scanning) need [Rust](https://rustup.rs). Without them Hoard uses its Python scanner.

## Build

PyInstaller doesn't cross-compile, so build each package on its own OS:

| OS | Command | Output |
|---|---|---|
| Windows | `powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1` | `dist\Hoard.exe` |
| macOS | `./packaging/build_macos.sh` | `dist/Hoard.app`, `dist/Hoard-<version>-macos-<arch>.dmg` |
| Linux | `./packaging/build_linux.sh` (build on Ubuntu 22.04 for the widest support) | `dist/*.AppImage`, `dist/*.deb` |

Set `VERSION=1.2.3` to stamp a version.

**Tests:**

```sh
python -m unittest discover -s tests -v              # unit tests
python packaging/smoke_test.py dist/Hoard.exe --trash # end-to-end check of a built app
```

**CI:**
- [`build.yml`](.github/workflows/build.yml) tests, builds and smoke-tests all three platforms. Pushing a `v*` tag publishes a release with every file and its checksum.
- [`tests.yml`](.github/workflows/tests.yml) runs the unit tests on macOS and Linux for `fix/**` and `test/**` branches.

## Project layout

```
hoard/server.py     scanner, saved scans, comparisons, local HTTP API
hoard/ui.html       the whole interface (one file)
hoard/fm.py         Files: job engine, plans, undo journal, compare/sync, archives, editor
hoard/safefs.py     link-proof, identity-checked file operations
hoard/plat/         everything OS-specific: disks, Trash, protected paths, windows
hoard/assets/       Files UI, Lottie animations and the dotLottie player
native/             Rust helpers (NTFS fast scan, directory walker)
packaging/          build scripts, icons, smoke test
tests/              unit tests
```

## Credits

- Animations play through [dotLottie web](https://github.com/LottieFiles/dotlottie-web) (MIT, see `hoard/assets/dotlottie-LICENSE.txt`), served by the app itself.
- Cloud drives are mapped through [rclone](https://rclone.org) (MIT), installed only if you connect a cloud.
- Native windows come from [pywebview](https://pywebview.flowrl.com) (BSD).

## License

[MIT](LICENSE) © 2026 flamrot. You're free to use, change and share Hoard, as long as the copyright notice comes along.
