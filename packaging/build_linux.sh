#!/usr/bin/env bash
# Builds dist/Hoard-<version>-x86_64.AppImage and dist/hoard_<version>_amd64.deb.
# Build on the OLDEST distro you want to support (e.g. Ubuntu 22.04): the result needs that glibc or newer.
# Needs: python3 (3.10+), python3-venv or pip, curl, dpkg-deb, file.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
VERSION="${VERSION:-1.0.0}"
ARCH="${ARCH:-x86_64}"
DEB_ARCH="$( [ "$ARCH" = "aarch64" ] && echo arm64 || echo amd64 )"
MAINTAINER="${MAINTAINER:-Hoard Maintainers <hoard@example.invalid>}"

python3 -m pip install --quiet --upgrade pyinstaller

# One-folder build (starts faster than one-file and packages cleanly)
python3 -m PyInstaller --noconfirm --clean --windowed --name hoard \
  --add-data "$ROOT/hoard/ui.html:." \
  --paths "$ROOT/hoard" \
  --distpath "$ROOT/build/dist" --workpath "$ROOT/build/work" --specpath "$ROOT/build" \
  --log-level WARN \
  "$ROOT/hoard/Hoard.pyw"
mkdir -p dist

# ---------------------------------------------------------------- AppImage
APPDIR="build/Hoard.AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib/hoard" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -a build/dist/hoard/. "$APPDIR/usr/lib/hoard/"
cp packaging/linux/hoard.desktop "$APPDIR/hoard.desktop"
cp packaging/linux/hoard.desktop "$APPDIR/usr/share/applications/hoard.desktop"
cp packaging/icons/hoard-256.png "$APPDIR/hoard.png"
cp packaging/icons/hoard-256.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/hoard.png"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/hoard/hoard" "$@"
EOF
chmod +x "$APPDIR/AppRun"
if [ ! -x build/appimagetool ]; then
  curl -fsSL -o build/appimagetool \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
  chmod +x build/appimagetool
fi
ARCH="$ARCH" build/appimagetool --appimage-extract-and-run "$APPDIR" "dist/Hoard-${VERSION}-${ARCH}.AppImage"

# ---------------------------------------------------------------- .deb
PKG="build/deb/hoard_${VERSION}_${DEB_ARCH}"
rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt/hoard" "$PKG/usr/bin" "$PKG/usr/share/applications" \
         "$PKG/usr/share/icons/hicolor/256x256/apps"
cp -a build/dist/hoard/. "$PKG/opt/hoard/"
ln -s /opt/hoard/hoard "$PKG/usr/bin/hoard"
cp packaging/linux/hoard.desktop "$PKG/usr/share/applications/hoard.desktop"
cp packaging/icons/hoard-256.png "$PKG/usr/share/icons/hicolor/256x256/apps/hoard.png"
cat > "$PKG/DEBIAN/control" <<EOF
Package: hoard
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${DEB_ARCH}
Maintainer: ${MAINTAINER}
Depends: libc6 (>= 2.35), xdg-utils
Recommends: libglib2.0-bin
Suggests: chromium | google-chrome-stable | microsoft-edge-stable
Description: See what's taking up disk space
 Hoard scans a disk and draws it as a treemap, so the biggest folders and
 files are the biggest blocks. It remembers each scan and shows what grew
 since the last one, and can move things to the Trash.
EOF
dpkg-deb --build --root-owner-group "$PKG" "dist/hoard_${VERSION}_${DEB_ARCH}.deb"
echo "Built dist/Hoard-${VERSION}-${ARCH}.AppImage and dist/hoard_${VERSION}_${DEB_ARCH}.deb"
