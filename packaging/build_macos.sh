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

# Parallel native scanner (optional; Hoard falls back to Python without it)
EXTRA=()
if command -v cargo >/dev/null; then
  cargo build --release --manifest-path native/Cargo.toml --bin hoard-walk
  EXTRA=(--add-binary "$ROOT/native/target/release/hoard-walk:.")
fi

python3 -m PyInstaller --noconfirm --clean --windowed --name Hoard \
  --icon "$ROOT/build/Hoard.icns" \
  ${EXTRA[@]+"${EXTRA[@]}"} \
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
if [ -n "${MACOS_CERT_P12:-}" ] && [ -n "${MACOS_CERT_PASSWORD:-}" ]; then
  # Developer ID signing + notarization (GitHub secrets: MACOS_CERT_P12 base64, MACOS_CERT_PASSWORD,
  # APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD). Signed + notarized apps open without warnings.
  KC=build/hoard.keychain-db
  security create-keychain -p hoard "$KC" && security unlock-keychain -p hoard "$KC"
  echo "$MACOS_CERT_P12" | base64 --decode > build/cert.p12
  security import build/cert.p12 -k "$KC" -P "$MACOS_CERT_PASSWORD" -T /usr/bin/codesign
  security set-key-partition-list -S apple-tool:,apple: -s -k hoard "$KC" >/dev/null
  security list-keychains -d user -s "$KC" $(security list-keychains -d user | tr -d '"')
  IDENTITY=$(security find-identity -v -p codesigning "$KC" | awk -F'"' '/Developer ID Application/ {print $2; exit}')
  codesign --force --deep --options runtime --timestamp --sign "$IDENTITY" "dist/Hoard.app"
  rm -f build/cert.p12
  SIGNED=1
else
  codesign --force --deep --sign - "dist/Hoard.app"   # ad-hoc signature (required on Apple Silicon)
fi

# Drag-to-Applications disk image
rm -rf build/dmg && mkdir -p build/dmg
cp -R dist/Hoard.app build/dmg/
ln -s /Applications build/dmg/Applications
DMG="dist/Hoard-${VERSION}-macos-$(uname -m).dmg"
hdiutil create -volname Hoard -srcfolder build/dmg -ov -format UDZO "$DMG"
if [ "${SIGNED:-0}" = 1 ] && [ -n "${APPLE_ID:-}" ]; then
  xcrun notarytool submit "$DMG" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD" --wait
  xcrun stapler staple "$DMG"
fi
echo "Built dist/Hoard.app and dist/Hoard-${VERSION}-macos-$(uname -m).dmg"
