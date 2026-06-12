#!/bin/bash
# Build "Claude Hue.app" and ClaudeHue.dmg into dist/.
set -euo pipefail
cd "$(dirname "$0")/.."

APP="dist/Claude Hue.app"
rm -rf dist
mkdir -p "$APP/Contents/MacOS" \
         "$APP/Contents/Resources/payload/claude_hooks" \
         "$APP/Contents/Resources/payload/static"

# bundle skeleton
cp installer/Info.plist "$APP/Contents/Info.plist"
cp installer/launcher "$APP/Contents/MacOS/launcher"
chmod +x "$APP/Contents/MacOS/launcher"
cp installer/claude_hue_app.py "$APP/Contents/Resources/"

# payload: everything the app installs on first launch
cp claude_hooks/hue_hook.py claude_hooks/hue_hook.sh claude_hooks/hue_off.sh \
   "$APP/Contents/Resources/payload/claude_hooks/"
cp dashboard.py "$APP/Contents/Resources/payload/"
cp static/index.html "$APP/Contents/Resources/payload/static/"

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
