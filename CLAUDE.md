# IYARI — Rebranding Hermes → IYARI (GRUPO 5: docs)

Fork de Hermes/Nous Research. Marca del producto: **IYARI** (Digital Services LLC).
Repo: `DIGITAL-SERVICES-LLC/iyari`, rama `main` (commit directo a main).
Idioma de trabajo con el usuario: **español**.

## Criterio GRUPO 5 — "docs manda, es fork"

En la documentación (`website/docs/`) se **TOCA todo** lo que sea marca visible
Hermes/Nous Research → IYARI/Digital Services LLC, **SALVO** lo funcional/legal:

- URLs `nousresearch.com` (y subdominios, p.ej. `hermes-agent.nousresearch.com`) — romperían llamadas.
- Comando y paquete `hermes` en **minúscula** (`hermes model`, `hermes-agent`, `hermes-gateway`, `hermes_cli`…).
- Paths `~/.hermes/` y env vars `HERMES_*` (funcionales).
- Modelo LLM real `Hermes-3` / `Hermes-4` (incluye `Hermes-4-70B`, `Hermes-4-405B`).
- `LICENSE` (legal).
- Servicio de terceros **Nous Portal** (y `Nous Subscription`, `Nous Tool`, `Nous Chat`,
  `provider=nous`, `Nous` como proveedor). Es un servicio distinto, NO es "Nous Research".

La doc refleja el producto final IYARI aunque el código `.py` heredado siga en
"Hermes Agent"/"Nous Research" (el código es un grupo futuro, no GRUPO 5).

## Las 5 reglas del transformador (`scripts/iyari_transform.py`)

Se aplican **en este orden**:

0. `NousResearch/hermes-agent` → `digital-services-llc/iyari`
   (cubre URLs `github.com/...`, `gh --repo ...`, prosa y `.git`; NO toca repos de
   terceros como `JiaDe-Wu/sample-hermes-agent-...`).
0b. `Nous Research` (con espacio) → `Digital Services LLC`
   (no toca `nousresearch.com` ni `Nous Portal`). Flag `--skip-nous-research` para
   omitirla cuando la ocurrencia es un caso dudoso a revisar a mano.
1. `Hermes Agent` → `IYARI` (elimina "Agent"). Va **antes** que la regla 2.
2. `\bHermes\b(?![- ][34])` → `IYARI` (protege el modelo `Hermes-3/4`).
3. `IYARI'(?!s)` → `IYARI's` (arregla el posesivo tras aplicar la regla 2).

Uso:
```
python3 scripts/iyari_transform.py --dry  <archivos>   # pasada en seco (diff)
python3 scripts/iyari_transform.py        <archivos>   # aplica in-place
```

## Flujo obligatorio por lote

1. **Detección/auditoría** de casos de riesgo: contextos de `Nous` (Portal/Tool vs
   Research), compuestos `Hermes-...`, y sobre todo **anclas/slugs**: si un heading
   con "Hermes"/"Nous" cambia, su slug cambia y rompe enlaces `](#...)` internos y
   cross-file (Docusaurus). Buscar y reapuntar esas referencias (a veces en archivos
   de otros directorios; se toca **solo el fragmento `#...`**, no la prosa de esos
   archivos pendientes).
2. **Dry-run** (`--dry`) y revisión del diff.
3. **Auditoría residual**: confirmar que lo que queda con `Hermes`/`Nous` es solo lo
   preservado (comandos/paths/env/modelo/servicio Nous Portal/URLs).
4. **Aplicar** el transformador.
5. **Resumen** al usuario (archivos tocados, anclas reapuntadas, casos dudosos
   marcados) y **ESPERAR su OK explícito**.
6. **Commit** directo a `main`, mensaje **plano** (lo da el usuario),
   **SIN Co-Authored-By ni trailers**. Autor: `Digital Services LLC <info@digitalservices.app>`.
7. **Push** a `main` y dar el **hash** del commit.

**Norma:** la actualización de la sección "Progreso" de este `CLAUDE.md` va **DENTRO
del commit de cada lote**, nunca en un commit suelto (excepción histórica: lote 4).

Lotes de 20-50 archivos. "Si dudas, déjalo y me lo señalas": marcar ambigüedades en
el resumen en vez de decidir a ciegas (p.ej. atribución de Nous Portal a su dueño,
enlaces a recursos comunitarios externos como el Discord de Nous).

## Progreso GRUPO 5

- Lote 1 `51b8f2874` + Lote 2 `017f84414`: `features/` + `getting-started/` (completos).
- Lote 3 `73a4a14e1`: `guides/` (completo).
- Lote 4 `70cac80a0`: `reference/` (12 archivos, completo) + ancla `desktop.md`. Casos
  faq.md decididos: L20 (atribución de Nous Portal a Nous Research) se dejó intacto;
  L844 (antes Discord de Nous) ahora apunta a `github.com/digital-services-llc/iyari`
  + `team@iyari.io`.
- Lote 5 `1c0ebbdf4`: `user-guide/` sueltos (15, sin configuration.md) + `secrets/`
  (3) = 18 archivos + ancla `env-vars.md#how-iyari-runs-shell-commands-on-windows`.
  Preservados nuevos identificadores funcionales: header HTTP `X-Hermes-Session-Token`,
  flag instalador `-HermesHome`, tarea schtasks `HermesGateway` (heredados del código).
  `secrets/index.md`: Discord de Nous → repo propio (precedente faq:844). desktop.md:
  "Nous Research" (botón/enlace OAuth) → Digital Services LLC por coherencia con
  web-dashboard.md ya commiteado.
- Lote 6 (este commit): `messaging/` (32 .md). Sin anclas que reapuntar (headings
  Hermes no referenciados). Preservado header `X-Hermes-Session-Id` (matrix.md).
  Cosmético `signal-cli link -n "HermesAgent"` → `"IYARI"`. Advisory GHSA en
  telegram.md repunta al repo fork (tradeoff conocido de la regla de repo).
- Sub-lotes user-guide/ restantes: 7=`configuration.md` (~2131 líneas),
  8=`skills/bundled/`+google-workspace (74, dividir en 2), 9-10=`skills/optional/` (101, dividir).
- Pendiente tras user-guide/: `developer-guide/`, `integrations/`, `docs/` sueltos, `userStories.json`.
- GRUPO 6 futuro: espejo chino `website/i18n/zh-Hans/` (NO tocar aún).
