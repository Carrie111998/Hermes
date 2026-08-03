#!/usr/bin/env bash
# Diagnostico de deriva con upstream (NousResearch/hermes-agent). Solo lectura:
# hace git fetch y reporta cuanto nos hemos separado. NO mergea, NO comitea,
# NO toca el arbol de trabajo. La decision de sincronizar sigue siendo del
# usuario -- este script solo la informa.
#
# Pensado para correr semanalmente (ver CLAUDE.md, seccion "Cadencia de sync
# con upstream"): cuanto mas chico el diff, mas facil el sync manual de marca
# y codigo solapado que sigue necesitando ojos humanos.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

if ! git remote get-url upstream >/dev/null 2>&1; then
  echo "No hay remote 'upstream' configurado. Ejecuta:" >&2
  echo "  git remote add upstream https://github.com/NousResearch/hermes-agent.git" >&2
  exit 1
fi

git fetch upstream --quiet

BASE="$(git merge-base main upstream/main)"
COMMITS="$(git rev-list --count "${BASE}..upstream/main")"
FILES="$(git diff --name-only "${BASE}" upstream/main | wc -l | tr -d ' ')"
DAYS="$(( ( $(date +%s 2>/dev/null || echo 0) - $(git log -1 --format=%ct "$BASE") ) / 86400 ))"

echo "=== Deriva con upstream/main ==="
echo "Punto de fork (merge-base):  $BASE"
echo "Dias desde el fork:          $DAYS"
echo "Commits de upstream por delante: $COMMITS"
echo "Archivos distintos:           $FILES"
echo ""

if [ "$COMMITS" -eq 0 ]; then
  echo "Ya estamos al dia con upstream/main."
elif [ "$COMMITS" -lt 400 ]; then
  echo "Diff manejable (cadencia semanal recomendada, ver CLAUDE.md)."
else
  echo "Diff grande -- lleva mas de una semana sin sincronizar. Considera priorizar el sync."
fi
