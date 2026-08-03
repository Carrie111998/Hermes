# REBRAND-EXCEPTIONS — guía de referencia para "Nous Research" en la doc

Este archivo existe porque las decisiones de qué convertir y qué preservar en
`website/docs/`, `website/i18n/zh-Hans/` y `locales/` vivían solo en mensajes de
commit pasados (GRUPO 5 lotes 4/5/8/10/13/14/15/16). Cada vez que hay que
reconstruirlas grepeando `git log`/`git show`, se repite trabajo y hay riesgo de
volver a cometer el mismo error (ver más abajo: "el bug del --skip-nous-research").
**Este archivo es la fuente de verdad textual. Antes de tocar una ocurrencia de
"Nous Research", mira aquí primero.**

Para las 5 reglas del transformador (`scripts/iyari_transform.py`) y el flujo por
lote, ver `CLAUDE.md`. Este archivo se centra solo en "Nous Research" — el caso que
no tiene una regla regex segura y necesita juicio caso por caso.

**Este archivo alimenta la mecánica, no solo la documenta.**
`scripts/audit-brand-residue.sh` (gate obligatorio de cada sync, ver CLAUDE.md
§"Proceso fijo de sync con upstream") usa `scripts/brand-audit-baseline.txt`
como lista de exclusión — cuando clasifiques un caso nuevo aquí, recuerda correr
`./scripts/audit-brand-residue.sh --update-baseline` para que el gate lo acepte.
`scripts/test_iyari_transform.sh` valida con fixtures que `apply-iyari-rebrand.sh`
nunca vuelve a convertir "Nous Research" ciegamente (la regresión de 2026-07).

## Regla de oro para syncs con upstream

`scripts/apply-iyari-rebrand.sh` (y `iyari_transform.py`) deben correr **siempre**
con `--skip-nous-research` sobre contenido recién traído de upstream. La regla 0b
del transformador convertiría ciegamente TODAS las "Nous Research" a
"Digital Services LLC", incluyendo las atribuciones factuales de abajo. Esto ya
pasó una vez (sync de 2026-07, ~72 ocurrencias mal convertidas, autodetectado y
revertido). El script ya tiene `--skip-nous-research` como default — no quitarlo.

Tras el paso automático, hay que aplicar a mano la lista de CONVERT de abajo
(el script no las replica) y verificar que la lista de PRESERVE quedó intacta.

## "Nous Research" que SIEMPRE se convierte a "Digital Services LLC"

1. **Filas de autoría de skills.** Patrón: línea que contiene `Author`/`作者` +
   `|` + `Nous Research` (tabla de metadata al principio de cada skill doc).
   → `Digital Services LLC` (o `IYARI (Digital Services LLC)` /
   `IYARI + Digital Services LLC` si el original ya combinaba el nombre del
   producto con el autor — revisar caso a caso, no asumir un único patrón).
   Afecta ~28 archivos bajo `website/docs/user-guide/skills/` (+ pares zh-Hans).

2. **Strings de identidad del prompt del agente** (el texto que el propio IYARI
   usa para presentarse). Patrón exacto documentado:
   - `personality.md` (+zh): `"You are IYARI, an intelligent AI assistant created
     by Nous Research..."` → `by Digital Services LLC...`
   - `prompt-assembly.md` (+zh, 2 ocurrencias): `You are IYARI, an AI assistant
     created by Nous Research.` → `by Digital Services LLC.` y `You are IYARI, an
     intelligent AI assistant created by Nous Research.` → `by Digital Services
     LLC.`
   Estas frases están en inglés incluso dentro de los archivos zh-Hans (son el
   contenido literal de un prompt, no prosa traducible) — no traducir, solo
   cambiar el nombre del autor.

3. **Prosa de skill que describe el propio producto** (no atribución de un
   servicio de terceros). Ejemplo:
   `autonomous-ai-agents-hermes-agent.md` (+zh): `"IYARI is an open-source AI
   agent framework by Nous Research..."` → `by Digital Services LLC...` /
   zh: `IYARI 是 Nous Research 开发的开源 AI agent 框架` → `IYARI 是 Digital
   Services LLC 开发的开源 AI agent 框架`.

