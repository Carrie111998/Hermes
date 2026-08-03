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

**Ver [`REBRAND-EXCEPTIONS.md`](./REBRAND-EXCEPTIONS.md)** para la lista textual
completa de qué "Nous Research" se convierte siempre, qué se preserva siempre, y
el texto exacto aprobado para cada reemplazo de enlace de Discord/comunidad —
esto vivía solo en mensajes de commit pasados y hay que consultarlo (no
regrepear el historial) antes de cada sync con upstream.

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

## Proceso fijo de sync con upstream (cadencia semanal)

Aprendido en el sync de 2026-07 (13 días de divergencia, 3663 commits, y un bug
real de `--skip-nous-research` que convirtió atribuciones factuales por error).
Estos tres puntos son parte del proceso, no "cosas que se hicieron una vez":

**1. Cadencia semanal, no bisemanal.** El solapamiento entre nuestros cambios y
los de upstream no baja porque pase menos tiempo en el calendario — baja porque
el diff se mantiene chico. Sync semanal fuerza eso; esperar dos semanas repite
el evento de 2026-07 cíclicamente. `scripts/check-upstream-drift.sh` hace
`git fetch upstream` (solo lectura, no mergea ni comitea nada) y reporta
commits/archivos de diferencia — úsalo para decidir si toca sync, no para
ejecutarlo automáticamente. La decisión de sincronizar y las decisiones de
marca/código solapado siguen necesitando el OK explícito del usuario cada vez
(ver `[[iyari-flujo-trabajo-usuario]]` en memoria) — lo único que se automatiza
aquí es el aviso y el diagnóstico, nunca el merge.

**2. `REBRAND-EXCEPTIONS.md` es la fuente de verdad, no la sesión de Claude.**
Antes de reconstruir "qué se convierte" o "qué texto exacto se usó para X" desde
`git log`/`git show`, leer ese archivo. Si un sync encuentra un caso nuevo no
cubierto ahí, añadirlo (no dejarlo solo en el mensaje de commit).

**3. Auditoría de marca de árbol completo como gate obligatorio, no informe
opcional.** `scripts/audit-brand-residue.sh` corre el grep de marca sobre TODO
el repo (no solo `website/docs/`) y compara contra
`scripts/brand-audit-baseline.txt` — solo falla (exit 1) si aparece algo NUEVO
no clasificado, así que escala con el tamaño del cambio real, no con el tamaño
del repo. Correr **antes de cualquier commit** del sync (después del checkout
automático de archivos no-solapados Y después del trabajo manual de marca).
Si falla: clasificar cada hit nuevo en `REBRAND-EXCEPTIONS.md` (convertir, o
documentar como preservado/deuda de código diferida) y correr
`--update-baseline` para aceptarlo — nunca comitear con hits sin clasificar.
Este gate fue precisamente lo que faltó cuando `hermes_cli/_startup_fast.py`
se coló sin rebrandear dentro del lote "seguro" de 4723 archivos.

**4. Test de regresión del propio script de rebranding, antes de correrlo en
masa.** `scripts/test_iyari_transform.sh` valida con fixtures que
`apply-iyari-rebrand.sh` preserva "Nous Research" (factual/autoría/Discord) y
convierte "Hermes Agent" — y que el flag `--skip-nous-research` realmente tiene
efecto (para que el test no sea vacuo). Corre en segundos; si falla, es
exactamente la regresión de 2026-07 (el script corriendo sin el flag) antes de
que toque cientos de archivos reales, no después.

**Peligro conocido: NUNCA correr `hermes update` (sin `--check`) dentro de este
checkout.** El 2026-08-03, durante este mismo sync, algo invocó el `hermes
update` real (no `--check`) usando el `.venv` de este repo — como es una
instalación editable (`pip install -e .`), `PROJECT_ROOT` resuelve exactamente
a `~/iyari`, así que el autostash + cambio de rama + reset de `hermes update`
operó sobre el propio checkout de trabajo, escondiendo ~350 archivos sin
comitear en un `git stash` sin que nadie lo pidiera (recuperado sin pérdida de
datos, pero costó una investigación forense completa). No se encontró cron ni
launchd agent recurrente causándolo — probablemente una invocación puntual, no
un job programado — pero el riesgo mecánico sigue ahí para cualquier sesión
futura que toque este código. `hermes update --check` es de solo lectura y
seguro; `hermes update` a secas (o cualquier test que lo invoque sin mockear
subprocess) NO lo es en este checkout. Si hace falta probar la lógica de
update, usar un clon/fixture temporal, nunca este directorio.

**⚠️ PENDIENTE, NO RESUELTO:** esto de arriba es solo una advertencia en texto,
no una guarda real — exactamente el tipo de cosa que ya demostró hoy no bastar
por sí sola (algo disparó `hermes update` sin que nadie lo pidiera, presumible-
mente sin haber leído este archivo antes). Falta un obstáculo mecánico real:
opción A) una guarda en el propio `hermes update` que aborte si detecta que
`PROJECT_ROOT` es un checkout de desarrollo (p.ej. vía una env var tipo
`HERMES_DEV_CHECKOUT=1`); opción B) quitar/renombrar temporalmente el
entrypoint `hermes` del PATH mientras dure un sync activo. Ninguna de las dos
está implementada — próxima sesión que toque este código, hacerlo antes de
asumir que el riesgo ya quedó cerrado.

