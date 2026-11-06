#!/usr/bin/env bash
# Build "NSE Quant Terminal.app" and a drag-to-Applications .dmg into dist/.
#   packaging/build_mac.sh            (uses ./.venv)
# Requirements: the project's .venv with requirements.txt + pyinstaller installed.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
VERSION=$(grep -E '^VERSION = ' packaging/NSEQuantTerminal.spec | cut -d'"' -f2)
APP="dist/NSE Quant Terminal.app"
DMG="dist/NSE-Quant-Terminal-${VERSION}.dmg"

[ -f packaging/AppIcon.icns ] || $PY packaging/make_icon.py
rm -rf build "dist/NSE Quant Terminal" "$APP" "$DMG"
$PY -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/NSEQuantTerminal.spec

# Safety: the bundle must never contain secrets.
if find "$APP" -name ".env" -o -name "shoonya.json" | grep -q .; then
  echo "ERROR: secrets found inside the app bundle" >&2; exit 1
fi

# Drag-to-Applications disk image
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "NSE Quant Terminal" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"
du -sh "$APP" "$DMG"
echo "Built: $APP and $DMG"