4. **Copy de botón/enlace OAuth "Sign in with Nous Research"** en el flujo de
   login del dashboard/desktop (es literalmente el nombre que usamos para
   nuestro propio proveedor OAuth por defecto, no atribución a un tercero).
   Afecta `desktop.md` y `web-dashboard.md` — ver la lista completa de
   ocurrencias exactas en la sección "Caso completo: web-dashboard.md /
   desktop.md" más abajo, porque además cambia un **anchor** (`#default-
   provider-nous-research` → `#default-provider-digital-services-llc`) referenciado
   desde varios archivos.

5. **Home (`index.mdx`, +zh) — título/descripción/hero.** El nombre del autor del
   producto en el frontmatter y el párrafo de apertura → `Digital Services LLC`
   (con enlace a `https://iyari.io` en la versión EN del hero). Ver texto exacto
   abajo en "Caso completo: index.mdx".

6. **Enlaces/menciones de Discord de la comunidad** → ver sección dedicada abajo,
   texto exacto aprobado.

## "Nous Research" que SIEMPRE se preserva (NO tocar)

1. **Atribución factual de Nous Portal como servicio de terceros.** Nous Portal
   es un servicio operado por Nous Research; decir que es "Nous Research's
   subscription gateway" es un hecho, no branding nuestro. Ejemplos ya
   confirmados (precedente GRUPO 5 lote 4, faq.md línea 20; lote 14,
   integrations/nous-portal.md y providers.md):
   - `faq.md` L20 (+zh L20): `**[Nous Portal](/integrations/nous-portal)** —
     Nous Research's subscription gateway — 300+ models...`
   - `integrations/nous-portal.md` (+zh): `[Nous Portal](...) is Nous Research's
     unified subscription gateway...`, `Nous Research's own **Hermes 4**
     family...`, `it's the official guidance from Nous Research.`
   - `integrations/providers.md` (+zh): `[Nous Portal](...) is Nous Research's
     unified subscription gateway...`

2. **Atribución factual en noticias/anuncios de terceros.**
   `guides/run-nemotron-3-ultra-free.md`: `Nous Research has been inducted into
   the Nemotron Coalition...` — es una noticia sobre Nous Research como empresa,
   no sobre nuestro producto. Preservar completo.

3. **Listas de proveedores de IA de terceros que incluyen "Nous Research" como
   nombre de empresa junto a OpenAI/Anthropic/Google/etc.**
   `user-guide/egress/iron-proxy.md`: `# ... and Nous Research.` (comentario de
   código listando proveedores permitidos). Preservar.

4. **Menciones de "the lab behind Hermes/Nomos/Psyche" en `index.mdx`** — es
   atribución honesta de fork, no un error. Texto exacto aprobado (precedente
   GRUPO 5 lote 15, reconfirmado en el sync de 2026-07): ver "Caso completo:
   index.mdx" abajo. Nota: en esta frase **"Hermes" se mantiene** (nombre del
   modelo/proyecto original), no se convierte a IYARI.

## Caso completo: `index.mdx` (+ zh-Hans)

Frontmatter y hero (EN):
```
description: "The self-improving AI agent built by Digital Services LLC. ..."
...
The self-improving AI agent built by [Digital Services LLC](https://iyari.io). ...
```

Bullet "Built on a model-trainer foundation" (antes "Built by model trainers"):
```
- **Built on a model-trainer foundation** — Originally built by [Nous Research](https://nousresearch.com), the lab behind Hermes, Nomos, and Psyche. Now maintained by Digital Services LLC as IYARI. Works with [Nous Portal](https://portal.nousresearch.com), [OpenRouter](https://openrouter.ai), OpenAI, or any endpoint
```

Bullet "Research-ready":
```
- **Research-ready** — Batch processing, trajectory export, RL training with Atropos. Originally built by [Nous Research](https://nousresearch.com) — the lab behind Hermes, Nomos, and Psyche models. Now maintained by Digital Services LLC as IYARI
```

