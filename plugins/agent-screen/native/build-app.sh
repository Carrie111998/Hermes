#!/bin/bash
# build-app.sh — compile + bundle + codesign Agent Screen.app.
#
# Usage:  ./build-app.sh            (build everything)
#         ./build-app.sh --check    (verify bundle + signature only)
#
# Requires:
#   - Xcode command line tools
#   - A codesigning identity named "Agent Screen Dev" in the login keychain
#     (never ad-hoc: Screen Recording TCC is bound to the signing identity).
#     Create it once: Keychain Access → Certificate Assistant → Create a
#     Certificate → name "Agent Screen Dev", identity type "Code Signing".
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${HOME}/.hermes/agent-screen"
APP_DIR="$INSTALL_DIR/app"
ICON_DIR="$PROJECT_DIR/icon"
BUNDLE="$APP_DIR/Agent Screen.app"
BINARY="$APP_DIR/agent-screen-app"
SOURCE="$PROJECT_DIR/agent-screen-app.swift"
HEADER="$PROJECT_DIR/CGVirtualDisplayPrivate.h"
ICNS="$ICON_DIR/AgentScreen.icns"
ICON_SRC="$ICON_DIR/agent-screen-icon-final.png"
CERT="Agent Screen Dev"
IDENTIFIER="ai.hermes.agent-screen"
PLIST="$BUNDLE/Contents/Info.plist"

cd "$PROJECT_DIR"

say()  { printf '\033[1;36m[build]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[build]\033[0m ERROR: %s\n' "$*" >&2; exit 1; }

ensure_cert() {
  # Self-signed identities often do not show up in `find-identity -v`
  # (Apple policy store). Presence of the cert is the real check; codesign
  # itself fails closed if the private key is missing.
  if security find-certificate -c "$CERT" >/dev/null 2>&1; then
    return 0
  fi
  cat >&2 <<EOF
[build] ERROR: codesigning identity '$CERT' not found.

Create it once (do not use ad-hoc 'codesign -s -' — Screen Recording TCC
is bound to the signing identity and is lost on every ad-hoc rebuild):

  1. Open Keychain Access
  2. Certificate Assistant → Create a Certificate...
  3. Name: $CERT
  4. Identity Type: Self Signed Root
  5. Certificate Type: Code Signing
  6. Check "Let me override defaults", continue, keep defaults

Then re-run ./build-app.sh
EOF
  exit 1
}

write_info_plist() {
  mkdir -p "$BUNDLE/Contents"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDevelopmentRegion</key>
	<string>en</string>
	<key>CFBundleDisplayName</key>
	<string>Agent Screen</string>
	<key>CFBundleExecutable</key>
	<string>agent-screen-app</string>
	<key>CFBundleIconFile</key>
	<string>AgentScreen</string>
	<key>CFBundleIdentifier</key>
	<string>${IDENTIFIER}</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleName</key>
	<string>Agent Screen</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>1.1</string>
	<key>CFBundleVersion</key>
	<string>3</string>
	<key>LSMinimumSystemVersion</key>
	<string>14.0</string>
	<key>LSUIElement</key>
	<false/>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>NSScreenCaptureUsageDescription</key>
	<string>Agent Screen needs Screen Recording to stream the virtual display into its window and the Hermes pane.</string>
	<key>NSAppleEventsUsageDescription</key>
	<string>Agent Screen uses Accessibility to move a window you drop onto it onto the virtual display.</string>
</dict>
</plist>
PLIST
}

check() {
  [ -d "$BUNDLE" ]                       || die "bundle missing: $BUNDLE"
  [ -x "$BUNDLE/Contents/MacOS/agent-screen-app" ] || die "binary missing inside bundle"
  [ -f "$PLIST" ]                        || die "Info.plist missing"
  grep -q "$IDENTIFIER" "$PLIST"         || die "Info.plist missing bundle id $IDENTIFIER"
  [ -f "$BUNDLE/Contents/Resources/AgentScreen.icns" ] || die ".icns missing inside bundle"
  codesign --verify --deep "$BUNDLE" 2>/dev/null || die "invalid signature (codesign --verify)"
  echo "Bundle + signature OK: $BUNDLE"
  codesign -dv "$BUNDLE" 2>&1 | grep -E "Identifier|Authority" | sed 's/^/  /'
}

