#!/bin/bash
# bootstrap.sh — Reprovision machine from scratch
# Run after: chezmoi init <your-github-username> --apply
#
# This script installs all packages and services needed on a fresh CachyOS install.
# Designed to be idempotent — safe to re-run.

set -euo pipefail

echo "=== CachyOS Bootstrap ==="
echo "Starting at $(date)"
echo ""

# 1. Install packages from saved package list
PKG_LIST="$HOME/.local/share/chezmoi/scripts/pkglist.txt"
if [ -f "$PKG_LIST" ]; then
  echo "[1/5] Installing packages from pkglist.txt..."
  sudo pacman -S --needed --noconfirm - < "$PKG_LIST"
else
  echo "[1/5] No pkglist.txt found — skipping package installation"
fi

# 2. Install AUR packages
AUR_LIST="$HOME/.local/share/chezmoi/scripts/aurlist.txt"
if [ -f "$AUR_LIST" ]; then
  echo "[2/5] Installing AUR packages..."
  # Requires an AUR helper (paru/yay). Install paru if missing.
  if ! command -v paru &>/dev/null; then
    echo "  Installing paru..."
    sudo pacman -S --needed --noconfirm base-devel
    tmpdir=$(mktemp -d)
    git clone https://aur.archlinux.org/paru.git "$tmpdir/paru"
    (cd "$tmpdir/paru" && makepkg -si --noconfirm)
    rm -rf "$tmpdir"
  fi
  paru -S --needed --noconfirm - < "$AUR_LIST"
else
  echo "[2/5] No aurlist.txt found — skipping AUR installation"
fi

# 3. Install Hermes Agent
echo "[3/5] Installing Hermes Agent..."
if ! command -v hermes &>/dev/null; then
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
else
  echo "  Hermes already installed — skipping"
fi

# 4. Install Infisical CLI
echo "[4/5] Installing Infisical CLI..."
if ! command -v infisical &>/dev/null; then
  npm install -g @infisical/cli
else
  echo "  Infisical CLI already installed — skipping"
fi

# 5. Enable system services
echo "[5/5] Enabling services..."
# Add your systemd services here:
# sudo systemctl enable --now ipu6-webcam-bridge.service

echo ""
echo "=== Bootstrap complete ==="
echo "Next steps:"
echo "  1. infisical login           # Login to Infisical cloud"
echo "  2. infisical init            # Connect this directory to your project (creates .infisical.json)"
echo "  3. infisical secrets set HERMES_CUSTOM_API_AIAND_COM_API_KEY=\"...\" --env=dev"
echo "  4. infisical secrets set SUDO_PASSWORD=\"...\" --env=dev"
echo "  5. infisical secrets set EXA_API_KEY=\"...\" --env=dev"
echo "  6. hermes-sec                # Launch Hermes with secrets injected"
echo ""
echo "Done at $(date)"