zh-Hans (traducción propia, sin precedente previo a este sync — mismo criterio,
frase "Now maintained by Digital Services LLC as IYARI" se tradujo como "现由
Digital Services LLC 以 IYARI 之名维护"):
```
description: "由 Digital Services LLC 构建的自我改进 AI 智能体。..."
由 [Digital Services LLC](https://iyari.io) 构建的自我改进 AI 智能体。...
- **基于模型训练者的基础** — 最初由 [Nous Research](https://nousresearch.com) 构建，该实验室是 Hermes、Nomos 和 Psyche 背后的团队，现由 Digital Services LLC 以 IYARI 之名维护。支持 [Nous Portal](https://portal.nousresearch.com)、[OpenRouter](https://openrouter.ai)、OpenAI 或任意端点
- **研究就绪** — 批处理、轨迹导出、基于 Atropos 的 RL 训练。最初由 [Nous Research](https://nousresearch.com) 构建——该实验室是 Hermes、Nomos 和 Psyche 模型背后的团队，现由 Digital Services LLC 以 IYARI 之名维护
```

⚠️ Gotcha recurrente: tras pasar el transformador automático sobre contenido
fresco de upstream, estas 4 líneas (EN+zh) vuelven a aparecer con **bare
"Hermes" convertido a "IYARI"** dentro de "the lab behind Hermes, Nomos, and
Psyche" (el lookahead de la regla 2 solo protege `Hermes-3/4` y `Hermes 3/4`, no
`Hermes,`). Hay que revertir manualmente ese "Hermes" cada vez — mismo patrón que
el gotcha de familia de modelos (ver más abajo).

## Caso completo: `web-dashboard.md` + `desktop.md` (sin equivalente zh-Hans a la
fecha de este documento — verificar si existen antes de asumir que no aplica)

Reemplazos exactos (8 en `web-dashboard.md`, 3 en `desktop.md`):

| Antes (upstream) | Después (IYARI) |
|---|---|
| `### Default provider: Nous Research` | `### Default provider: Digital Services LLC` |
| `#### Worked example: Nous Research` | `#### Worked example: Digital Services LLC` |
| `Click **Sign in with Nous Research**` | `Click **Sign in with Digital Services LLC**` |
| `[Nous Research provider](#default-provider-nous-research)` | `[Digital Services LLC provider](#default-provider-digital-services-llc)` |
| `[Nous Research](#default-provider-nous-research)` (link corto) | `[Digital Services LLC](#default-provider-digital-services-llc)` |
| `"Continue with Nous Research" button` | `"Continue with Digital Services LLC" button` |
| `*Sign in with Nous Research*` (cursiva) | `*Sign in with Digital Services LLC*` |
| `[Default provider: Nous Research](#default-provider-nous-research)` | `[Default provider: Digital Services LLC](#default-provider-digital-services-llc)` |
| `sign in from the app with **Sign in with Nous Research**` (desktop.md) | `**Sign in with Digital Services LLC**` |
| `(e.g. *Sign in with Nous Research*)` (desktop.md) | `(e.g. *Sign in with Digital Services LLC*)` |

**Anchor que cambia y hay que reapuntar en todo el árbol** (el heading
`### Default provider: Nous Research` genera el slug `#default-provider-nous-
research`; al cambiar el heading el slug cambia a `#default-provider-digital-
services-llc`). Grep de verificación tras cada sync:
```
grep -rn "default-provider-nous-research" website/docs/ website/i18n/
```
Debe devolver **cero** resultados. Igual para el segundo anchor afectado en el
mismo par de archivos, `#connecting-hermes-desktop-to-a-remote-backend` →
`#connecting-iyari-desktop-to-a-remote-backend` (el heading es "## Connecting
Hermes Desktop..." → la regla 2 del transformador ya convierte "Hermes Desktop"
a "IYARI Desktop" en el heading, pero **no** actualiza los anchors que apuntan a
él desde otros archivos — hay que reapuntarlos a mano). Archivos que referencian
este segundo anchor, a día de este sync: `desktop.md` (×2),
`features/web-dashboard.md` (×2), `reference/environment-variables.md` (×1).