build_icns() {
  if [ ! -f "$ICON_SRC" ]; then
    say "no $ICON_SRC — skipping .icns build"
    return
  fi
  say "building AgentScreen.icns from $ICON_SRC"
  rm -rf "$ICON_DIR/AgentScreen.iconset"
  mkdir -p "$ICON_DIR/AgentScreen.iconset"
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import PIL' 2>/dev/null; then
    python3 - "$ICON_SRC" "$ICON_DIR/AgentScreen.iconset" <<'PYEOF'
import sys
from PIL import Image
src, out = sys.argv[1], sys.argv[2]
img = Image.open(src).convert("RGB")
spec = {
    "icon_16x16.png": 16, "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32, "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128, "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256, "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512, "icon_512x512@2x.png": 1024,
}
for name, size in spec.items():
    img.resize((size, size), Image.LANCZOS).save(f"{out}/{name}")
PYEOF
  else
    for spec in "16 icon_16x16.png" "32 icon_16x16@2x.png" "32 icon_32x32.png" \
                "64 icon_32x32@2x.png" "128 icon_128x128.png" "256 icon_128x128@2x.png" \
                "256 icon_256x256.png" "512 icon_256x256@2x.png" \
                "512 icon_512x512.png" "1024 icon_512x512@2x.png"; do
      set -- $spec
      sips -z "$1" "$1" "$ICON_SRC" --out "$ICON_DIR/AgentScreen.iconset/$2" >/dev/null
    done
  fi
  iconutil -c icns "$ICON_DIR/AgentScreen.iconset" -o "$ICNS"
  rm -rf "$ICON_DIR/AgentScreen.iconset"
  say "AgentScreen.icns built ($(stat -f%z "$ICNS") bytes)"
}

compile_one() {
  local target="$1" dest="$2"
  swiftc -O -target "$target" "$SOURCE" -import-objc-header "$HEADER" -o "$dest"
}

if [ "${1:-}" = "--check" ]; then
  check
  exit 0
fi

[ -f "$SOURCE" ] || die "source missing: $SOURCE"
[ -f "$HEADER" ] || die "header missing: $HEADER"
ensure_cert

mkdir -p "$APP_DIR" "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

# 1) Compile. Prefer a universal binary; fall back to the host arch.
HOST_ARCH="$(uname -m)"
ARM_T="arm64-apple-macos14.0"
X86_T="x86_64-apple-macos14.0"
say "compiling ${SOURCE}..."
if [ "$HOST_ARCH" = "arm64" ]; then
  compile_one "$ARM_T" "$BINARY-arm64"
  if compile_one "$X86_T" "$BINARY-x86_64" 2>/tmp/agent-screen-x86-build.log; then
    lipo -create "$BINARY-arm64" "$BINARY-x86_64" -o "$BINARY"
    rm -f "$BINARY-arm64" "$BINARY-x86_64"
    say "universal binary (arm64 + x86_64)"
  else
    mv "$BINARY-arm64" "$BINARY"
    rm -f "$BINARY-x86_64"
    say "arm64-only (x86_64 cross-compile unavailable — Intel Macs need a rebuild)"
  fi
else
  compile_one "$X86_T" "$BINARY"
  say "x86_64 binary"
fi

# 2) Assemble the bundle
say "assembling bundle..."
cp "$BINARY" "$BUNDLE/Contents/MacOS/agent-screen-app"
chmod +x "$BUNDLE/Contents/MacOS/agent-screen-app"
write_info_plist

if [ ! -f "$ICNS" ] || [ "$ICON_SRC" -nt "$ICNS" ]; then
  build_icns
fi
if [ -f "$ICNS" ]; then
  cp "$ICNS" "$BUNDLE/Contents/Resources/AgentScreen.icns"
fi

# 3) Sign (named identity — never ad-hoc)
say "signing with '$CERT'..."
codesign --force --sign "$CERT" --timestamp=none "$BUNDLE"

# 4) Refresh Launch Services so the Dock/Finder pick up the icon + id
LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
"$LSREG" -f "$BUNDLE" >/dev/null 2>&1 || true

say "verifying..."
check
echo
say "done. start: $PROJECT_DIR/agent-screen.sh"
say "if the Dock shows a stale icon after an icon change: killall Dock"