**⚠️ PENDIENTE, NO RESUELTO (tests): de los 98 tests que fallan en la suite
completa (`./scripts/run_tests.sh`, aislamiento por archivo, 34 archivos),
solo 10 están arreglados y confirmados en verde** (`test_i18n.py`,
`test_prompt_builder.py` [reconciliación real: le faltaba el monkeypatch de
`load_config_readonly` que upstream sí tiene, tenía tests propios de guardas
de inyección que upstream no tiene — nunca se clasificó como real_overlap en
Bloque A], `test_startup_fast_guards.py`, `test_packaging_build_guard.py`
[ambos: aserciones de string exacto contra marca vieja, mismo patrón que
`test_prompt_builder.py` de Fase 1]). 3 tests más (`test_wake_word.py`,
`test_service_manager.py`, `test_gateway_service.py`) tienen un traceback leído
que sugiere razonablemente que son específicos de macOS (tflite/D-Bus/bits de
permiso APFS) pero **sin confirmar contra el commit base pre-sync** — no dar
esto por "no es regresión" sin correrlo. **Los otros 85 tests (27 archivos,
incluidos 2 timeouts en `test_browser_hardening.py`/`test_browser_homebrew_paths.py`)
nunca se individualmente inspeccionaron** — solo se asumió por el nombre del
archivo (creds de Daytona/Modal/Fal, hardware, etc.), sin leer un solo
traceback real. Antes de dar la suite por buena: correr
`./scripts/run_tests.sh` de nuevo, leer los 85 restantes uno por uno o
agrupados por patrón de error (como se hizo con los 1.134→98 de hoy), y recién
entonces clasificar como regresión real vs entorno/dependencia faltante.

**⚠️ PENDIENTE, NO RESUELTO (hermes_state.py — migración de esquema, NO tocar
sin diseñarla primero):** `hermes_state.py` tiene 286 líneas de diff sin
reconciliar contra `upstream/main` (nunca formó parte de Bloque A ni de ningún
lote de rebranding; quedó "congelado" en el checkout automático del lote
"seguro" — la 5ª vez que aparece este patrón hoy, ver la entrada dedicada en
`REBRAND-EXCEPTIONS.md`). A diferencia de `tools/transcription_tools.py`
(13 líneas, aplicado quirúrgicamente esta noche, sin riesgo — solo nuevas
claves dentro de un blob JSON existente, sin tocar esquema), este diff **sí
introduce columnas reales nuevas** en la tabla `sessions`
(`last_activity_at`, `last_activity_description`, `last_activity_provenance`,
usadas por los nuevos métodos `touch_session_activity` / `get_session_activity`
/ `clear_session_activity_labels` / `session_yolo_enabled` / `set_session_yolo`).

Confirmado con evidencia real (no asumido):
- `grep` de esos 3 nombres de columna contra `CREATE\|ALTER\|migration\|schema`
  en nuestro `hermes_state.py` **y** en la copia de `upstream/main`: cero
  resultados en ambos — ninguna de las dos versiones del archivo trae consigo
  la migración que crea estas columnas (debe vivir en otro sitio del árbol de
  upstream que no se ha localizado, o depende de un runner de migraciones que
  no está en este archivo).
- `sqlite3 ~/.hermes/state.db ".schema sessions"` en una base de datos de
  desarrollo real: la tabla `sessions` **no tiene** ninguna columna
  `last_activity_*`.

Consecuencia si se aplica el fragmento de código sin la migración: cualquier
llamada a `touch_session_activity` fallaría en tiempo de ejecución contra una
base de datos existente (columna inexistente) — no es un bug sutil, sería
inmediato. Y una base de datos real con sesiones ya guardadas necesitaría un
`ALTER TABLE` explícito y versionado antes de poder correr ese código, no un
simple `git checkout` del archivo.

**No aplicar con la técnica quirúrgica de esta noche.** Los tests de
`tests/test_hermes_state.py` (7 fallos) quedan clasificados como **"pendiente
de migración de esquema"**, no como "arreglado" ni como "no-regresión, entorno"
— es una categoría propia. Antes de tocar `hermes_state.py`: (1) localizar
dónde vive realmente la migración de estas columnas en el árbol de upstream
(puede que en un commit posterior al fetch usado hoy, o en un mecanismo de
migración separado), (2) diseñar el `ALTER TABLE` para bases de datos
existentes, (3) solo entonces aplicar el código. Dedicarle su propia sesión
con calma, no la pieza final de una sesión ya larga.

**⚠️ PENDIENTE, NO RESUELTO (8 tests + 2 timeouts, sospecha fundada pero sin
confirmar contra upstream):**