**No confundir con:** "OAuth (Nous Portal)" como nombre del método de
autenticación — eso SÍ se preserva (Nous Portal es el servicio real que emite el
login), solo el nombre del *proveedor* que aparece en el botón/heading
("Nous Research" → "Digital Services LLC") se convierte.

## Texto exacto aprobado para reemplazar enlaces/menciones de Discord

Nous Research tiene un canal de Discord (`#plugins-skills-and-skins`) que
upstream referencia para "publica tu plugin/skill aquí" o "pregunta a la
comunidad aquí". Como fork, no tenemos ese Discord — se reemplaza por un enlace
al repo de GitHub propio, con wording específico por contexto (no es un único
texto genérico, cada archivo tiene su propia frase — mantener la variación,
no forzar una sola plantilla):

| Archivo | Antes | Después |
|---|---|---|
| `developer-guide/plugins/index.md` | `Promote it in the Nous Research Discord \`#plugins-skills-and-skins\` channel.` | `Promote it on [IYARI on GitHub](https://github.com/digital-services-llc/iyari).` |
| `developer-guide/secret-source-plugin.md` | `...share it in the Nous Research Discord (\`#plugins-skills-and-skins\`).` | `...share it on [IYARI on GitHub](https://github.com/digital-services-llc/iyari).` |
| `user-guide/secrets/index.md` | `...share them in the Nous Research Discord (\`#plugins-skills-and-skins\`).` | `...share them on [IYARI on GitHub](https://github.com/digital-services-llc/iyari).` |
| `reference/faq.md` (+zh) | `2. **Ask the community:** [Nous Research Discord](https://discord.gg/nousresearch)` | `2. **Ask the community:** [IYARI on GitHub](https://github.com/digital-services-llc/iyari) or write to team@iyari.io` |

zh-Hans de `faq.md` (traducción propia de la fila anterior, sin precedente previo
a este sync):
```
2. **向社区提问：** [GitHub 上的 IYARI](https://github.com/digital-services-llc/iyari) 或发邮件至 team@iyari.io
```

`user-guide/secrets/index.md` y `developer-guide/plugins/index.md` sí tienen
equivalente zh-Hans, pero a la fecha de este sync ninguno de los dos contenía la
frase de Discord en la versión traducida (posible traducción parcial previa, o
la traducción nunca incluyó esa oración) — verificar con
`grep -n "Nous Research Discord\|discord.gg/nousresearch"` en cada sync futuro,
no asumir que sigue así.

`developer-guide/secret-source-plugin.md` no tiene equivalente zh-Hans a la
fecha de este documento.

## Gotcha recurrente: falsos positivos de "Hermes" en contexto de familia de modelos

El lookahead de la regla 2 del transformador (`\bHermes\b(?![- ][34])`) protege
`Hermes-3/4` y `Hermes 3/4` pero **no** cubre listas/tablas que mencionan el
modelo sin ese sufijo inmediato. Revisar SIEMPRE tras cada sync/re-transform:

- `**Hermes**` (celda de tabla en `integrations/nous-portal.md`/`providers.md`,
  fila de la familia de modelos)
- `Hermes 2/3` (menciones de parsers/tool-calling en `providers.md`)
- `Mistral, Hermes)` / `Mistral、Hermes）` (listas de modelos soportados)
- `the lab behind Hermes, Nomos, and Psyche` (ver caso `index.mdx` arriba)

Grep de verificación tras cada sync (debe devolver cero falsos positivos, es
decir, ninguna de estas líneas debería tener "IYARI" en vez de "Hermes"):
```
grep -rn "IYARI 2/3\|Mistral、IYARI\|\*\*IYARI\*\*\|lab behind IYARI" website/docs/ website/i18n/
```

## Gotcha recurrente (el más repetido del sync de 2026-08): contenido "congelado"
## en versión pre-sync en vez de recibir la actualización real de upstream

Durante el sync de 2026-08 este patrón exacto apareció **cinco veces**, en
archivos de naturaleza completamente distinta — no es un incidente aislado,
es una característica repetida del método "aplicar y luego corregir":

