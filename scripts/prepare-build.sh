#!/bin/bash
# prepare-build.sh — 在 WSL 中预生成 git 信息，供 build.ps1 读取
# 用法: cd ~/clawd/qiji-fork && bash scripts/prepare-build.sh

set -e
REPO="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
BUILD_DIR="$REPO/.build"

mkdir -p "$BUILD_DIR"

echo "=== 预生成 git 信息 (build.ps1 需要) ==="
echo "仓库: $REPO"

# git status
git -C "$REPO" status --short > "$BUILD_DIR/git-status.txt" 2>&1
echo "  git-status.txt: $(wc -l < "$BUILD_DIR/git-status.txt") 行"

# git SHA
git -C "$REPO" rev-parse HEAD > "$BUILD_DIR/git-sha.txt" 2>&1
echo "  git-sha.txt: $(cat "$BUILD_DIR/git-sha.txt" | head -c 12)..."

echo ""
echo "完成。现在可以在 Windows PowerShell 中运行: .\\build.ps1"