- `tests/tools/test_file_tools.py` (2 tests: `TestWriteFileHandler::
  test_writes_content`, `TestPatchHandler::test_replace_mode_calls_patch_replace`)
  — falla comparando `'/tmp/out.txt'` (esperado) contra `'/private/tmp/out.txt'`
  (real). Sospecha fundada: macOS resuelve `/tmp` como symlink a `/private/tmp`
  (Linux no tiene este symlink), mismo patrón exacto que el bug ya confirmado en
  `tools/approval.py::_is_verification_artifact_cleanup`. Falta: diffear
  `tools/file_tools.py` y este test contra `upstream/main` para confirmar que
  el código es idéntico (no algo que rompimos) antes de cerrarlo como
  "no-regresión, entorno".
- `tests/tools/test_execution_flag_detection.py` (3 tests, todos
  `test_real_binaries_execute_leading_dash_program_payload` con distintos
  parámetros `sort`/`man`) — un caso hace timeout de 20s con
  `sort --buffer-size=1K --compress-program`, dos más no encuentran el archivo
  marcador esperado. Sospecha fundada: `--buffer-size`/`--compress-program` son
  flags de GNU `sort`; macOS trae `sort`/`man` de BSD, que no los reconoce (los
  ignora o falla distinto). Falta: mismo diff contra upstream para confirmar
  código idéntico, y correr el test en un Linux real (o Docker) para verificar
  que ahí sí pasa como se espera.
- `tests/tools/test_windows_native_support.py::
  TestReadmeNoLongerSaysWindowsUnsupported::test_readme_mentions_powershell_installer`
  (1 test) — **no es un bug**, es una decisión de producto pendiente: el test
  espera que `README.md` mencione `install.ps1` (instalador nativo de
  Windows), pero el `README.md` actual (sub-lote B, reescrito a mano con texto
  del usuario) no lo menciona. Decidir: ¿el README debería añadir la mención
  de instalación nativa en Windows, o el test debería ajustarse/eliminarse
  porque el README de IYARI no cubre ese flujo? Pendiente de decisión de Ruben,
  no de investigación técnica.
- `tests/tools/test_browser_hardening.py` y
  `tests/tools/test_browser_homebrew_paths.py` — **timeout de 300s en el
  runner canónico, nunca investigados**. Sospecha razonable dado el nombre
  (Homebrew/navegador real) de que dependen de red o de un binario de
  navegador instalado localmente, pero esto NO está confirmado — a diferencia
  de todo lo demás en esta sesión, aquí no se leyó ni un traceback real.
  Primer paso la próxima vez: correrlos sueltos con un timeout más largo y ver
  en qué línea exacta se cuelgan antes de suponer nada.

Ninguno de estos 8 (+2 timeouts) bloquea el merge de
`chore/upstream-partial-sync-non-overlapping` a `main` — quedan documentados
con dueño y con el paso exacto que falta, no ocultos.

**Orden recomendado por sync:**
```
./scripts/check-upstream-drift.sh          # ¿toca sync? (diagnóstico, no toca nada)
./scripts/test_iyari_transform.sh          # ¿el rebrander funciona como se espera?
# ... trabajo de sync (Bloque A manual + Bloque B con apply-iyari-rebrand.sh) ...
./scripts/audit-brand-residue.sh           # gate: ¿algo nuevo sin clasificar?
# solo si pasa limpio: commit
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
- Lote 6 `a675ede0c`: `messaging/` (32 .md). Sin anclas que reapuntar (headings
  Hermes no referenciados). Preservado header `X-Hermes-Session-Id` (matrix.md).
  Cosmético `signal-cli link -n "HermesAgent"` → `"IYARI"`. Advisory GHSA en
  telegram.md repunta al repo fork (tradeoff conocido de la regla de repo).
- Lote 7 (este commit): `configuration.md` (2131 líneas, 93 "Hermes" → IYARI). Sin
  anclas (headings son config-keys). Preservados `HermesSweEnv` (nombre de entorno
  benchmark RL) y `Nous-managed gateway` (servicio Nous).
- Sub-lote B (este commit, paréntesis fuera de la numeración de docs): `README.md` raíz
  reemplazado por versión IYARI (texto exacto del usuario) + logo `assets/iyari-logo-completo.png`
  (PNG real 645×474). Único ajuste sobre el texto pegado: `src` de imagen `.jpg`→`.png` y
  Quickstart envuelto en ```bash (aprobado por el usuario). Mensaje de commit:
  "G5 sub-lote B: README raíz IYARI + logo marca".
