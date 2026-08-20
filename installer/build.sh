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

# Version: the latest git tag is the single source of truth, so a release and
# the version the updater compares against can never drift. Override with
# VERSION=1.2.3 for a test build off an untagged tree.
PB=/usr/libexec/PlistBuddy
VERSION="${VERSION:-$(git describe --tags --abbrev=0 2>/dev/null | sed 's/^v//' || true)}"
if [ -z "$VERSION" ]; then
  VERSION=$($PB -c "Print :CFBundleShortVersionString" "$APP/Contents/Info.plist")
  echo "warning: no git tag found — falling back to Info.plist version $VERSION" >&2
fi
BUILD_NUM="$(git rev-list --count HEAD 2>/dev/null || echo 1)"
$PB -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist"
$PB -c "Set :CFBundleVersion $BUILD_NUM" "$APP/Contents/Info.plist"
echo "version: $VERSION (build $BUILD_NUM)"

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
  $PB -c "Set :CFBundleExecutable launcher" "$APP/Contents/Info.plist"
  $PB -c "Add :LSUIElement bool true" "$APP/Contents/Info.plist"
  echo "shell: browser fallback (install Xcode Command Line Tools for the native window)"
fi

# payload: everything the app installs on first launch
cp claude_hooks/hue_hook.py claude_hooks/hue_hook.sh claude_hooks/hue_off.sh \
   "$APP/Contents/Resources/payload/claude_hooks/"
cp dashboard.py "$APP/Contents/Resources/payload/"
cp -R static/. "$APP/Contents/Resources/payload/static/"

# icon: installer/icon.png is the artwork (1024x1024 RGBA, transparent outside
# the squircle). gen_icon.py draws a stand-in if the asset is ever missing.
ICON_PNG=$(mktemp -t claude_hue_icon).png
ICONSET=$(mktemp -d -t ClaudeHue).iconset
mkdir -p "$ICONSET"
if [ -f installer/icon.png ]; then
  cp installer/icon.png "$ICON_PNG"
else
  echo "warning: installer/icon.png missing — drawing the fallback icon" >&2
  python3 installer/gen_icon.py "$ICON_PNG" 1024 >/dev/null
fi
for s in 16 32 128 256 512; do
  sips -z $s $s "$ICON_PNG" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d "$ICON_PNG" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# Signature. A Developer ID identity plus the hardened runtime and a secure
# timestamp is what notarization requires, and what lets someone else's Mac
# open the app without the "damaged" scare. Falling back to ad-hoc keeps local
# builds working (Apple silicon kills a wholly unsigned bundle), but an ad-hoc
# app cannot be notarized and will be quarantined after download.
SIGN_ID="${CODESIGN_ID:-$(security find-identity -v -p codesigning 2>/dev/null \
  | awk -F'"' '/Developer ID Application/ {print $2; exit}')}"
if [ -n "$SIGN_ID" ]; then
  codesign --force --options runtime --timestamp --sign "$SIGN_ID" "$APP"
  echo "signed: $SIGN_ID"
else
  codesign --force --sign - "$APP" 2>/dev/null \
    && echo "signed: ad-hoc (no Developer ID identity — not distributable)" \
    || echo "warning: ad-hoc signing failed" >&2
fi

# dmg with the classic drag-to-Applications layout
DMGROOT="dist/dmg"
mkdir -p "$DMGROOT"
cp -R "$APP" "$DMGROOT/"
ln -s /Applications "$DMGROOT/Applications"
hdiutil create -volname "Claude Hue" -srcfolder "$DMGROOT" -ov -format UDZO \
  dist/ClaudeHue.dmg >/dev/null
rm -rf "$DMGROOT"

# The disk image needs its own signature, not just the app inside it: Gatekeeper
# evaluates the container it was handed, and an unsigned .dmg reads as
# "no usable signature" however well-notarized its contents are.
if [ -n "$SIGN_ID" ]; then
  codesign --force --timestamp --sign "$SIGN_ID" dist/ClaudeHue.dmg
  echo "signed: disk image"
fi

echo "built: $APP"
echo "built: dist/ClaudeHue.dmg"
