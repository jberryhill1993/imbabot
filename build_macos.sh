#!/usr/bin/env bash
# Build Jbot.app on macOS and package it into release/Jbot-macOS-arm64.zip
#
# Usage (from the project folder):
#   ./build_macos.sh
#
# Builds in a temp dir to avoid Finder/Spotlight re-tagging the bundle with
# com.apple.FinderInfo (which breaks code-signing), then ad-hoc signs and zips.
set -euo pipefail

PROJ="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-$PROJ/.venv/bin/python}"
BUILD="$(mktemp -d /tmp/imbabot-build.XXXXXX)"
DIST="$(mktemp -d /tmp/imbabot-dist.XXXXXX)"

echo "==> Using Python: $PY"
if [ ! -x "$PY" ]; then
  echo "Creating venv (.venv) ..."
  python3 -m venv "$PROJ/.venv"
  PY="$PROJ/.venv/bin/python"
  "$PY" -m pip install --upgrade pip
fi

echo "==> Installing dependencies ..."
"$PY" -m pip install -r "$PROJ/requirements.txt" pyinstaller >/dev/null

echo "==> Running self-test ..."
"$PY" -m imbabot.cli selftest >/dev/null && echo "    self-test OK"

echo "==> Building Jbot.app (in $DIST) ..."
( cd "$PROJ" && "$PY" -m PyInstaller imbabot.spec --noconfirm \
    --distpath "$DIST" --workpath "$BUILD" --log-level ERROR )

APP="$DIST/Jbot.app"
echo "==> Cleaning extended attributes + ad-hoc signing ..."
xattr -cr "$APP" || true
xattr -d com.apple.FinderInfo "$APP" 2>/dev/null || true
xattr -d com.apple.FinderInfo "$APP/Contents/Frameworks/Python3.framework" 2>/dev/null || true
codesign --force --deep -s - "$APP"
codesign --verify --strict "$APP" && echo "    signature OK"

mkdir -p "$PROJ/release"
ZIP="$PROJ/release/Jbot-macOS-arm64.zip"
echo "==> Packaging -> $ZIP"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

echo ""
echo "Done."
echo "  App: $APP"
echo "  Zip: $ZIP"
echo ""
echo "First launch on another Mac (unsigned/ad-hoc): right-click the app -> Open,"
echo "or run:  xattr -dr com.apple.quarantine /path/to/Jbot.app"
