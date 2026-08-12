#!/usr/bin/env bash
set -Eeuo pipefail

export TERMUX_VERSION="ci"
export HERMES_REPO_URL="file:///workspace"
export HERMES_HOME="$HOME/.hermes-ci"
export HERMES_INSTALL_DIR="$HOME/hermes-agent-ci"
# The bootstrap image intentionally has no git yet; Hermes must install it.
# Pre-authorize the read-only mounted PR checkout through Git environment
# configuration so the regression wrapper never calls git before install.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0=/workspace

echo "== native installer =="
bash /workspace/scripts/install-termux.sh \
  --branch termux-ci \
  --dir "$HERMES_INSTALL_DIR" \
  --hermes-home "$HERMES_HOME" \
  --skip-setup \
  --skip-browser \
  --no-skills \
  --non-interactive

echo "== CLI smoke =="
hermes --version
hermes --help >/dev/null
"$HERMES_INSTALL_DIR/venv/bin/python" - <<'PY'
from hermes_cli.main import _is_termux_startup_environment
from hermes_cli.termux_desktop import chromium_browser_spec, TermuxDesktopRuntime

assert _is_termux_startup_environment(), "Termux startup detection is false in native image"
spec = chromium_browser_spec(
    TermuxDesktopRuntime(
        browser="/data/data/com.termux/files/usr/bin/chromium-browser",
        display=":1",
        x11="/data/data/com.termux/files/usr/bin/termux-x11",
    )
)
assert "--app=%s" in spec
assert "--no-sandbox" not in spec
print("native-termux-runtime-contract-ok")
PY

echo "== Desktop renderer build (must not execute Electron) =="
hermes desktop --build-only
test -f "$HERMES_INSTALL_DIR/apps/desktop/dist/index.html"

echo "== TUI install/build path =="
"$HERMES_INSTALL_DIR/venv/bin/python" - <<'PY'
from pathlib import Path
from hermes_cli.main import PROJECT_ROOT, _make_tui_argv

argv, cwd = _make_tui_argv(PROJECT_ROOT / "ui-tui", False)
entry = Path(argv[-1])
assert entry.is_file(), entry
assert "ui-tui" in str(entry) or "tui_dist" in str(entry)
print(f"native-tui-entry={entry}")
print(f"native-tui-cwd={cwd}")
PY

echo "== frontend type/regression tests =="
cd "$HERMES_INSTALL_DIR"
npm run build:ink --workspace ui-tui
npm run typecheck --workspace ui-tui
npm exec --workspace ui-tui -- vitest run src/__tests__/termuxComposerLayout.test.ts
echo "== real narrow PTY TUI smoke =="
"$HERMES_INSTALL_DIR/venv/bin/python" scripts/smoke-termux-tui.py --cols 48 --rows 18
npm run typecheck --workspace apps/desktop
npm exec --workspace apps/desktop -- vitest run \
  src/lib/browser-desktop-bridge.test.ts \
  src/lib/local-preview.test.ts \
  src/lib/desktop-fs.test.ts \
  --project ui

echo "== Termux:X11 package availability =="
pkg install -y x11-repo >/dev/null
pkg install -y termux-x11-nightly chromium >/dev/null
apt-cache show termux-x11-nightly >/dev/null
apt-cache show chromium >/dev/null

echo "== browser-hosted Desktop loopback smoke =="
export HERMES_WEB_DIST="$HERMES_INSTALL_DIR/apps/desktop/dist"
unset HERMES_SERVE_HEADLESS || true
"$HERMES_INSTALL_DIR/venv/bin/python" -c \
  'from hermes_cli.web_server import start_server; start_server(host="127.0.0.1", port=9127, open_browser=False)' \
  >"$TMPDIR/hermes-desktop-browser-smoke.log" 2>&1 &
server_pid=$!
cleanup_browser_smoke() { kill "$server_pid" >/dev/null 2>&1 || true; }
trap cleanup_browser_smoke EXIT
for _attempt in $(seq 1 60); do
  if curl --fail --silent --show-error http://127.0.0.1:9127/ >/dev/null; then
    break
  fi
  if ! kill -0 "$server_pid" >/dev/null 2>&1; then
    cat "$TMPDIR/hermes-desktop-browser-smoke.log" >&2
    exit 1
  fi
  sleep 0.5
done
curl --fail --silent --show-error http://127.0.0.1:9127/ >/dev/null
browser_exe="$(command -v chromium-browser || command -v chromium)"
HERMES_BROWSER_EXECUTABLE="$browser_exe" \
  HERMES_BROWSER_HOST_URL="http://127.0.0.1:9127/" \
  HERMES_BROWSER_REQUIRE_TERMINAL=1 \
  npm run smoke:browser-host --workspace apps/desktop
cleanup_browser_smoke
trap - EXIT

echo "native-termux-regression-ok"