1. `locales/` — 16 de 17 archivos quedaron con contenido viejo (secciones
   `diff:`/`context:` enteras faltantes) mientras que solo `ar.yaml` se
   actualizó de verdad.
2. `hermes_cli/_startup_fast.py` — sin rebrandear dentro del lote "seguro"
   (encontrado en Bloque A).
3. 5 skill docs de `skills/bundled/github/` (+ sus espejos zh-Hans) — content
   con un regex sed inseguro que upstream ya había eliminado en su propia
   versión actual.
4. `tools/transcription_tools.py` (código) — faltaban 13 líneas reales de una
   función de upstream (`_confidence_thresholds` aplicado al gate de
   faster-whisper).
5. `hermes_state.py` (código, persistencia de estado) — 286 líneas de diff sin
   reconciliar, con el agravante de que aquí sí toca columnas reales de tabla
   (ver pendiente dedicado en `CLAUDE.md`) — no resuelto, dejado a propósito.

**Causa común probable:** el checkout automático (`git checkout upstream/main
-- <dir>`) no garantiza que TODO el árbol quedó realmente al día — puede haber
quedado un fetch parcial, una interrupción a medio camino, o un archivo que
nunca se tocó porque no calificó como "no solapado" en la clasificación
inicial pero tampoco entró en la lista manual de Bloque A.

**Regla a aplicar en cada sync, no solo cuando un test lo delate:** tras
cualquier checkout/reconciliación, para cada archivo relevante (no solo los
que un test señale) correr `diff <(git show upstream/main:<archivo>) <archivo>`
explícitamente — no asumir que "ya se actualizó" solo porque el comando de
checkout se ejecutó sin error. Si el diff no es cero, decidir con evidencia
(no por el nombre del archivo): ¿es contenido de marca (resuelto por el
transformador), prosa/config pura (aplicar directo, como los skills de
GitHub o los locales), código sin tocar esquema/formato persistido (aplicar
quirúrgico tras confirmar acoplamiento, como `transcription_tools.py`), o
código que toca esquema de base de datos/formato de serialización (NO aplicar
sin diseñar la migración primero, como `hermes_state.py`)? El nivel de cuidado
escala con lo que el diff realmente toca, no con la etiqueta "código vs docs".

## Gotcha recurrente: `_category_.json` de Docusaurus

`scripts/apply-iyari-rebrand.sh` originalmente solo buscaba `.md/.mdx/.yaml/.yml`
y se saltaba los `_category_.json` (metadata de sidebar de Docusaurus, campo
`link.description`) — 4 archivos con "Hermes Agent" sin convertir sobrevivieron
un sync entero hasta que la auditoría completa de árbol (paso 3 de Bloque B) los
encontró. El script ya se corrigió para incluir `*.json` en el `find`. Verificar
tras cada sync:
```
grep -rln "Hermes Agent\|Nous Research" website/docs/ website/i18n/ locales/ --include="*.json"
```
Debe devolver cero (o solo casos ya clasificados arriba, ninguno esperado en
`_category_.json`).

## Gotcha recurrente (grave): hechos históricos/partnerships de Nous Research NO
## son prosa de marca genérica — cambiar el sujeto inventa una afirmación falsa

