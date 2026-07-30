#!/usr/bin/env bash
# Neon Genie — Hermes Skill Installer
# Usage:
#   ./install.sh                 # installs to ~/.hermes/skills/neon-genie

set -euo pipefail

TARGET_BASE="${HOME}/.hermes/skills"
DEST="${TARGET_BASE}/neon-genie"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing Neon Genie to: ${DEST}"
mkdir -p "${TARGET_BASE}"

if [ -d "${DEST}" ]; then
  echo "Existing installation found. Backing up to ${DEST}.bak"
  rm -rf "${DEST}.bak"
  mv "${DEST}" "${DEST}.bak"
fi

mkdir -p "${DEST}"
# Copy skill tree; exclude VCS metadata
if command -v rsync >/dev/null 2>&1; then
  rsync -a --exclude '.git' --exclude '.gitignore' "${ROOT}/" "${DEST}/"
else
  tar -C "${ROOT}" --exclude '.git' -cf - . | tar -C "${DEST}" -xf -
fi

chmod +x "${DEST}/scripts/"*.py 2>/dev/null || true
chmod +x "${DEST}/install.sh" 2>/dev/null || true

echo ""
echo "Neon Genie installed successfully."
echo "Location: ${DEST}"
echo ""
echo "Next steps:"
echo "  1. Restart Hermes or reload skills."
echo "  2. Validate: python ${DEST}/scripts/validate_hermes_skill.py"
echo "  3. Try triggers like: 'product audit', 'zero option', 'wayfinder handoff'"
echo ""
echo "The skill works standalone inside Hermes (advisory only)."