- Micro-lote READMEs idiomas (este commit): `git rm README.zh-CN.md README.ur-pk.md`
  (huérfanos con marca vieja); `README.es.md` reemplazado por versión IYARI (verbatim del
  usuario + "Inicio rápido" en ```bash). `README.md` y `README.es.md` únicos READMEs vivos.
- Lote 8 (este commit): `skills/bundled/{apple,autonomous-ai-agents,creative,data-science,
  dogfood,email,github,media}` + `skills/google-workspace.md` = 40. Sin anclas. Preservados
  `HermesCLI` (clase), `Nous Portal`. Prosa core del skill "hermes-agent" rebrandeada a IYARI.
- Lote 9 (este commit): `skills/bundled/{mlops,note-taking,productivity,research,smart-home,
  social-media,software-development,yuanbao}` = 34. Residual limpio. **`bundled/` COMPLETO.**
- Lote 10 (este commit): `skills/optional/` parte A {autonomous-ai-agents,blockchain,
  communication,creative,devops,dogfood,email,finance,gaming,health,mcp,migration} = 41.
  Preservados `HermesTokenStorage` (clase), `NousResearch/pokemon-agent` (repo distinto),
  `Nous catalog`. Autores → Digital Services LLC (incl. "Anthropic (adapted by DS LLC)").
- Lote 11 `f76567d7e`: `skills/optional/mlops` = 30. Residual limpio.
- Lote 12 (este commit): `skills/optional/` resto {payments,productivity,research,security,
  software-development,web-development} = 30. **`skills/`, `optional/` y `user-guide/` COMPLETOS.**
  Verificación global skills/: residual solo `Nous` (servicio), `HermesTokenStorage`/`HermesCLI`
  (clases), `NousResearch/pokemon-agent` (repo distinto) — todo preservado a propósito.
- **`features/`, `getting-started/`, `guides/`, `reference/`, `user-guide/` (todo) COMPLETOS.**
- Lote 13 (este commit): `developer-guide/` (31 .md/.mdx). Sin anclas que reapuntar
  (headings Hermes no referenciados; `HermesACPAgent` camelCase y `Surfacing Env Vars in
  \`hermes config\`` minúscula intactos). Preservados: headers `X-Hermes-Session-Id/Key`,
  clases `HermesPlugin`/`HermesCLI`/`HermesACPAgent`, servicio `Nous`, repo upstream distinto
  `NousResearch/hermes-example-plugins` (⚠️ FLAG: preservado; repointar solo si existe fork).
  Identity strings de prompt-assembly → "You are IYARI ... created by Digital Services LLC".
  2 refs a Discord de Nous → repo propio (precedente faq:844).
- Lote 14 (este commit): `integrations/` (3: index, nous-portal, providers). Página del
  servicio de terceros Nous Portal → `--skip-nous-research`: las 5 `Nous Research` son
  atribución factual de Nous Portal/Chat/modelo Hermes-4 a su dueño (precedente faq:20 =
  dejar). **Falsos positivos de familia de modelos preservados a mano** (bare "Hermes" que
  el lookahead no cubre): `**Hermes**` (fila tabla de familias), `Hermes 2/3` (×2, parsers/
  tool-calling), `Mistral, Hermes)` (lista de modelos). Ancla interna `#a-note-on-hermes-4`
  intacta (heading "A note on Hermes 4" = modelo, no cambia). NOTA CLAVE: revisar SIEMPRE
  bare "Hermes" en contexto de familia de modelos LLM (tablas, listas de parsers/tool-calling)
  porque el lookahead solo protege "Hermes-3/4" y "Hermes 3/4" directos.