`guides/run-nemotron-3-ultra-free.md` anuncia un hecho histórico real y fechado:
"Nous Research fue admitida en la Nemotron Coalition (NVIDIA) y se asoció con
Nebius en junio de 2026". Al reparar la corrupción del autostash (ver gotcha de
abajo) se cambió el sujeto de la frase a "Digital Services LLC" — **eso no es
rebranding, es una afirmación verificablemente falsa**: Digital Services LLC
nunca fue admitida en esa coalición ni se asoció con NVIDIA/Nebius. El primer
intento de arreglo tampoco bastó: revertir solo el sujeto ("Nous Research has
been inducted...") sin revisar el resto de la frase dejó un pronombre en
primera persona ("we've partnered with Nebius") pegado a un sujeto en tercera
persona — hay que revisar la frase completa, no solo la primera ocurrencia.

**Criterio a aplicar en cada sync, especialmente en `website/docs/guides/`:**
la pregunta correcta no es "¿dice Nous Research?" sino **"¿esta frase describe
algo que Nous Research hizo de verdad (un partnership, un anuncio, un logro,
una fecha concreta), o es solo texto de marca genérico/reemplazable?"**. Si es
lo primero, "Nous Research" se preserva como sujeto del hecho **en toda la
oración**, incluyendo pronombres derivados (they/their, no we/our) — no se
aplica `--skip-nous-research` a ciegas ni se revierte solo la primera palabra
encontrada. Texto correcto de referencia (`run-nemotron-3-ultra-free.md`):
```
Nous Research has been inducted into the **Nemotron Coalition** of leading AI
labs working with **NVIDIA** to advance open frontier foundation models. In
honor of this, they've partnered with **Nebius** to provide **Nemotron 3
Ultra** free on [Nous Portal](https://portal.nousresearch.com) for two weeks
(**June 4th – June 18th**). Follow the instructions below to try the model in
your IYARI today.
```
Cualquier página nueva de `website/docs/guides/` que anuncie un partnership,
colaboración, evento o logro histórico específico de Nous Research corre el
mismo riesgo — revisar la oración completa, no solo la mención de marca.

## Gotcha recurrente: `\b` no protege "Hermes" pegado a texto CJK/no-latino

En `locales/ko.yaml` (y potencialmente ja/zh/zh-hant), el regex `\bHermes\b` de la
regla 2 no matchea cuando "Hermes" está pegado sin espacio a un carácter Hangul/
Han (p.ej. `Hermes는`, partícula coreana) — Python trata esos caracteres como
`\w`, así que no hay frontera de palabra entre "s" y el carácter siguiente. El
transformador no lo toca y queda sin convertir. Revisar a mano bare "Hermes"
pegado a texto no-latino en locales/i18n CJK tras cada sync:
```
grep -n "Hermes[^ -]" locales/*.yaml website/i18n/*/**/*.md 2>/dev/null
```

## Gotcha recurrente: `git checkout upstream/main -- <dir>` no garantiza que TODO
## el directorio se haya escrito realmente

En el sync de 2026-08, tras corregir el bug de `--skip-nous-research` se volvió a
correr `git checkout upstream/main -- locales/ website/docs/ website/i18n/` para
descartar el primer intento fallido. El resultado: 16 de 17 archivos de
`locales/` quedaron con contenido **viejo, pre-sync** (secciones enteras
faltantes: `diff:`, `context:`, `background_delegations`) mientras que
`ar.yaml` sí se actualizó — causa exacta no confirmada (posible interrupción o
un checkout parcial), pero el efecto fue silencioso: sin error, sin conflicto,
simplemente contenido desactualizado que solo se detectó porque
`tests/agent/test_i18n.py::test_catalog_keys_match_english` comparaba claves
entre locales y notó que `ar.yaml` tenía claves que `en.yaml` no tenía.
**Lección: tras cualquier `git checkout upstream/main -- <dir>` de un sync,
diffear cada archivo del directorio contra `upstream/main` (no solo un muestreo)
antes de asumir que "ya se actualizó todo".** El test de i18n es una red de
seguridad real para `locales/` — correrlo explícitamente
(`pytest tests/agent/test_i18n.py`) es parte del checklist de todo sync que
toque `locales/`, no solo confiar en el checkout.

## Identificadores funcionales que NUNCA se tocan (recordatorio, detalle completo en CLAUDE.md)

`hermes` (comando/paquete en minúscula), `~/.hermes/`, `HERMES_*` (env vars),
`Hermes-3`/`Hermes-4` (modelo LLM real), URLs `nousresearch.com` y subdominios,
`LICENSE`, y los identificadores de clase heredados del código Python
(`HermesCLI`, `HermesPlugin`, `HermesACPAgent`, `HermesTokenStorage`,
`HermesSweEnv`, `HermesBench` — este último confirmado con el usuario como
benchmark externo de Nous, no nuestro).
