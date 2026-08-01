#!/bin/bash
# SessionStart hook: make codebase-memory-mcp work in remote (Claude Code on
# the web) containers.
#
# The repo's .claude/settings.json launches the codebase-memory MCP server
# from /Users/danizhaky/.local/bin/codebase-memory-mcp — a path that exists on
# the Mac (installed by dotfiles) but not in a fresh remote container, so the
# server silently never started there. This hook downloads the same pinned,
# checksum-verified release the dotfiles installer uses, satisfies the
# hardcoded path with a symlink, and installs the repo-local cbm-* agent
# hooks into ~/.claude/hooks (where this repo's hook config expects them).
#
# Remote-only (no-op on the Mac, where dotfiles owns the install), idempotent,
# and soft-fail: a download failure degrades to today's behavior (no
# codebase-memory) rather than blocking the session.
set -uo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

CBM_VERSION="0.9.0"
CBM_BIN="$HOME/.local/bin/codebase-memory-mcp"
# Path hardcoded in .claude/settings.json (mcpServers) and the cbm-* hooks.
MAC_BIN_DIR="/Users/danizhaky/.local/bin"

# ---------------------------------------------------------------- binary
if [ ! -x "$CBM_BIN" ]; then
  arch="$(uname -m)"
  case "$arch" in
    x86_64) arch=amd64 ;;
    aarch64) arch=arm64 ;;
    *) echo "cbm session-start: unsupported arch $arch — skipping" >&2; exit 0 ;;
  esac
  asset="codebase-memory-mcp-linux-${arch}.tar.gz"
  base="https://github.com/DeusData/codebase-memory-mcp/releases/download/v${CBM_VERSION}"

  tmp="$(mktemp -d)" || exit 0
  trap 'rm -rf "$tmp"' EXIT

  if ! curl -fsSL -o "$tmp/$asset" "$base/$asset" \
     || ! curl -fsSL -o "$tmp/checksums.txt" "$base/checksums.txt"; then
    echo "cbm session-start: download failed — continuing without codebase-memory" >&2
    exit 0
  fi
  if ! (cd "$tmp" && grep " ${asset}\$" checksums.txt | sha256sum -c - >/dev/null 2>&1); then
    echo "cbm session-start: checksum verification FAILED — not installing" >&2
    exit 0
  fi
  tar xzf "$tmp/$asset" -C "$tmp" || exit 0
  mkdir -p "$(dirname "$CBM_BIN")"
  install -m 0755 "$tmp/codebase-memory-mcp" "$CBM_BIN" || exit 0
fi

# Satisfy the Mac-absolute path the checked-in config references.
if [ ! -x "$MAC_BIN_DIR/codebase-memory-mcp" ]; then
  mkdir -p "$MAC_BIN_DIR" 2>/dev/null \
    && ln -sf "$CBM_BIN" "$MAC_BIN_DIR/codebase-memory-mcp" \
    || echo "cbm session-start: could not create $MAC_BIN_DIR symlink" >&2
fi

# ------------------------------------------------- agent hooks (~/.claude)
# The repo's hook config invokes ~/.claude/hooks/cbm-* (dotfiles-installed on
# the Mac). Install the repo-local copies so those hooks work remotely too.
HOOKS_SRC="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}/.claude/hooks"
mkdir -p "$HOME/.claude/hooks"
for h in cbm-code-discovery-gate cbm-session-reminder; do
  if [ -f "$HOOKS_SRC/$h" ] && [ ! -f "$HOME/.claude/hooks/$h" ]; then
    install -m 0755 "$HOOKS_SRC/$h" "$HOME/.claude/hooks/$h" 2>/dev/null || true
  fi
done

echo "cbm session-start: codebase-memory-mcp ready ($("$CBM_BIN" --version 2>/dev/null || echo unknown))"
exit 0
