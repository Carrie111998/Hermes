#!/usr/bin/env bash
# Test de regresion para scripts/apply-iyari-rebrand.sh + iyari_transform.py.
#
# Existe porque en el sync de 2026-07, apply-iyari-rebrand.sh se corrio una vez
# sin --skip-nous-research y convirtio ciegamente ~72 atribuciones factuales de
# "Nous Research" (propiedad de Nous Portal, menciones "lab behind Hermes") a
# "Digital Services LLC" antes de que alguien lo notara. Este test fija el
# comportamiento esperado para que la regresion se detecte antes de tocar
# archivos reales, no despues.
#
# Correr antes de cada sync con upstream, y cada vez que cualquiera de los dos
# scripts cambie.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

# --- Fixture 1: fila de autor de skill (debe quedar intacta al pasar por el
#     wrapper -- la conversion de autoria es un paso manual aparte, no algo
#     que apply-iyari-rebrand.sh haga por su cuenta) ---
cat > "$TMPDIR/skill.md" <<'EOF'
| Author | Nous Research |
EOF

# --- Fixture 2: atribucion factual de Nous Portal (nunca se debe convertir) ---
cat > "$TMPDIR/nous-portal.md" <<'EOF'
Nous Portal is Nous Research's unified subscription gateway.
EOF

# --- Fixture 3: mencion de Discord (nunca se auto-convierte -- el texto de
#     reemplazo se elige a mano por archivo, no es un trabajo de regex) ---
cat > "$TMPDIR/discord.md" <<'EOF'
Publish your backend as a standalone plugin repo and share it in the Nous Research Discord (`#plugins-skills-and-skins`).
EOF

# --- Fixture 4: nombre de nuestro propio producto, si debe convertirse ---
cat > "$TMPDIR/product.md" <<'EOF'
Hermes Agent is a self-improving AI agent.
EOF

./scripts/apply-iyari-rebrand.sh "$TMPDIR" > /dev/null

grep -q "Nous Research" "$TMPDIR/skill.md" || fail "skill.md: 'Nous Research' fue convertido por el wrapper (deberia preservarse -- la fila de autor se convierte en un paso manual aparte, no con apply-iyari-rebrand.sh)"
grep -q "Nous Research" "$TMPDIR/nous-portal.md" || fail "nous-portal.md: atribucion factual de Nous Portal fue convertida (regresion del bug de --skip-nous-research)"
grep -q "Nous Research Discord" "$TMPDIR/discord.md" || fail "discord.md: mencion de Discord fue convertida automaticamente (deberia quedar intacta para reemplazo manual)"
grep -q "IYARI is a self-improving" "$TMPDIR/product.md" || fail "product.md: 'Hermes Agent' NO fue convertido a IYARI (el transformador no esta funcionando)"

echo "OK: apply-iyari-rebrand.sh preserva 'Nous Research' (factual/autoria/Discord) y convierte 'Hermes Agent' -- comportamiento esperado."

# --- Chequeo de cordura: probar que --skip-nous-research realmente hace algo,
#     para que el bloque anterior no sea trivialmente cierto porque el
#     transformador nunca toca "Nous Research" sin importar el flag. ---
rm -f "$TMPDIR"/*.md
cat > "$TMPDIR/sanity.md" <<'EOF'
Nous Research built this.
EOF
python3 scripts/iyari_transform.py "$TMPDIR/sanity.md" > /dev/null
grep -q "Digital Services LLC built this" "$TMPDIR/sanity.md" || fail "sanity: iyari_transform.py SIN --skip-nous-research no convirtio 'Nous Research' -- el flag no se puede probar con este fixture, revisar el test"

echo "OK: iyari_transform.py sin --skip-nous-research si convierte 'Nous Research' (confirma que el flag tiene efecto real, el test anterior no es vacio)."
echo ""
echo "REGRESION DE 2026-07 CUBIERTA: si alguien vuelve a correr apply-iyari-rebrand.sh sin el flag por defecto, este test lo detecta."
