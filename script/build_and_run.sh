#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export CLANG_MODULE_CACHE_PATH="${TMPDIR:-/tmp}/director-clang-cache"
export SWIFTPM_MODULECACHE_OVERRIDE="$CLANG_MODULE_CACHE_PATH"
swift build --package-path "$ROOT/macos" --scratch-path "$ROOT/macos/.build" --disable-sandbox
DEST="$ROOT/dist/AI-Dev-Director.app"
STAGING=$(mktemp -d "${TMPDIR:-/tmp}/director-bundle.XXXXXX")
trap 'rm -rf "$STAGING"' EXIT HUP INT TERM
APP="$STAGING/AI-Dev-Director.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
ICONSET="$ROOT/macos/.build/AppIcon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$ROOT/macos/Assets/AppIcon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$ROOT/macos/Assets/AppIcon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
/usr/bin/python3 "$ROOT/script/package_icon.py" "$ICONSET" "$APP/Contents/Resources/DirectorIcon.icns"
cp "$ROOT/macos/.build/debug/DirectorApp" "$APP/Contents/MacOS/DirectorApp"
/usr/bin/python3 - "$APP" "$ROOT" <<'PY'
import plistlib,sys
from pathlib import Path
app,root=sys.argv[1:]
with (Path(app)/'Contents/Info.plist').open('wb') as f:
 plistlib.dump({'CFBundlePackageType':'APPL','CFBundleExecutable':'DirectorApp','CFBundleIdentifier':'is.hanginis.ai-dev-director','CFBundleName':'AI Dev Director','CFBundleDisplayName':'AI Dev Director','CFBundleIconFile':'DirectorIcon.icns','CFBundleVersion':'4','CFBundleShortVersionString':'1.1','LSMinimumSystemVersion':'13.0','NSPrincipalClass':'NSApplication','NSHighResolutionCapable':True,'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True},'DirectorRoot':root},f)
PY
# iCloud/Finder may attach metadata that invalidates an otherwise valid signature.
xattr -d com.apple.FinderInfo "$APP" 2>/dev/null || true
codesign --force --sign - "$APP"
# Sign outside iCloud, which may attach Finder metadata during signing.
mkdir -p "$ROOT/dist"
rm -f "$DEST/Contents/Resources/AppIcon.icns"
/usr/bin/ditto "$APP" "$DEST"
# Let Finder/Launch Services notice changes to a bundle rebuilt in place.
touch "$DEST"
if [ "${1:-}" != "--build-only" ]; then
  /usr/bin/open "$DEST"
fi
