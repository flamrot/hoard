#!/usr/bin/env bash
# Builds dist/Hoard.app and dist/Hoard-macos.dmg. Run on a Mac (Apple Silicon builds an arm64 app,
# an Intel Mac builds x86_64). Requires Python 3.10+ from python.org or Homebrew.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
VERSION="${VERSION:-1.0.0}"

python3 -m pip install --quiet --upgrade pyinstaller pywebview   # pywebview = native WKWebView window

# App icon (.icns) from the 1024 px PNG
rm -rf build/Hoard.iconset && mkdir -p build/Hoard.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s packaging/icons/hoard.png --out "build/Hoard.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) packaging/icons/hoard.png --out "build/Hoard.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns build/Hoard.iconset -o build/Hoard.icns

python3 -m PyInstaller --noconfirm --clean --windowed --name Hoard \
  --icon "$ROOT/build/Hoard.icns" \
  --osx-bundle-identifier app.hoard.Hoard \
  --add-data "$ROOT/hoard/ui.html:." \
  --paths "$ROOT/hoard" \
  --distpath "$ROOT/dist" --workpath "$ROOT/build/work" --specpath "$ROOT/build" \
  --log-level WARN \
  "$ROOT/hoard/Hoard.pyw"

# Version + a usage string macOS shows if it asks before Hoard reads protected folders
PLIST="dist/Hoard.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST" || true
/usr/libexec/PlistBuddy -c "Add :NSDesktopFolderUsageDescription string 'Hoard measures folder sizes to show what uses disk space.'" "$PLIST" || true
/usr/libexec/PlistBuddy -c "Add :NSDocumentsFolderUsageDescription string 'Hoard measures folder sizes to show what uses disk space.'" "$PLIST" || true
/usr/libexec/PlistBuddy -c "Add :NSDownloadsFolderUsageDescription string 'Hoard measures folder sizes to show what uses disk space.'" "$PLIST" || true
codesign --force --deep --sign - "dist/Hoard.app"   # ad-hoc signature (required on Apple Silicon)

# Drag-to-Applications disk image
rm -rf build/dmg && mkdir -p build/dmg
cp -R dist/Hoard.app build/dmg/
ln -s /Applications build/dmg/Applications
hdiutil create -volname Hoard -srcfolder build/dmg -ov -format UDZO "dist/Hoard-${VERSION}-macos-$(uname -m).dmg"
echo "Built dist/Hoard.app and dist/Hoard-${VERSION}-macos-$(uname -m).dmg"
