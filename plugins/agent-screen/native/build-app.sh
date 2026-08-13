#!/bin/bash
# build-app.sh — baut die Agent-Screen-App als signiertes .app-Bundle.
# Ein Befehl, alles drin: kompilieren → Bundle → .icns → signieren → prüfen.
#
# Nutzung:  ./build-app.sh            (alles bauen)
#           ./build-app.sh --check    (nur Bundle/Signatur/Icon prüfen)
#
# Voraussetzungen:
#   - Zertifikat "Agent Screen Dev" im Login-Keychain (NIE ad-hoc!)
#   - icon/agent-screen-icon-final.png (1024²) als Icon-Quelle
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${AGENT_SCREEN_DIR:-$HOME/.hermes/agent-screen}"
APP_DIR="$INSTALL_DIR/app"
ICON_DIR="$PROJECT_DIR/icon"
BUNDLE="$APP_DIR/Agent Screen.app"
BINARY="$APP_DIR/agent-screen-app"
SOURCE="$PROJECT_DIR/agent-screen-app.swift"
HEADER="$PROJECT_DIR/CGVirtualDisplayPrivate.h"
ICNS="$ICON_DIR/AgentScreen.icns"
ICON_SRC="$ICON_DIR/agent-screen-icon-final.png"
CERT="Agent Screen Dev"
IDENTIFIER="com.agent.screen"

cd "$PROJECT_DIR"

# ---------------------------------------------------------------- helpers
say()  { printf '\033[1;36m[build]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[build]\033[0m FEHLER: %s\n' "$*" >&2; exit 1; }

check() {
  [ -d "$BUNDLE" ]                       || die "Bundle fehlt: $BUNDLE"
  [ -x "$BUNDLE/Contents/MacOS/agent-screen-app" ] || die "Binary im Bundle fehlt"
  [ -f "$BUNDLE/Contents/Resources/AgentScreen.icns" ] || die ".icns im Bundle fehlt"
  codesign --verify --deep "$BUNDLE" 2>/dev/null || die "Signatur ungültig (codesign --verify)"
  echo "Bundle + Signatur OK: $BUNDLE"
  codesign -dv "$BUNDLE" 2>&1 | grep -E "Identifier|Authority" | sed 's/^/  /'
}

build_icns() {
  if [ ! -f "$ICON_SRC" ]; then
    say "Kein $ICON_SRC — überspringe .icns-Build"
    return
  fi
  say "Baue AgentScreen.icns aus $ICON_SRC"
  rm -rf "$ICON_DIR/AgentScreen.iconset"
  mkdir -p "$ICON_DIR/AgentScreen.iconset"
  # iconset-Größen (16…1024 inkl. @2x-Stufen) — python3/PIL, sonst sips
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
  say "AgentScreen.icns gebaut ($(stat -f%z "$ICNS") Bytes)"
}

# ------------------------------------------------------------------ main
if [ "${1:-}" = "--check" ]; then
  check
  exit 0
fi

[ -f "$SOURCE" ] || die "Quelle fehlt: $SOURCE"
[ -f "$HEADER" ] || die "Header fehlt: $HEADER"

# 1) Kompilieren (macOS-14-Target ist Pflicht für CGDisplayStream)
say "Kompiliere $SOURCE …"
swiftc -O -target arm64-apple-macos14.0 "$SOURCE" \
       -import-objc-header "$HEADER" -o "$BINARY"

# 2) In Bundle-Kopie legen
say "Kopiere Binary ins Bundle …"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cp "$BINARY" "$BUNDLE/Contents/MacOS/agent-screen-app"
chmod +x "$BUNDLE/Contents/MacOS/agent-screen-app"

# 3) Icon (bei Änderung der Quelle wird neu gebaut)
if [ ! -f "$ICNS" ] || [ "$ICON_SRC" -nt "$ICNS" ]; then
  build_icns
fi
cp "$ICNS" "$BUNDLE/Contents/Resources/AgentScreen.icns"

# 4) Signieren (Zertifikat — NIE ad-hoc, sonst verfällt der TCC-Grant)
say "Signiere mit '$CERT' …"
codesign --force --sign "$CERT" --timestamp=none "$BUNDLE"

# 5) LaunchServices frisch registrieren (Icon-Cache)
LSREG="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
"$LSREG" -f "$BUNDLE" >/dev/null 2>&1 || true

# 6) Verifizieren
say "Verifiziere …"
check
echo
say "Fertig. Start: AGENT_SCREEN_DIR=$INSTALL_DIR $PROJECT_DIR/agent-screen.sh"
say "HINWEIS: Zeigt das Dock nach einem Icon-Wechsel noch das alte Icon,"
say "den Dock-Icon-Cache leeren (Rezept im Skill 'computer-use'):"
say "  pkill -f agent-screen-app; killall IconServicesAgent Dock; sleep 2"
say '  find /var/folders -iname "*iconserv*" -exec rm -rf {} \;'
say '  find /var/folders -iname "*iconcache*" -exec rm -rf {} \;'
say "  killall IconServicesAgent Dock"
