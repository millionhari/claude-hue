#!/bin/bash
# Cut a Claude Hue release: tag → build → sign → notarize → staple → publish.
#
#   installer/release.sh 1.2.0        # tag v1.2.0 and release it
#   installer/release.sh              # re-release the current tag
#
# One-time setup (stores an app-specific password in the keychain — run it
# yourself, it takes a secret):
#
#   xcrun notarytool store-credentials claude-hue \
#     --apple-id <your-apple-id> --team-id 59G3A9CB35 --password <app-specific-password>
#
# App-specific passwords come from appleid.apple.com → Sign-In and Security.
set -euo pipefail
cd "$(dirname "$0")/.."

NOTARY_PROFILE="${NOTARY_PROFILE:-claude-hue}"
REPO="${REPO:-millionhari/claude-hue}"

die() { echo "error: $*" >&2; exit 1; }

# ---- preflight: fail before doing anything half-way -------------------------
command -v gh   >/dev/null || die "gh CLI not found (brew install gh)"
command -v xcrun >/dev/null || die "xcrun not found (install Xcode command line tools)"
gh auth status >/dev/null 2>&1 || die "gh is not authenticated (gh auth login)"
[ -z "$(git status --porcelain)" ] || die "working tree is dirty — commit or stash first"

security find-identity -v -p codesigning 2>/dev/null | grep -q "Developer ID Application" \
  || die "no Developer ID Application identity in the keychain"

xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" >/dev/null 2>&1 \
  || die "no notarytool credentials for profile '$NOTARY_PROFILE' — see the header of this script"

# ---- version / tag ----------------------------------------------------------
if [ $# -ge 1 ]; then
  VERSION="${1#v}"
  TAG="v$VERSION"
  if git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "tag $TAG already exists — reusing it"
  else
    git tag -a "$TAG" -m "Claude Hue $VERSION"
    echo "tagged $TAG"
  fi
else
  TAG=$(git describe --tags --abbrev=0 2>/dev/null) || die "no tags yet — pass a version, e.g. release.sh 1.2.0"
  VERSION="${TAG#v}"
  echo "reusing current tag $TAG"
fi

git push origin "$TAG"

# ---- build (build.sh reads the tag for the version it stamps) ---------------
bash installer/build.sh

DMG="dist/ClaudeHue.dmg"
APP="dist/Claude Hue.app"
codesign --verify --strict "$APP" || die "the built app failed signature verification"
codesign -dv --verbose=4 "$APP" 2>&1 | grep -q "TeamIdentifier=" \
  || die "the built app is ad-hoc signed — it cannot be notarized"

# ---- notarize + staple ------------------------------------------------------
echo "notarizing $DMG (this usually takes a couple of minutes)…"
xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait \
  || die "notarization failed — 'xcrun notarytool log <id> --keychain-profile $NOTARY_PROFILE' has the detail"
xcrun stapler staple "$DMG"
spctl -a -t open --context context:primary-signature -v "$DMG" \
  || die "Gatekeeper rejected the stapled disk image"
echo "notarized and stapled"

# ---- publish ----------------------------------------------------------------
# The app's updater looks for the newest release with a .dmg asset, so the
# upload below is what actually ships the update to installed copies.
if gh release view "$TAG" -R "$REPO" >/dev/null 2>&1; then
  gh release upload "$TAG" "$DMG" -R "$REPO" --clobber
  echo "updated existing release $TAG"
else
  gh release create "$TAG" "$DMG" -R "$REPO" \
    --title "Claude Hue $VERSION" --generate-notes
  echo "published release $TAG"
fi

echo
echo "released: $(gh release view "$TAG" -R "$REPO" --json url -q .url)"
