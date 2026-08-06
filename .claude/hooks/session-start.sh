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

# Expected sha256 of the v0.9.0 release tarballs, pinned HERE rather than read
# from the release's own checksums.txt.
#
# Fetching the checksum from the same release as the artifact proves the
# download wasn't corrupted in transit; it proves nothing about the publisher.
# Anyone who can alter the release alters both files together and verification
# still passes. A hash committed in this repo is an independent reference: it
# fails closed if the tag is ever re-pointed or the asset re-uploaded. That
# matters more than usual here because cbm-code-discovery-gate executes this
# binary on every PreToolUse once the hook is registered.
#
# Same pattern as this repo's gitleaks workflow (pinned by version AND tarball
# sha256). Values below were computed by downloading each artifact and running
# sha256sum locally, not transcribed from checksums.txt.
#
# To bump: change CBM_VERSION, download both tarballs, sha256sum them, paste.
CBM_SHA256_amd64="e2832a8d207c26beaa30efa6222ed4a37cb3f526ca4bee060bfbf336ed6fc679"
CBM_SHA256_arm64="68a345d9a6842f02a3cb07e187b28bc38c4f3a22967f47fadbcd0757ba93a680"

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

  # Resolve the pinned hash for this arch. An arch with no pin is a hard stop,
  # not a fall-through to unverified install.
  eval "expected=\${CBM_SHA256_${arch}:-}"
  if [ -z "$expected" ]; then
    echo "cbm session-start: no pinned sha256 for $arch — not installing" >&2
    exit 0
  fi

  if ! curl -fsSL -o "$tmp/$asset" "$base/$asset"; then
    echo "cbm session-start: download failed — continuing without codebase-memory" >&2
    exit 0
  fi
  actual="$(sha256sum "$tmp/$asset" | cut -d' ' -f1)"
  if [ "$actual" != "$expected" ]; then
    echo "cbm session-start: checksum verification FAILED — not installing" >&2
    echo "  expected $expected" >&2
    echo "  actual   $actual" >&2
    echo "  The pinned hash is in .claude/hooks/session-start.sh. A mismatch means" >&2
    echo "  the release asset changed since it was pinned — verify before bumping." >&2
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