- Lote 15 (este commit): sueltos raíz `website/docs/` = `index.mdx` (home) + `user-stories.mdx`.
  `userStories.json` (website/src/data/) **INTACTO** por decisión del usuario (testimonios/citas
  verbatim de terceros; opción 2). index.mdx: autoría del producto → Digital Services LLC +
  link iyari.io; repo GitHub → fork; producto Hermes/Hermes Agent → IYARI; lab de Nous como
  **atribución honesta de fork** ("Originally built by Nous Research, the lab behind Hermes,
  Nomos, and Psyche. Now maintained by Digital Services LLC as IYARI"); `Hermes` en esa frase =
  modelo/proyecto preservado. user-stories.mdx: envoltorio → IYARI, citas intactas.
  ⚠️ FLAG: home aún enlaza instalación/descarga a `hermes-agent.nousresearch.com` (install.sh/
  ps1, Download Desktop) — preservado por regla de URL; actualizar si el fork tiene su instalador.
- **GRUPO 5 (docs de website/docs/) COMPLETO.** Pendiente futuro: GRUPO 6 = i18n zh-Hans/;
  código `.py` heredado (identity strings ya se tocan en docs, no en código). Revisar si
  procede `userStories.json`/instaladores del home en una pasada de marketing aparte.

## Verificación de cierre GRUPO 5 (post-commit e877ed748)

Tras cerrar GRUPO 5 se corrieron 4 checks de auditoría (residual global, corrupción
inversa, build, commits/Co-Authored). Resultado y **lote 16** de corrección:

- **Residual**: 2384 "Hermes" brutos en website/, pero 2311 viven en `i18n/zh-Hans/`
  (GRUPO 6, fuera de alcance, correcto no tocarlo). En `website/docs/` (alcance real):
  73 "Hermes" (todo intencional: modelo Hermes-2/3/4, clases camelCase HermesCLI/
  HermesPlugin/HermesACPAgent/HermesTokenStorage/HermesSweEnv/HermesBench —éste último
  un benchmark externo de Nous, preservado tras confirmarlo con el usuario—, headers
  X-Hermes-Session-*, flag -HermesHome, tarea HermesGateway, lab-attribution en
  index.mdx) y 8 "Nous Research" (todas decisiones ya tomadas: lab, Nous Portal/Chat,
  faq:20). **Sin corrupción inversa** (falsos positivos de regex con 2 URLs en la misma
  línea, verificado sin duplicación real).
- **Lote 16 (bug-fix)**: se hallaron ~17 "Hermes" residuales en `user-guide/features/`
  heredados de los **lotes 1-2** (sesión anterior, antes del criterio actual) — prosa/
  comentarios de código/ejemplos que nombraban el producto como "Hermes" sin rebrandear
  (api-server.md, acp.md, browser.md, mcp.md, extending-the-dashboard.md,
  built-in-plugins.md, kanban-worker-lanes.md → "IYARI Kanban", codex-app-server-runtime.md
  incl. diagrama ASCII realineado, memory-providers.md, hooks.md, goals.md incl. transcript
  "Hermes:"→"IYARI:"). Corregidos con el transformador + reversión de headers
  X-Hermes-Session-Id/Key. **Commit pendiente de push al cierre de esta sesión.**
- **Build**: `website/` nunca tuvo `node_modules` instalado en este entorno; se usó
  `pnpm install` (hay `pnpm-lock.yaml`, ahora commiteado). Dos bugs preexistentes de
  dependencias corregidos (autorizados por el usuario, NO son parte del rebranding):
  1. `@docusaurus/theme-mermaid` estaba en rango `"^3.9.2"` mientras el resto de
     `@docusaurus/*` fijan exacto `3.9.2` (Docusaurus exige versión idéntica) → fijado a
     `"3.9.2"`.
  2. Tras el fix anterior, `webpack` resolvía a `5.109.0` (más nueva de lo que
     `@docusaurus/core@3.9.2` espera; su ProgressPlugin no acepta ya la opción
     `reporter`) → añadido `pnpm.overrides.webpack: "5.95.0"` (el `overrides` raíz
     preexistente es sintaxis npm, pnpm no lo lee; se dejó intacto sin tocar sus otras
     2 entradas — no es de esta sesión decidir si migrarlo).
  - **⚠️ DEUDA PENDIENTE, NO RESUELTA**: tras (1)+(2), el build sigue fallando en SSG
    (4 de 358 páginas: `/docs/developer-guide/worktree-ui-dev`,
    `/docs/user-guide/checkpoints-and-rollback`, `/docs/user-guide/messaging/`,
    `/docs/user-guide/messaging/open-webui`) con
    `ReactContextError: useColorMode called outside <ColorModeProvider>` dentro de
    `@docusaurus/theme-mermaid`'s `MermaidRenderer`, pese a que theme-mermaid declara
    soportar React 19 (`peerDependencies: react ^18||^19`, instalado 19.2.8). Parece bug
    real de la librería con SSG + React 19, no relacionado con el contenido rebrandeado.
    El usuario decidió NO seguir investigando en esta sesión — queda como deuda técnica
    aparte. Próximo paso sugerido: probar downgrade acotado de theme-mermaid o de
    react/react-dom a una combinación conocida-buena, en una sesión dedicada a infra.
- **Commits**: 19 commits en el rango `1a8df5d88..main` (todo GRUPO 5), autor único
  Digital Services LLC, **cero** "Co-Authored" — limpio.
- Pendiente tras user-guide/: `developer-guide/`, `integrations/`, `docs/` sueltos, `userStories.json`.

## Progreso GRUPO 6 (espejo chino `website/i18n/zh-Hans/`)

Mismo transformador y mismo criterio que GRUPO 5, aplicado al mirror `zh-Hans`
(`website/i18n/zh-Hans/docusaurus-plugin-content-docs/current/`, 314 `.md` + 2 `.mdx`,
misma estructura de directorios que `website/docs/`). Plan de lotes (mismo orden que
GRUPO 5): `getting-started`(6) → `guides`(28) → `integrations`+`reference`(14) →
`user-guide/` sueltos+`secrets`(15) → `user-guide/features`(44) →
`user-guide/messaging`(28) → `user-guide/skills/bundled`(70, 2 lotes) →
`user-guide/skills/optional`(81, 2-3 lotes) → `developer-guide`(27) →
`index.mdx`+`user-stories.mdx`(2).

Hallazgos de auditoría previos al lote 1 (aplican a todo GRUPO 6):
- El transformador es case-sensitive: protege `HERMES_*` (env vars mayúsculas),
  `hermes` minúscula (comando/paths) y `Hermes-3/4` igual que en inglés, sin cambios.
- 153 headings con `Hermes`/`Nous` detectados en el árbol completo. Los headings en
  chino generan su propio slug (basado en el texto chino), pero varios enlaces
  internos ya apuntaban a fragmentos **en inglés que nunca coincidían** con el heading
  chino real (p.ej. `environment-variables.md` → `#how-hermes-runs-shell-commands-on-windows`
  contra un heading "## Hermes 在 Windows 上如何运行 shell 命令" sin anchor custom) —
  **bug preexistente de la traducción, ajeno al rebranding**, no se corrige en este
  grupo. El rebranding no rompe nada que ya funcionara: solo renombra `Hermes`→`IYARI`
  en el texto visible del heading, sin tocar el fragmento (minúscula, no matcheado).
- Build de verificación (`pnpm exec docusaurus build --locale zh-Hans`) reproduce el
  mismo bug SSG de `theme-mermaid`/React 19 ya documentado en GRUPO 5 (4 páginas,
  deuda técnica aparte). Los ~98 warnings de enlaces rotos previos al crash son todos
  por páginas nunca traducidas al chino (`platform-support.md`, `multi-profile-gateways.md`,
  `worktree-ui-dev.md`, etc.), no por el rebranding. **Nota:** el build reutiliza cache
  de webpack entre corridas y deja de re-emitir esos warnings en la segunda corrida —
  no sirve como diff incremental fiable; el chequeo real por lote es el grep de headings
  con `Hermes`/`Nous` antes de aplicar.

- Lote 1 `47cc184b3`: `getting-started/` (6 archivos). Sin headings con Hermes/Nous en
  este lote → cero riesgo de ancla. Preservados `HERMES_HOME`, `HERMES_GIT_BASH_PATH`,
  `HERMES_DISABLE_WINDOWS_UTF8`, `HERMES_DEV`, `HERMES_MANAGED`, `HERMES_BUNDLED_SKILLS`,
  `Nous` como nombre de provider (Nous Portal). Repo `NousResearch/hermes-agent` →
  `digital-services-llc/iyari` en comandos `nix run`/`git clone`/flake inputs. Build
  limpio (`.docusaurus`+cache borrados) confirmó 98/98 warnings idénticos a la
  baseline → cero anclas rotas (evidencia concluyente, no como el de lote 1 en vivo
  que salió cacheado).
- Lote 2 `68243263c`: `guides/` (28 archivos). 21 headings con Hermes/Nous cambiaron
  texto (auditados contra todo `zh-Hans`, filtrando falsos positivos de comentarios
  `#` dentro de bloques ```bash```; ninguno tenía un anchor interno que coincidiera
  realmente → cero riesgo). Preservados `HERMES_STREAM_READ_TIMEOUT`,
  `HERMES_API_TIMEOUT`, `HERMES_STREAM_STALE_TIMEOUT`, `HERMES_GATEWAY_TOKEN`,
  `HERMES_CRON_TIMEOUT`, `Hermes-4`/`Hermes-4-70B`/`Hermes-4-405B` (modelo, en
  `run-hermes-with-nous-portal.md`), `Nous Portal`/`Subscription`/`Chat`/provider
  (incl. cita literal `"using Nous as inference provider"`). `User-Agent:
  "Hermes-Monitor/1.0"` → `"IYARI-Monitor/1.0"`. Build limpio: 98/98 warnings
  idénticos a la baseline.
- Lote 3 (este commit): `integrations/`+`reference/` (14 archivos). **No calificó para
  auto-aprobación** (mismo patrón de casos dudosos que G5 lotes 4/14): `--skip-nous-research`
  para 6 atribuciones factuales (`nous-portal.md`×4, `providers.md`×1, `faq.md`×1,
  precedente `faq:20`); edición manual `faq.md` `[Nous Research Discord]` →
  `[IYARI 的 GitHub](https://github.com/digital-services-llc/iyari)` + `team@iyari.io`
  (precedente `faq:844`); 4 falsos positivos de familia de modelos revertidos a mano
  (`nous-portal.md:38` tabla `**Hermes**`, `providers.md:686`/`:759` `Hermes 2/3`,
  `providers.md:807` `Mistral、Hermes）`) — el lookahead del regex no cubre "Hermes X"
  con X≠3/4. 3 headings cambiaron (`faq.md` L15/L39/L745), sin anchors internos que
  las referenciaran. Build limpio: 98/98 warnings idénticos a la baseline.
- Lote 4 (auto-aprobado, este commit): `user-guide/` sueltos (13) + `secrets/` (2) = 15
  archivos. Cumplió las 7 condiciones del checklist de auto-aprobación → aplicado,
  auditado, commiteado y pusheado sin pausa intermedia. Sin `Nous Research`, sin
  falsos positivos de familia de modelos. 10 headings cambiaron (`configuring-models.md`,
  `docker.md`, `git-worktrees.md`, `windows-native.md`×2, `windows-wsl-quickstart.md`×5),
  ninguno con anchor interno real que coincidiera. Preservados: `HermesSweEnv`
  (benchmark RL), `-HermesHome` (flag instalador), `HermesGateway` (tarea schtasks),
  `window.__HERMES_SESSION_TOKEN__` (JS global), `Nous Portal`. Build limpio: 98/98
  warnings idénticos a la baseline.
- Lote 5 (este commit): `user-guide/features/` (44 archivos). **No calificó para
  auto-aprobación**: 1 `Nous Research` en `personality.md:127`, pero NO era atribución
  factual de servicio sino un string de identidad embebido en el fallback de
  prompt-assembly ("You are Hermes Agent... created by Nous Research...") — precedente
  **G5 lote 13**: estos strings se rebrandean completos, no se preservan. Aplicado
  transformador normal (sin `--skip-nous-research`, único caso en el lote) →
  `"You are IYARI, an intelligent AI assistant created by Digital Services LLC..."`.
  Preservado `NousResearch/hermes-example-plugins` (repo upstream distinto, mismo
  precedente de lote 13, idéntico a la doc en inglés) en `browser.md`/
  `extending-the-dashboard.md`. 13 headings cambiaron, ninguno con anchor interno real.
  Build limpio: 98/98 warnings idénticos a la baseline.
- Lote 6 (auto-aprobado, este commit): `user-guide/messaging/` (28 archivos). Cumplió
  las 7 condiciones → aplicado, auditado, commiteado y pusheado sin pausa. Sin
  `Nous Research`, sin falsos positivos de familia de modelos. Advisory
  `GHSA-3vpc-7q5r-276h` en `telegram.md` repunta al repo fork automáticamente vía
  regla 0 (mismo tradeoff que G5 lote 6). 28 headings cambiaron (patrón repetido
  "配置 Hermes"/"Hermes 的行为方式" en cada doc de plataforma), ninguno con anchor
  interno real. Cosmético `signal-cli link -n "HermesAgent"` → `"IYARI"` (mismo
  precedente que G5 lote 6, camelCase sin word-boundary que el script no toca solo).
  Build limpio: 98/98 warnings idénticos a la baseline.
- Lote 7 (este commit): `skills/bundled/{apple,autonomous-ai-agents,creative,data-science,
  dogfood,email,github,media}` = 38 (primera mitad de `bundled/`, 70 en total).
  **No calificó para auto-aprobación** en el chequeo inicial: 1 `Nous Research` en
  `autonomous-ai-agents-hermes-agent.md` (prosa descriptiva del framework, no atribución
  factual de servicio) y un "falso positivo" aparente en `creative-ascii-art.md`
  (`0xbyt4, Hermes Agent` en fila de autor) — ambos verificados contra la doc en inglés
  ya commiteada: **coinciden exactamente** con transformación normal ya aplicada allí
  (`Nous Research`→Digital Services LLC, `Hermes Agent`→IYARI en la fila de autor), no
  eran casos nuevos. Aplicado transformador normal a los 38. 7 headings cambiaron, sin
  anchors internos que los referenciaran. Preservados `HermesCLI` (clase), `Nous Portal`.
  Build limpio: 98/98 warnings idénticos a la baseline.
- Lote 8 (este commit): `skills/bundled/{mlops,note-taking,productivity,research,
  smart-home,social-media,software-development,yuanbao}` = 32 (segunda mitad de
  `bundled/`). **`skills/bundled/` COMPLETO (70/70).** 2 casos verificados contra la
  doc en inglés (ambos ya resueltos allí igual, transformación normal): autor
  `Nous Research`→`Digital Services LLC` en `productivity-google-workspace.md`, mención
  normal del producto en `productivity-nano-pdf.md` ("already available in Hermes"→IYARI).
  Curiosidad replicada a propósito: heading `# 编写 Hermes-Agent Skills（仓库内）` →
  `# 编写 IYARI-Agent Skills（仓库内）` (guión, no espacio — el regex no limpia el
  "-Agent" residual); el inglés ya commiteado tiene el mismo artefacto
  ("Authoring IYARI-Agent Skills (in-repo)"), se replica por consistencia, no es un
  bug nuevo. 10 headings cambiaron, sin anchors internos que los referenciaran. Build
  limpio: 98/98 warnings idénticos a la baseline.
- Lote 9 (este commit): `skills/optional/{autonomous-ai-agents,blockchain,communication,
  creative,devops,dogfood,email,finance,health}` = 27 (parte 1 de `optional/`, 81 en
  total; sin `gaming`/`payments` en el mirror zh). 9 `Nous Research` verificados contra
  inglés (todas filas de autor: `Hermes Agent (Nous Research)`,
  `Anthropic（由 Nous Research 改编）`×7, `Hermes Agent + Nous Research`) — transformación
  normal, coincide exacto con `Digital Services LLC` ya commiteado en inglés. 2 headings
  cambiaron, sin anchors internos. Build limpio: 98/98 warnings idénticos a la baseline.
- Lote 10 (este commit): `skills/optional/{mcp,migration,mlops}` = 31 (parte 2 de
  `optional/`). 1 `Nous Research` en `migration-openclaw-migration.md` (fila de autor
  `Hermes Agent (Nous Research)`, verificado contra inglés `IYARI (Digital Services
  LLC)` ya commiteado) — transformación normal. 2 headings cambiaron, sin anchors
  internos. Residual post-transform: cero (ni siquiera `HERMES_HOME`). Build limpio:
  98/98 warnings idénticos a la baseline.
- Lote 11 (este commit): `skills/optional/{productivity,research,security,
  software-development,web-development}` = 23 (parte 3 y cierre de `optional/`).
  **`skills/optional/` y `skills/` COMPLETOS.** 1 `Nous Research` en
  `productivity-telephony.md` (fila de autor, verificado contra inglés
  `Digital Services LLC` ya commiteado). 5 headings cambiaron, sin anchors internos.
  Build limpio: 98/98 warnings idénticos a la baseline.
- **`skills/bundled/` y `skills/optional/` (todo `skills/`) COMPLETOS en GRUPO 6.**
- Lote 12 (este commit): `developer-guide/` (27 archivos). 2 `Nous Research` en
  `prompt-assembly.md` (identity strings "You are Hermes...created by Nous
  Research"/"You are Hermes Agent...created by Nous Research", precedente exacto de
  G5 lote 13) — transformación normal, coincide con inglés ya commiteado
  ("You are IYARI...created by Digital Services LLC" ×2). Preservado
  `NousResearch/hermes-example-plugins` (repo distinto, mismo precedente de lotes
  5/13), `HermesCLI`/`HermesACPAgent` (clases camelCase), `hermes config` minúscula +
  anchor custom `{#surfacing-env-vars-in-hermes-config}`, header
  `X-Hermes-Session-Id/Key`. 2 headings cambiaron, sin anchors internos. Build limpio:
  98/98 warnings idénticos a la baseline.
- Lote 13 (este commit, cierre GRUPO 6): `index.mdx` (home) + `user-stories.mdx`.
  **No calificó para auto-aprobación**: el home en inglés (lote 15) no fue transformación
  mecánica sino reescritura a mano (título/descripción/hero atribuidos a Digital
  Services LLC + link `iyari.io`, y frase "Now maintained by Digital Services LLC as
  IYARI" añadida a las 2 menciones de "the lab behind Hermes, Nomos, and Psyche"). El
  chino tenía el texto **pre-rebranding** intacto. Traducida la misma reescritura
  (confirmada por el usuario): título/descripción/hero → `Digital Services LLC`
  (hero con link a `iyari.io`, reemplaza `nousresearch.com`); las 2 frases de
  atribución del laboratorio mantienen `Nous Research` intacto (atribución honesta,
  precedente index.mdx inglés) + se añadió "现由 Digital Services LLC 以 IYARI 之名维护".
  **Bug evitado**: el transformador con `--skip-nous-research` igual transforma
  bare "Hermes" en "该实验室是 Hermes、Nomos 和 Psyche" (coma no protegida por el
  lookahead) → revertido a mano en las 2 líneas, igual que el inglés preserva "Hermes"
  ahí (modelo/proyecto, no producto). Resto del archivo (heading, repo `NousResearch/
  hermes-agent`, menciones genéricas de Hermes en tabla de enlaces) transformación
  normal sin incidentes. `user-stories.mdx`: cambio mecánico único, limpio.
  Build limpio: 98/98 warnings idénticos a la baseline.
- **GRUPO 6 (i18n `website/i18n/zh-Hans/`) COMPLETO.** 13 lotes, 316 archivos
  (314 `.md` + 2 `.mdx`), mismo criterio y transformador que GRUPO 5. Pendiente futuro:
  código `.py` heredado (fuera de alcance de ambos grupos de docs).

## Auto-aprobación delegada (GRUPO 6)

El usuario delega aprobación previa para lotes mecánicos de rebranding en `website/i18n/zh-Hans/`. Claude Code puede aplicar, commitear y pushear SIN preguntar si se cumplen TODAS estas condiciones:

1. Alcance del lote está en la tabla de lotes del GRUPO 6.
2. Dry-run muestra solo transformaciones esperadas: `Hermes Agent`→IYARI, `Hermes` suelto→IYARI, `NousResearch/hermes-agent`→digital-services-llc/iyari.
3. Se preservan: `hermes` minúscula, `HERMES_*`, URLs `nousresearch.com`/`hermes-agent.nousresearch.com`, IDs modelo `NousResearch/Hermes-*`, `LICENSE`, Nous Portal, clases `Hermes*`, `HermesBench`, `X-Hermes-*`.
4. Auditoría residual no encuentra `Hermes`/`Nous Research` no preservados.
5. Build acotado `npm run build -- --locale zh-Hans` no introduce warnings ni anclas rotas nuevas respecto a la línea base.
6. Mensaje de commit: `G6 lote N: rebrand i18n/zh-Hans/<ruta>/ (X archivos)`.
7. Autor: `Digital Services LLC <info@digitalservices.app>`, sin Co-Authored-By.

Si FALTA alguna condición, Claude para y pregunta. Esta delegación NO aplica a Fase 1, operaciones destructivas ni gasto extra no presupuestado.
