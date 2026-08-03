#!/usr/bin/env bash
# Fixed sync-process gate: flags any "Hermes Agent" / "Nous Research" /
# nousresearch.com hit across the WHOLE repo that isn't already accounted for
# in scripts/brand-audit-baseline.txt.
#
# See REBRAND-EXCEPTIONS.md for the textual rationale behind every preserved
# pattern (Nous Portal factual attribution, Discord replacement text, etc.) --
# this script is its machine-readable enforcement, not a replacement for it.
#
# Scales with the size of the CHANGE, not the size of the repo: known hits
# (whether legitimately preserved, or accepted pre-existing code debt deferred
# to a future rebrand phase) are baselined once and never re-flagged. Only
# genuinely new hits -- e.g. a new file from an upstream sync shipping
# unrebranded "Hermes Agent" prose -- fail the gate.
#
# Exit 0: no new/unclassified hits (safe to commit).
# Exit 1: new/unclassified hits found, printed to stderr. For each one: either
# fix it, or -- if it's a legitimate preserve/deferred case -- add it to
# REBRAND-EXCEPTIONS.md and re-run with --update-baseline to accept it.
#
# Usage:
#   ./scripts/audit-brand-residue.sh                    # gate mode
#   ./scripts/audit-brand-residue.sh --update-baseline  # accept current state
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

BASELINE="scripts/brand-audit-baseline.txt"

raw_hits() {
  grep -rn "Hermes Agent\|Nous Research\|nousresearch\.com" \
    --include="*.py" --include="*.md" --include="*.mdx" --include="*.yaml" \
    --include="*.yml" --include="*.ts" --include="*.tsx" --include="*.json" . \
    2>/dev/null \
    | grep -v "_OFFICIAL_REPO_CANONICAL\|LICENSE\|\.venv\|node_modules\|README.md" \
    | sed -E 's/^(\.\/)?([^:]+):[0-9]+:/\2:/'
}

if [ "${1:-}" = "--update-baseline" ]; then
  raw_hits | sort -u > "$BASELINE"
  echo "Baseline actualizada: $(wc -l < "$BASELINE" | tr -d ' ') líneas conocidas."
  exit 0
fi

if [ ! -f "$BASELINE" ]; then
  echo "No existe $BASELINE. Corre '$0 --update-baseline' primero." >&2
  exit 1
fi

new_hits="$(comm -23 <(raw_hits | sort -u) <(sort -u "$BASELINE") || true)"

if [ -n "$new_hits" ]; then
  echo "AUDITORIA DE MARCA: hits NUEVOS sin clasificar (no estan en $BASELINE):" >&2
  echo "" >&2
  echo "$new_hits" >&2
  echo "" >&2
  echo "Para cada uno: corrigelo, o si es un caso legitimo de preservar/deuda" >&2
  echo "de codigo diferida, documentalo en REBRAND-EXCEPTIONS.md y corre" >&2
  echo "'$0 --update-baseline' para aceptarlo. No comitear con hits sin clasificar." >&2
  exit 1
fi

echo "Auditoria de marca limpia: sin hits nuevos respecto a la baseline ($(wc -l < "$BASELINE" | tr -d ' ') lineas conocidas)."
exit 0
