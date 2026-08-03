#!/usr/bin/env bash
# Thin wrapper over scripts/iyari_transform.py for post-sync rebranding.
#
# Usage:
#   ./scripts/apply-iyari-rebrand.sh <dir> [<dir> ...]
#
# Finds every .md/.mdx/.yaml file under the given directories and applies
# the same 5-rule transformer used throughout GRUPO 5/6 (see CLAUDE.md).
# Idempotent: safe to re-run after every upstream sync.
#
# Runs with --skip-nous-research by default: "Nous Research" as factual
# attribution (Nous Portal ownership, lab-behind-Hermes mentions, etc.) is
# the common case across the doc tree and should default to preserved, not
# rewritten. The handful of identity-string exceptions (index.mdx hero,
# skill authorship strings, etc. -- see CLAUDE.md's GRUPO 5 lote notes for
# the exact list) still need the same manual pass done by hand every sync,
# same as GRUPO 5/6. This script does NOT replicate that manual pass.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "Usage: $0 <dir> [<dir> ...]" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

files=()
while IFS= read -r -d '' f; do
  files+=("$f")
done < <(find "$@" -type f \( -name "*.md" -o -name "*.mdx" -o -name "*.yaml" -o -name "*.yml" -o -name "*.json" \) -print0)

if [ "${#files[@]}" -eq 0 ]; then
  echo "No .md/.mdx/.yaml files found under: $*" >&2
  exit 0
fi

echo "Applying rebrand to ${#files[@]} files..."
python3 "$SCRIPT_DIR/iyari_transform.py" --skip-nous-research "${files[@]}"
