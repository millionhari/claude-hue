#!/bin/bash
# Build "Claude Hue.app" and ClaudeHue.dmg into dist/.
#
# The app is a native AppKit + WKWebView window around the local dashboard
# (installer/ClaudeHueShell.swift). Without swiftc the build falls back to the
# shell launcher, which opens the dashboard in a chromeless browser window.
set -euo pipefail
cd "$(dirname "$0")/.."

APP="dist/Claude Hue.app"
rm -rf dist
mkdir -p "$APP/Contents/MacOS" \
         "$APP/Contents/Resources/payload/claude_hooks" \
         "$APP/Contents/Resources/payload/static"

# bundle skeleton
cp installer/Info.plist "$APP/Contents/Info.plist"
cp installer/claude_hue_app.py "$APP/Contents/Resources/"

# native window shell: universal when both slices compile, else this machine's
build_slice() {  # <target> <out>
  swiftc -O -target "$1" -o "$2" "$BUILD/main.swift" 2>/dev/null
}
if command -v swiftc >/dev/null 2>&1; then
  BUILD=$(mktemp -d -t claude-hue-build)
  cp installer/ClaudeHueShell.swift "$BUILD/main.swift"   # top-level code needs main.swift
  SLICES=()
  for pair in "arm64-apple-macos11.0 $BUILD/arm64" "x86_64-apple-macos11.0 $BUILD/x86_64"; do
    # shellcheck disable=SC2086
    if build_slice $pair; then SLICES+=("${pair#* }"); fi
  done
  if [ ${#SLICES[@]} -eq 0 ]; then
    echo "error: swiftc could not build the app shell" >&2
    exit 1
  fi
  lipo -create "${SLICES[@]}" -output "$APP/Contents/MacOS/ClaudeHue"
  chmod +x "$APP/Contents/MacOS/ClaudeHue"
  echo "shell: native window ($(lipo -archs "$APP/Contents/MacOS/ClaudeHue"))"
  rm -rf "$BUILD"
else
  # No Swift toolchain: agent app that opens a chromeless browser window.
  cp installer/launcher "$APP/Contents/MacOS/launcher"
  chmod +x "$APP/Contents/MacOS/launcher"
  PB=/usr/libexec/PlistBuddy
  $PB -c "Set :CFBundleExecutable launcher" "$APP/Contents/Info.plist"
  $PB -c "Add :LSUIElement bool true" "$APP/Contents/Info.plist"
  echo "shell: browser fallback (install Xcode Command Line Tools for the native window)"
fi

# payload: everything the app installs on first launch
cp claude_hooks/hue_hook.py claude_hooks/hue_hook.sh claude_hooks/hue_off.sh \
   "$APP/Contents/Resources/payload/claude_hooks/"
cp dashboard.py "$APP/Contents/Resources/payload/"
cp -R static/. "$APP/Contents/Resources/payload/static/"

# icon: render once, downscale into an iconset, compile to icns
ICON_PNG=$(mktemp -t claude_hue_icon).png
ICONSET=$(mktemp -d -t ClaudeHue).iconset
mkdir -p "$ICONSET"
python3 installer/gen_icon.py "$ICON_PNG" 1024 >/dev/null
for s in 16 32 128 256 512; do
  sips -z $s $s "$ICON_PNG" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d "$ICON_PNG" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# ad-hoc signature: unsigned bundles get killed on Apple silicon
codesign --force --deep --sign - "$APP" 2>/dev/null \
  && echo "signed: ad-hoc" || echo "warning: ad-hoc signing failed" >&2

# dmg with the classic drag-to-Applications layout
DMGROOT="dist/dmg"
mkdir -p "$DMGROOT"
cp -R "$APP" "$DMGROOT/"
ln -s /Applications "$DMGROOT/Applications"
hdiutil create -volname "Claude Hue" -srcfolder "$DMGROOT" -ov -format UDZO \
  dist/ClaudeHue.dmg >/dev/null
rm -rf "$DMGROOT"

echo "built: $APP"
echo "built: dist/ClaudeHue.dmg"
