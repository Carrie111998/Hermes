# CAPABILITIES.md — Inventario de Hermes Agent

> Repo auditado: NousResearch/hermes-agent
> Commit: `c9de69c6d5ed602059f5e9c9950c150e07b89212` · Fecha commit: 2026-07-30 15:08:31 -0400 · Total commits: 19615
> Clon local auditado: `C:\proyectos\inventario\hermes-oficial` (solo lectura)
>
> ⚠️ **LEE ESTE DOCUMENTO ANTES DE PROPONER CONSTRUIR CUALQUIER COSA.**
> Si lo que vas a construir aparece aquí, NO lo construyas: úsalo o extiéndelo. Si crees que lo existente no sirve, pregunta primero.
>
> **Nota sobre cobertura**: esta auditoría se hizo con 6 sub-agentes en paralelo. 4 de 6 terminaron su barrido completo (dominios D1, D2, D6, D7, D8, D9, D10, D11, D13, D14, D15). Los 2 restantes (D3 — plataformas de mensajería, D4 — webhooks a fondo, D5 — generación de medios a fondo, D12 — browser a fondo) fueron **detenidos antes de completar** por control de consumo de tokens. Para esos dominios este documento usa evidencia indirecta encontrada dentro de los reportes ya completados (mencionada explícitamente como tal) — son pistas fuertes de que la capacidad existe y dónde vive, pero **no una auditoría de archivo por archivo**. Todo lo marcado "NO VERIFICADO" necesita una pasada dedicada antes de confiar en ello al 100%.

---

## TABLA DE CONSULTA RÁPIDA

| Quiero... | ¿Existe? | Dónde | Cómo se extiende |
|---|---|---|---|
| Programar tareas recurrentes / one-shot | **SÍ** | `cron/scheduler.py`, `cron/jobs.py`, `cron/executions.py` | Nuevo trigger: proveedor en `plugins/cron_providers/`. Nuevo job: `cron.jobs.create_job()` o tool `cronjob` |
| Terminal integrada (usuario, app escritorio) | **SÍ** | `apps/desktop/src/app/right-sidebar/terminal/*`, xterm.js + node-pty vía IPC (`electron/main.ts`, `electron/preload.ts`) | Nuevo `kind` de terminal en `terminals.ts`/`workspace.tsx` |
| Terminal/backends de ejecución para el agente | **SÍ**, 8 backends | `tools/environments/{local,docker,ssh,singularity,modal,managed_modal,daytona,vercel_sandbox}.py` | Heredar `BaseEnvironment` (`tools/environments/base.py`), registrar en `tools/terminal_tool.py::_create_environment` |
| Recibir/enviar por Telegram, WhatsApp, Discord, Slack, etc. | **SÍ (evidencia indirecta fuerte, D3 no auditado a fondo)** | `plugins/platforms/`, `gateway/platforms/` — lista de plataformas soportadas confirmada indirectamente vía targets de entrega de cron: Telegram, Discord, Slack, Matrix, Feishu, WhatsApp, Signal, SMS, Email, Weixin, Mattermost, Home Assistant, DingTalk, WeCom, BlueBubbles, QQ Bot | Nuevo adaptador: implementar interfaz de `gateway/platforms/base.py`, ver `gateway/platforms/ADDING_A_PLATFORM.md` (no leído en esta pasada) |
| Webhooks entrantes (GitHub, API, eventos) | **SÍ** | `hermes_cli/subcommands/webhook.py` (`hermes webhook subscribe`), `gateway/platforms/webhook.py` (rutas declarativas con `events`, `secret`, `prompt`, `deliver`, `deliver_only`) | Nueva ruta: entrada en `platforms.webhook.extra.routes` de `config.yaml`, sin tocar código |
| Generación de imágenes con IA | **SÍ (evidencia indirecta, D5 no auditado a fondo)** | `plugins/image_gen/` (7 backends), `tools/image_generation_tool.py` (1668L) | Nuevo backend: `plugin.yaml` en `plugins/image_gen/<nombre>/` |
| Generación de video con IA | **SÍ (evidencia indirecta, D5 no auditado a fondo)** | `plugins/video_gen/` (3 backends), `tools/video_generation_tool.py`, `tools/flux3_video_tool.py`, `tools/xai_video_tools.py` | Nuevo backend: `plugin.yaml` en `plugins/video_gen/<nombre>/` |
| TTS / voz | **SÍ** | `tools/tts_tool.py` (3676L), `tools/tts_streaming.py`, `tools/neutts_synth.py`, `tools/voice_mode.py`, `tools/wake_word.py` | NO VERIFICADO cómo se añade backend nuevo de TTS |
| Composición de imagen (plantillas, texto, formatos sociales) | **NO VERIFICADO** (D5 no completado, sin evidencia indirecta) | — | — |
| Navegador / scraping / browsing agente | **SÍ** | `plugins/browser/` (browser_use, browserbase, firecrawl — 3 sub-plugins confirmados), `tools/browser_tool.py`, `tools/browser_camofox.py`, `tools/browser_cdp_tool.py` | Nuevo backend: `plugin.yaml` en `plugins/browser/<nombre>/` |
| Publicar en X/Twitter | **SÍ** | `skills/social-media/xurl/SKILL.md` (skill, 437L), `tools/x_search_tool.py` | Editar/clonar la skill xurl |
| Publicar en Instagram / Facebook | **NO VERIFICADO** (categoría `skills/social-media/` solo tiene 2 skills; una es xurl; la otra no identificada) | — | — |
| Publicar en TikTok / LinkedIn / YouTube | **NO VERIFICADO**, sin evidencia encontrada | — | — |
| Calendario de contenido | **NO EXISTE** (confirmado: 0 resultados grep `content_calendar`/`editorial calendar` en todo el repo) | — | Construir desde cero |
| Multi-marca / multi-cliente aislado | **SÍ** | `hermes_cli/profiles.py` (2226L) — instancias `HERMES_HOME` completamente independientes (config, memoria, skills, sesiones, cron) | `hermes profile create <nombre>`, `--clone`/`--clone-all` |
| Perfil de voz/tono de marca persistente | **SÍ (parcial)** | `SOUL.md` por perfil (`hermes_cli/default_soul.py`), forma parte de `_CLONE_CONFIG_FILES` en `profiles.py` | Un perfil = una marca; editar `SOUL.md` |
| Memoria persistente / RAG | **SÍ**, 8 proveedores | `plugins/memory/{byterover,hindsight,holographic,honcho,mem0,openviking,retaindb,supermemory}/` | Nuevo proveedor: carpeta con `plugin.yaml` + implementar `MemoryProvider` (`agent/memory_provider.py`) — **ojo: solo UN proveedor activo a la vez, sin namespaces internos** |
| Memoria aislada por marca/cliente | **SÍ, pero vía perfiles**, no vía namespace interno de memoria | `hermes_cli/profiles.py` | Un perfil = una memoria aislada |
| Sistema de skills (recetas del agente) | **SÍ**, 70 (bundled) + 111 (optional) = 181 | `skills/`, `optional-skills/`, compatible con agentskills.io | Carpeta nueva con `SKILL.md` (frontmatter YAML: `name`, `description` req.) bajo `skills/<categoría>/<nombre>/` |
| El agente crea skills nuevas solo | **SÍ** | `tools/skill_manager_tool.py` (`action='create'`), instruido en `agent/prompt_builder.py` (`SKILLS_GUIDANCE`) | — |
| Sistema de plugins genérico | **SÍ**, 18 carpetas / 97 `plugin.yaml` | `plugins/*/plugin.yaml`, loader en `hermes_cli/plugins.py` | 4 fuentes: bundled, `~/.hermes/plugins/`, `./.hermes/plugins/` (opt-in), pip entry-point `hermes_agent.plugins` |
| MCP — Hermes como servidor | **SÍ (superficie angosta: solo mensajería/aprobaciones)** | `mcp_serve.py` (`hermes mcp serve`) — 9 tools | No hay exportador genérico de tools propias vía MCP con 1 click |
| MCP — conectar servidores externos | **SÍ** | `tools/mcp_tool.py` (6829L, el archivo más grande del repo), OAuth en `tools/mcp_oauth.py`/`mcp_oauth_manager.py` | `hermes mcp add <nombre> --command/--url ...`, o catálogo curado en `optional-mcps/` (6 entradas) |
| Gestión de tokens OAuth con refresco automático | **SÍ** | `tools/mcp_oauth_manager.py`, `agent/azure_identity_adapter.py`, auth Nous vía `get_provider_auth_state()` | Reutilizable como patrón para nuevas integraciones OAuth |
| Medición de consumo y créditos por usuario | **SÍ**, robusto | `agent/usage_pricing.py` (`CanonicalUsage`, `PricingEntry`), `agent/credits_tracker.py` (`CreditsState`, headers `x-nous-credits-*`) | Motor de medición por token es genérico/reutilizable |
| Integración de pagos (Stripe) | **PARCIAL / NO reutilizable directo** | `hermes_cli/nous_billing.py` — Stripe opera **detrás** del Nous Portal, Hermes nunca lo llama directo | Habría que construir cliente Stripe propio desde cero; solo el motor de medición (`usage_pricing.py`) es reutilizable |
| Aprobación humana antes de acciones sensibles | **SÍ, muy maduro** | `tools/approval.py` (4161L), `tools/write_approval.py`, `hermes_cli/subcommands/approvals.py` | Patrones nuevos en `DANGEROUS_PATTERNS`; allowlist vía `config.yaml` |
| Almacenamiento en la nube (S3, R2) | **NO VERIFICADO** (D5 no completado) | — | — |
| Alojamiento de media en URLs HTTPS públicas | **NO VERIFICADO** (D5 no completado) | — | — |
| Métricas de rendimiento de publicaciones | **NO VERIFICADO**, sin evidencia encontrada | — | — |
| Delegación / subagentes en paralelo | **SÍ** | `tools/delegate_tool.py` (3974L, `ThreadPoolExecutor`), `tools/async_delegation.py` | Modos single-task y batch paralelo |
| Sandboxing de ejecución de código | **SÍ**, multi-capa | Docker hardening (`cap-drop ALL`, `no-new-privileges`, `pids-limit`) en `tools/environments/docker.py`; también Modal/Daytona/Vercel Sandbox remotos | — |
| Guardas anti-inyección de prompts (cron) | **SÍ** | `cron/scheduler.py` (`CronPromptInjectionBlocked`, `_guard_job_credential_exfil`), `tools/cronjob_tools.py` | — |
| CLI con subcomandos | **SÍ**, 38 subcomandos | `hermes_cli/subcommands/*.py` | — |
| Perfiles multi-idioma (i18n) app escritorio | **SÍ**, 5 idiomas (en, ar, ja, zh, zh-hant) | `apps/desktop/src/i18n/` | Plugins pueden registrar sus propios bundles (`plugin-i18n.ts`) |
| Extensibilidad de la app de escritorio (pantallas propias) | **SÍ** | `apps/desktop/src/app/contrib/` (sistema "contrib"), `routes.ts` tiene tipo `'extension'` | NO VERIFICADO en profundidad el mecanismo exacto de registro |

---

## HUECOS CONFIRMADOS

Lo único que, con la evidencia recolectada, **realmente no existe** y habría que construir desde cero:

1. **Calendario de contenido / editorial** — confirmado ausente (grep exhaustivo sin resultados).
2. **Composición de imagen** (plantillas, texto sobre imagen, redimensionado a formatos sociales 9:16/1:1/4:5) — sin evidencia de que exista, aunque D5 no se auditó a fondo.
3. **Integración de pagos propia (Stripe u otro) desacoplada de Nous** — el código de cobro real es 100% específico del Portal de Nous; no hay interfaz de "billing provider" abstracta reutilizable.
4. **Publicación nativa en Instagram/Facebook/TikTok/LinkedIn/YouTube** — sin evidencia encontrada (solo X/Twitter confirmado vía skill `xurl`).
5. **Métricas de rendimiento de publicaciones** (analytics de posts) — sin evidencia encontrada en los dominios auditados.
6. **Almacenamiento en la nube (S3/R2) y alojamiento de media en URLs públicas HTTPS** — sin evidencia en los dominios auditados; probablemente vive en D5 (no completado) si existe.

**Dominios que necesitan una pasada dedicada antes de dar por buena esta lista de huecos**: D3 (plataformas de mensajería a fondo), D4 (webhooks a fondo — lo esencial ya está cubierto vía D1), D5 (generación de medios a fondo), D12 (browser a fondo — lo esencial ya está cubierto vía D8/D13).

---

## D1 — SCHEDULER Y EJECUCIÓN PERSISTENTE

**QUÉ HACE**: Motor de tareas programadas y automatizaciones desatendidas: cron jobs (delay relativo, intervalo, cron de 5 campos, timestamp ISO one-shot), webhooks de eventos, entrega multi-plataforma, ledger de auditoría, recuperación tras caída, locking cross-proceso, modo "managed" (Chronos) para scale-to-zero.

**DÓNDE VIVE**:
| Archivo | Líneas |
|---|---|
| `cron/scheduler.py` | 4364 |
| `cron/jobs.py` | 2609 |
| `cron/executions.py` | 280 |
| `cron/blueprint_catalog.py` | 713 |
| `cron/suggestion_catalog.py` | 154 |
| `cron/suggestions.py` | 260 |
| `cron/lifecycle_guard.py` | 141 |
| `cron/scheduler_provider.py` | 357 |
| **Total `cron/`** | **8920** |

Además: `hermes_cli/cron.py`, `hermes_cli/subcommands/cron.py`, `hermes_cli/web_routers/cron.py`, `tools/cronjob_tools.py`, `plugins/cron_providers/`, doc interna `website/docs/developer-guide/cron-internals.md`, y `hermes-already-has-routines.md` (raíz del repo).

**PERSISTENCIA**: 2 mecanismos. (1) Definición de jobs: `~/.hermes/cron/jobs.json` — JSON con escritura atómica (temp+rename) y lock cross-proceso (`fcntl.flock`/`msvcrt.locking`), **NO SQLite**. (2) Ledger de ejecuciones: SÍ SQLite, `~/.hermes/cron/executions.db` (`cron/executions.py:40-53`), estados `claimed/running/completed/failed/unknown`, WAL + `PRAGMA synchronous=FULL`.

**RECUPERACIÓN TRAS CAÍDA**: `recover_interrupted_executions()` (`cron/executions.py:199-233`) escanea filas `claimed`/`running` de otro proceso, verifica si el PID sigue vivo; si no, marca `unknown` (no reintenta automáticamente).

**CLAIM/LEASE/HEARTBEAT**: Sí, multi-capa — lock de tick cross-proceso (`.tick.lock`), `claim_dispatch`/`claim_job_for_fire` (compare-and-set, at-most-once multi-máquina), `_running_lock` in-process, heartbeat de ticker (`record_ticker_heartbeat`) y heartbeat de claim de ejecución (`heartbeat_run_claim`, cada 60s desde hilo dedicado).

**TRIGGERS SOPORTADOS**: delay relativo (`30m`, `2h`), intervalo (`every 2h`), cron 5-campos (`0 9 * * *`), ISO timestamp one-shot. Separado: webhooks de eventos externos (`hermes webhook subscribe`, HMAC).

**ENTREGA DE RESULTADOS**: Sistema de targets `platform:<destino>:<thread>` — Telegram, Discord, Slack, Matrix, Feishu, WhatsApp, Signal, SMS, Email, Weixin, Mattermost, Home Assistant, DingTalk, WeCom, BlueBubbles, QQ Bot, `origin`, `local`. Prefijo `[SILENT]` suprime entrega.

**BLUEPRINTS vs SUGGESTIONS**: Blueprints = automatización parametrizada con slots tipados, renderizada en form GUI/CLI/agente/deep-link (`cron/blueprint_catalog.py`). Suggestions = propuestas que el usuario acepta/descarta (`cron/suggestions.py`, `cron/suggestion_catalog.py`), 4 orígenes (catalog/blueprint/usage/integration), cap `MAX_PENDING=5`. Ambos convergen en `create_job` — no hay motor duplicado.

**API PÚBLICA**: `cron.jobs.create_job(prompt, schedule, name, repeat, deliver, origin, skill, skills, model, provider, script, ...)`, `get_job`, `list_jobs`, `update_job`, `pause_job`, `resume_job`, `trigger_job`, `remove_job`. CLI: `hermes cron create <schedule> [prompt] --name --deliver --repeat --skill (repetible) --script --no-agent`. Agente: tool `cronjob` (`create/list/update/pause/resume/run/remove`).

**CÓMO SE EXTIENDE**: Nuevo trigger → proveedor `CronScheduler` en `plugins/cron_providers/<nombre>/`, activado vía `cron.provider: <nombre>` en config; si falla, cae al built-in (fallback nunca vive en `plugins/`). Nuevo blueprint → entrada en `CATALOG` de `cron/blueprint_catalog.py`. Nueva suggestion curada → `CatalogEntry` en `cron/suggestion_catalog.py`.

**GUARDAS DE SEGURIDAD**: `lifecycle_guard.py` (bloquea comandos que reinician/matan el propio gateway), `CronPromptInjectionBlocked` (regex anti-inyección + unicode invisible), `_guard_job_credential_exfil`, recursion guard (tool `cronjob` deshabilitada dentro de una sesión cron), sesión fresca sin historial por run.

**MADUREZ**: ≈49 archivos de test relacionados con cron (`tests/cron/` 35 archivos + dispersos en `tests/gateway`, `tests/hermes_cli`, `tests/tools`, `tests/agent`, `tests/monitoring`, `tests/plugins`), varios dirigidos a issues numerados específicos (locking, stall del ticker) → madurez alta, endurecido por incidentes reales.

**LÍMITES**: `jobs.json` es un único archivo por perfil (no sharding). Solo Chronos permite scale-to-zero (depende de infra de Nous). Sin gateway corriendo, los jobs solo disparan durante sesiones CLI activas. Timeout de script (3600s) y timeout de idle del agente (`HERMES_CRON_TIMEOUT`, 600s) son relojes independientes.

---

## D2 — TERMINAL Y BACKENDS DE EJECUCIÓN

**QUÉ HACE**: Terminal de usuario real (xterm.js + node-pty) en la app de escritorio, completamente separada del backend Python; más 8 backends de ejecución sandboxed que usa el AGENTE (no el usuario) vía la tool `terminal`.

**DÓNDE VIVE — terminal desktop**: `apps/desktop/src/app/right-sidebar/terminal/{persistent.tsx (222L), rail.tsx (168L), workspace.tsx (66L), use-agent-terminal.ts (171L)}`, lógica de revive en `use-terminal-session.ts` (1053L) + test `revive-buffer.test.ts`. PTY real en `apps/desktop/electron/main.ts` (`node-pty`, handler `hermes:terminal:start`), expuesta al renderer vía `contextBridge` en `electron/preload.ts` (`window.hermesDesktop.terminal.*`).

**Terminal de usuario vs terminal del agente**: Usuario = PTY real bidireccional. Agente = xterm de solo lectura (`disableStdin: true`), sin PTY, alimentado por stream de eventos (`agent.terminal.output`) sobre WebSocket desde el gateway Python — refleja procesos en background lanzados por la tool `terminal(background=true)`. **No comparten proceso.**

**REVIVE-BUFFER**: Serializa scrollback (tope 200 líneas / 48000 chars) al cerrar, lo limpia de prompts vacíos residuales, y lo reproduce al reabrir — **el proceso NO se revive**, solo el texto visual. Lee cwd real vía OSC 7/9;9 para reabrir en el último directorio.

**DÓNDE VIVE — backends del agente** (`tools/environments/`, todos heredan `BaseEnvironment` en `base.py`, 446L clase base):
| Backend | Archivo | Líneas |
|---|---|---|
| Local | `local.py` | 1627 |
| Docker | `docker.py` | 1945 |
| SSH | `ssh.py` | 375 |
| Singularity/Apptainer | `singularity.py` | 268 |
| Modal (directo) | `modal.py` | 478 |
| Modal (gestionado) | `managed_modal.py` | 282 |
| Daytona | `daytona.py` | 270 |
| Vercel Sandbox | `vercel_sandbox.py` | 662 |

Selección de backend en `tools/terminal_tool.py::_create_environment` (línea 1553). Utilidad compartida de sync de archivos: `tools/environments/file_sync.py`.

**CÓMO SE EXTIENDE**: Heredar `BaseEnvironment`, implementar `cleanup()`, `execute()`, `init_session()`, `stop()`; registrar el `env_type` como rama nueva en `_create_environment()`. Modelo "spawn-per-call": cada comando lanza un `bash -c` nuevo, estado de sesión se re-sourcea, cwd persiste vía marcadores de stdout/archivo temporal.

**MADUREZ**: Tests dispersos (compilados `.pyc` confirmados, fuente no verificado 1:1): `test_terminal_tool_pty_fallback`, `test_daytona_terminal`, `test_modal_terminal`, `test_pty_bridge`, `test_win_pty_bridge`. Lado desktop: `persistent.test.tsx` (312L, más grande que el propio componente), `terminals.test.ts`, `rail.test.tsx`, `revive-buffer.test.ts`, `clipboard.test.ts`, `terminal-backend-panel.test.tsx`.

**LÍMITES**: Terminal del agente es estrictamente unidireccional (sin paste). Primer mount de xterm se difiere hasta tener dimensiones reales (evita bootear PTY a 0×0 en Windows).

---

## D3 — PLATAFORMAS DE MENSAJERÍA *(NO VERIFICADO a fondo — sub-agente detenido)*

**Evidencia indirecta recolectada** (desde el reporte de D1): el sistema de entrega de cron (`--deliver platform:<destino>:<thread>`) soporta como plataformas nombradas: Telegram, Discord, Slack, Matrix, Feishu, WhatsApp, Signal, SMS, Email, Weixin, Mattermost, Home Assistant, DingTalk, WeCom, BlueBubbles, QQ Bot — lo que confirma fuertemente que existen adaptadores para todas estas en `plugins/platforms/` y/o `gateway/platforms/`, aunque **no se verificó el archivo de cada adaptador, su tamaño, ni la interfaz base**.

**NO VERIFICADO específicamente**:
- Diferencia exacta entre `plugins/platforms/` y `gateway/platforms/`.
- Contenido de `gateway/platforms/ADDING_A_PLATFORM.md` y `gateway/platforms/base.py`.
- Detalle de `telegram/` y `whatsapp/`/`whatsapp_cloud.py` (líneas, capacidades de media/botones/hilos/reacciones).
- Mecanismo de autenticación Meta Graph API y verificación de firma de webhook en `whatsapp_cloud.py`.

**Recomendación**: repetir la auditoría de D3 en una pasada dedicada antes de decidir cómo integrar Instagram (reutilizando el patrón de `whatsapp_cloud.py` si aplica, como sugería el prompt original).

---

## D4 — WEBHOOKS E INGESTA

**QUÉ HACE (confirmado vía D1)**: `hermes webhook subscribe` crea suscripciones a eventos externos con entrega configurable. `gateway/platforms/webhook.py` implementa rutas declarativas: cada `route` en `config.yaml` bajo `platforms.webhook.extra.routes` define `events` (filtro), `secret` (HMAC), `prompt` (template), `skills` (a cargar), `deliver`/`deliver_extra`, y `deliver_only` (modo sin LLM, "zero LLM cost", notificación pura).

**DÓNDE VIVE**: `hermes_cli/subcommands/webhook.py` (`--events`, `--prompt`, `--deliver`, `--secret` — auto-generado si se omite), `gateway/platforms/webhook.py`.

**AUTENTICACIÓN**: HMAC (secret por ruta, auto-generado si no se especifica).

**NO VERIFICADO**: contenido completo de `api_server.py`, lista exhaustiva de endpoints HTTP expuestos por el sistema, si existe pre-procesamiento de webhook configurable antes de invocar al agente más allá de `deliver_only`.

---

## D5 — GENERACIÓN DE MEDIOS *(NO VERIFICADO a fondo — sub-agente detenido)*

**Evidencia indirecta recolectada** (desde reportes de D8 y D13):

Backends por plugin (D8): `plugins/image_gen/` → 7 sub-plugins con `plugin.yaml` propio. `plugins/video_gen/` → 3 sub-plugins.

Herramientas que ve el agente (D13, `tools/*.py`):
| Archivo | Líneas | Área |
|---|---|---|
| `tools/image_generation_tool.py` | 1668 | Imagen |
| `tools/flux3_video_tool.py` | — | Video |
| `tools/video_generation_tool.py` | — | Video |
| `tools/xai_video_tools.py` | — | Video (xAI) |
| `tools/vision_tools.py` | — | Visión/análisis de imagen |
| `tools/tts_tool.py` | 3676 | TTS |
| `tools/tts_streaming.py` | — | TTS streaming |
| `tools/neutts_synth.py` | — | Síntesis de voz |
| `tools/voice_mode.py` | — | Modo de voz |
| `tools/wake_word.py` | — | Wake word |
| `tools/transcription_tools.py` | 2674 | Transcripción (STT) |
| `tools/audio_container.py` | — | Contenedor de audio |

**NO VERIFICADO específicamente**: qué modelos soporta cada backend, cómo se seleccionan modelo/backend y dónde se configura, si existe composición/post-procesado de imágenes (plantillas, redimensionado, texto sobre imagen — **evidencia apunta a que NO existe**, ver Huecos Confirmados), estructura exacta de un `plugin.yaml` de `image_gen`/`video_gen`, y si hay almacenamiento en la nube (S3/R2) o alojamiento de media en URLs HTTPS públicas.

**Recomendación**: repetir auditoría de D5 dedicada — es el dominio más relevante para el caso de uso de contenido/redes del usuario y el que menos profundidad tiene en este documento.

---

## D6 — MEMORIA

**QUÉ HACE**: Sistema de proveedores de memoria plugin-based. Orquestación en `agent/memory_manager.py` (1241L) y `agent/memory_provider.py` (315L, ABC `MemoryProvider`). Loader en `plugins/memory/__init__.py` (462L) escanea bundled + `$HERMES_HOME/plugins/`.

**PROVEEDORES (8)**: `byterover` (CLI externo `brv`, tiered retrieval), `hindsight` (grafo de conocimiento, entity resolution), `holographic` (100% local: SQLite+FTS5, sin dependencias cloud), `honcho` (modelado de usuario cross-session, OAuth propio), `mem0` (extracción de hechos server-side, dedup automático), `openviking` (context base tipo filesystem navegable), `retaindb` (API cloud, búsqueda híbrida, 7 tipos de memoria), `supermemory` (memoria semántica, profile recall).

**AISLAMIENTO POR CONTEXTO**: NO hay namespaces internos — **"Only ONE provider can be active at a time"** (`plugins/memory/__init__.py:12`, confirmado también en `memory_manager.py` ~línea 419-421). Para aislar memoria por marca/cliente, la única vía real es el sistema de **perfiles** (`hermes_cli/profiles.py`), no namespaces del memory_manager.

**`query_rewrite.py`** (140L): reescribe el último mensaje del usuario en pregunta de retrieval limpia en inglés vía LLM auxiliar, con validaciones anti-inyección. Provider-agnostic; no implementa aislamiento por marca.

**CÓMO SE EXTIENDE**: nuevo proveedor = carpeta en `plugins/memory/<nombre>/` con `plugin.yaml` + implementar `MemoryProvider`.

**RIESGO A EVITAR RECONSTRUIR**: cualquier idea de "memoria multi-proveedor simultánea" o "namespace interno" ya está descartada por diseño — usar perfiles en su lugar.

---

## D7 — SISTEMA DE SKILLS

**CONTEO**: `skills/` = 70 SKILL.md. `optional-skills/` = 111 SKILL.md. Total 181.

**Categorías `skills/`** (14): apple=5, autonomous-ai-agents=6, creative=17, email=2, github=7, index-cache=3, media=4, mlops=5, note-taking=2, productivity=12, research=6, smart-home=2, software-development=11, **social-media=2**.

**Categorías `optional-skills/`** (20): incluye mlops=29 (la más grande), research=12, creative=13, finance=8, devops=5, security=7, etc.

**FORMATO** (ejemplo real, `skills/social-media/xurl/SKILL.md`, 437L): frontmatter YAML (`name`, `description`, `version`, `author`, `license`, `platforms`, `prerequisites`, `metadata.hermes.{tags,homepage,upstream_skill}`) + cuerpo estructurado (Use cases → Secret Safety → Installation → Setup manual → Quick Reference → Command Details → Agent Workflow → Troubleshooting → Attribution).

**DESCUBRIMIENTO**: `tools/skill_manager_tool.py` (1768L) + `agent/skill_utils.py` (918L) + `agent/skill_commands.py` (812L) — camina directorios buscando `SKILL.md`, parsea frontmatter, filtra por plataforma/entorno/disabled, cachea índice en `agent/prompt_builder.py` (snapshot `.skills_prompt_snapshot.json`).

**CUÁNDO USA EL AGENTE UNA SKILL**: el índice (nombre + descripción truncada a 57 chars) se inyecta en el system prompt junto con `SKILLS_GUIDANCE`; decisión semántica del LLM, no un matcher de keywords separado.

**COMPATIBLE CON agentskills.io**: SÍ, confirmado (`tools/skills_tool.py` líneas 28-46, 1438, 1453, 1691-1695) — frontmatter y directorio `assets/` siguen el estándar de agentskills.io.

**CREACIÓN EN RUNTIME**: SÍ — `skill_manage(action='create')` recibe frontmatter+body completo y escribe el SKILL.md; el agente es instruido a guardar workflows nuevos como skills proactivamente.

**CÓMO AÑADIR UNA SKILL PROPIA**: carpeta `<categoría>/<nombre>/SKILL.md` bajo `skills/`, `optional-skills/`, o `$HERMES_HOME/skills/`, con subcarpetas opcionales `references/`, `templates/`, `assets/`, `scripts/`. Se descubre en el siguiente `reload_skills()`/`/reload-skills` sin reiniciar el proceso.

---

## D8 — SISTEMA DE PLUGINS

**CONTEO**: `plugins/` = 18 entradas de primer nivel (16 carpetas de plugin reales). Varias son contenedoras de sub-plugins: `browser/` (3), `model-providers/` (~30), `platforms/` (~17), `image_gen/` (7), `video_gen/` (3), `web/` (8), `memory/` (8), `dashboard_auth/` (4), `observability/` (2). Total `plugin.yaml` en el árbol: **97**.

**ESQUEMA DE `plugin.yaml`** (consolidado de varios ejemplos reales):
```yaml
name: <str>                    # requerido
label: <str>
kind: model-provider|platform|backend
version: <str>
description: <str>
author: <str>
hooks: [on_pre_compress, on_session_end, post_tool_call, transform_tool_result, pre_tool_call, ...]
pip_dependencies: [pkg>=x.y.z]
external_dependencies:
  - {name: <binario>, install: "<comando>", check: "<comando --version>"}
requires_env:
  - {name: TOKEN_VAR, description: "...", prompt: "...", url: "https://...", password: true|false}
optional_env: [...]  # mismo shape
provides_web_providers: [tavily]
provides_tools: [spotify_playback, ...]
```
Ejemplo mínimo real (`plugins/model-providers/anthropic/plugin.yaml`, 5 líneas): `name`, `kind`, `version`, `description`, `author`.

**CARGA/REGISTRO**: `hermes_cli/plugins.py` (2485L) + `hermes_cli/plugins_cmd.py` (2082L). 4 fuentes en orden de precedencia: (1) bundled `<repo>/plugins/<name>/` (excluye `memory/` y `context_engine/`, que tienen su propio discovery), (2) `~/.hermes/plugins/<name>/`, (3) `./.hermes/plugins/<name>/` (opt-in vía `HERMES_ENABLE_PROJECT_PLUGINS`), (4) pip packages vía entry-point `hermes_agent.plugins`. Cada plugin requiere `plugin.yaml` + `__init__.py` con `register(ctx)`. Core llama `invoke_hook(name, **kwargs)`; `PluginContext.register_tool()` delega a `tools.registry.register()`.

**RIESGO A EVITAR RECONSTRUIR**: loader de plugins genérico (4 fuentes, sistema de hooks completo) y gestión de credenciales de plugin (`requires_env`/`optional_env` ya con UI de prompt asociada) ya existen — no reconstruir.

---

## D9 — MCP (Model Context Protocol)

**QUÉ HACE**: Doble rol. (a) Servidor: expone mensajería/conversaciones de Hermes como tools MCP (`mcp_serve.py`, 9 tools: `conversations_list`, `conversation_get`, `messages_read`, `attachments_fetch`, `events_poll`, `events_wait`, `messages_send`, `permissions_list_open`, `permissions_respond`, `channels_list`). **No expone tools genéricas del usuario.** (b) Cliente: se conecta a servidores MCP externos (stdio/HTTP/SSE) y registra sus tools como nativas del agente.

**DÓNDE VIVE**: `mcp_serve.py` (982L), `tools/mcp_tool.py` (6829L — el archivo más grande del repo), `tools/mcp_oauth.py` (1369L), `tools/mcp_oauth_manager.py` (785L), `hermes_cli/mcp_config.py` (1135L), `hermes_cli/mcp_catalog.py` (812L), `hermes_cli/subcommands/mcp.py` (126L).

**CATÁLOGO CURADO** (`optional-mcps/`): 6 entradas (`blender`, `comfy-cloud`, `figma`, `linear`, `n8n`, `unreal-engine`), cada una con `manifest.yaml` (transport, comando pineado a versión exacta, allowlist de tools por defecto, advertencias de seguridad explícitas). Instalación: `hermes mcp install <name>` o `hermes mcp catalog`/`picker`.

**CÓMO EXPONDRÍA EL USUARIO SUS PROPIAS HERRAMIENTAS VÍA MCP**: NO hay un exportador genérico de "convierte tu script en tool MCP". La vía soportada es la inversa: el usuario escribe su propio servidor MCP (stdio/HTTP) y lo conecta con `hermes mcp add`.

**API PÚBLICA**: Cliente: `mcp_servers` en `config.yaml` + `hermes mcp add/remove/list/test/configure/login/reauth/install/catalog/picker`. Servidor: `hermes mcp serve [-v]`.

**CÓMO SE EXTIENDE**: nuevo servidor externo = solo config, no código. Nuevo entry en catálogo curado = `manifest.yaml` en `optional-mcps/<name>/` + PR review.

---

## D10 — APP DE ESCRITORIO

**STACK** (`apps/desktop/package.json`): Electron 40.10.2, Vite ^8.0.10, React ^19.2.5, TypeScript ^6.0.3, electron-builder ^26.8.1, Vitest ^4.1.5, Tailwind CSS ^4.2.4 (vía plugin de Vite, sin `tailwind.config.js` clásico), xterm.js `@xterm/xterm ^6.0.0` + `node-pty 1.1.0`, Playwright `=1.58.2`, React Router `^7.17.0`, UI kit interno `@nous-research/ui ^0.13.0`. Node `^20.19.0 || >=22.12.0`.

**ELECTRON-BUILDER**: config inline en `package.json` (bloque `build`, sin YAML separado). appId `com.nousresearch.hermes`, protocolo custom `hermes://`. Mac: `dmg`/`zip`, hardened runtime + notarización (`afterSign: scripts/notarize.mjs`). Win: `nsis`/`msi`, instalador no one-click. Linux: `AppImage`/`deb`/`rpm`. `asarUnpack` para `**/*.node` y `**/prebuilds/**` (necesario por `node-pty`).

**MAPA DE MÓDULOS** (`apps/desktop/src/app/`):
| Carpeta | Qué hace |
|---|---|
| `agents` | Vista/listado de agentes |
| `artifacts` | Panel de artifacts generados |
| `chat` | Conversación: composer, drag&drop de sesiones, estado de runtime |
| `command-center` | Panel central de comandos/mantenimiento |
| `command-palette` | Cmd+K, incluye marketplace de temas |
| `contrib` | **Sistema de plugins/contribuciones de la app** (extensibilidad) |
| `cron` | UI de programador de tareas |
| `gateway` | Hooks de integración con el gateway Python |
| `learning` | Diálogo de archivado de skills aprendidas |
| `messaging` | Integraciones de mensajería (ruta `/messaging`) |
| `overlays` | Framework genérico de overlays/paneles modales |
| `profiles` | Gestión de perfiles/cuentas (ruta `/profiles`) |
| `right-sidebar` | Archivos, revisión git, terminal (ver D2) |
| `session` | Resolución de sesión/workspace activo |
| `settings` | Apariencia, billing, credenciales, backend de terminal, gateway |
| `shell` | "Chrome" de la app: titlebar, statusbar, menús de modelo/aprobación |
| `skills` | Hub de skills + pestaña de servidores MCP |
| `starmap` | Visualización tipo mapa estelar (usa `d3-force`) |
| `webhooks` | Gestión de webhooks (ruta `/webhooks`) |

**COMUNICACIÓN FRONTEND↔BACKEND**: UI React → IPC (`contextBridge`, `preload.ts`) → proceso Electron → HTTP+WebSocket local (`http://127.0.0.1:{port}`, `ws://127.0.0.1:{port}/api/ws`) → backend Python (proceso hijo, `electron/backend-child.ts`). Excepción: terminal de usuario usa IPC directo a `node-pty`, sin pasar por Python.

**CÓMO AÑADIR MÓDULO/PANTALLA NUEVA**: rutas nativas vía `routes.ts` (`APP_ROUTES`, usa `registry` de `@/contrib/registry`); o sistema "contrib" (`apps/desktop/src/app/contrib/wiring.tsx`, 1092L) — `AppView` tiene un valor `'extension'` explícito para "página completa aportada por plugin". **NO VERIFICADO en profundidad** el contenido interno de `wiring.tsx`.

**TEMAS**: Tailwind v4 + `apps/desktop/src/styles.css` (2216L, variables CSS) + `apps/desktop/src/themes/` (soporte de importación de temas de VS Code vía `vscode.ts`/`vscode-marketplace.ts`).

**i18n**: `apps/desktop/src/i18n/` — 5 idiomas (en 2957L, ar, ja, zh, zh-hant), formato TypeScript tipado (no JSON/.po). Plugins pueden registrar bundles propios (`plugin-i18n.ts::registerPluginLocales`).

---

## D11 — FACTURACIÓN, CRÉDITOS Y SUSCRIPCIÓN

**DÓNDE VIVE**: `agent/credits_tracker.py` (852L), `agent/billing_usage.py` (323L), `agent/billing_view.py` (511L), `agent/subscription_view.py` (507L), `agent/usage_pricing.py` (1432L), `agent/portal_tags.py` (144L), `agent/billing_links.py` (124L), `hermes_cli/nous_billing.py` (675L), `hermes_cli/nous_subscription.py` (1302L), `hermes_cli/cli_billing_mixin.py`.

**MEDICIÓN DE CONSUMO**: Por **tokens**, no por operación. `CanonicalUsage` (`usage_pricing.py:31`): input/output/cache_read/cache_write/reasoning tokens + request_count. Costo vía `PricingEntry` (USD por millón de tokens). `credits_tracker.py` parsea headers `x-nous-credits-*` en `CreditsState` — dinero como enteros "micros", nunca float.

**CÓMO SE MUESTRAN LOS CRÉDITOS**: comando `/topup` en chat interactivo; `BillingState`/`UsageModel` (`billing_view.py`) y `SubscriptionState` (`subscription_view.py`) alimentan pantallas TUI/gateway.

**AGOTAMIENTO**: propiedad `depleted` = `not paid_access` (NUNCA se infiere de `remaining_micros==0`, evita falsos positivos). Notificación sticky `credits.depleted`. Bandas de aviso progresivo al 50/75/90% del cap.

**ACOPLAMIENTO A NOUS PORTAL**: **Total**. `portal_tags.py` etiqueta cada request para atribución de Nous. `nous_billing.py` es cliente REST concreto a `https://portal.nousresearch.com` con contrato JSON propietario (rutas `/api/billing/*`, errores `stripe_unavailable`, `insufficient_scope`, `monthly_cap_exceeded`).

**STRIPE**: Ya está en uso, pero **indirectamente** — es el procesador *detrás* del Nous Portal, Hermes nunca lo llama directo. Evidencia: `BillingStripeUnavailable` (mapea error 503 del Portal), comentario literal "The SINGLE money route: one Stripe op prorates, charges the card already on the Portal" (`nous_billing.py:656`), redacción de `sk_live_`/`sk_test_` en `agent/redact.py:89-91`. **No existe interfaz `BillingProvider` abstracta.**

**PARA CONECTAR STRIPE PROPIO**: reemplazar `nous_billing.py` por cliente Stripe propio; reemplazar auth OAuth-Nous por claves API propias; reescribir `credits_tracker.py` (depende 100% de headers propietarios `x-nous-credits-*`); reutilizables: `CanonicalUsage`/`PricingEntry`/`CostResult` (medición) y el patrón multi-proveedor de `billing_links.py` (`_PROVIDERS` tuple).

---

## D12 — NAVEGADOR Y WEB *(parcialmente verificado vía D8/D13, no auditado a fondo)*

**QUÉ HACE (confirmado)**: `plugins/browser/` con 3 sub-plugins confirmados: `browser_use`, `browserbase`, `firecrawl` (D8). Herramientas del lado del agente (D13): `tools/browser_tool.py`, `tools/browser_camofox.py`, `tools/browser_camofox_state.py`, `tools/browser_cdp_tool.py`, `tools/browser_dialog_tool.py`, `tools/browser_supervisor.py`.

**NO VERIFICADO**: diferencia funcional exacta entre los 3 backends (browser_use vs browserbase vs firecrawl), cuándo el agente elige cada uno, contenido de `plugin.yaml` de cada uno.

---

## D13 — NÚCLEO DEL AGENTE

**ARCHIVOS RAÍZ**: `toolsets.py` (1003L), `run_agent.py` (7410L).

**BUCLE PRINCIPAL**: `agent/conversation_loop.py` (7040L, extraído de `run_agent.AIAgent`). Función única masiva `run_conversation` (línea 1084). Por turno: construye/valida system prompt, gestiona failover entre proveedores, aplica compresión de contexto pre-API, llama al modelo con reintentos/backoff, clasifica errores, detecta bloqueos de billing/entitlement (ver D11), sanitiza mensajes, dispara hooks de revisión en background (memoria/skills).

**SUBAGENTES**: `tools/delegate_tool.py` (3974L) — spawns de `AIAgent` hijos con contexto aislado, toolsets heredados menos `DELEGATE_BLOCKED_TOOLS` (delegate_task, clarify, memory, send_message, cronjob — sin recursión). Paralelización vía **`ThreadPoolExecutor`** (no asyncio), modos single-task y batch. `tools/async_delegation.py` (1461L) para delegación en background con `DaemonThreadPoolExecutor`, resultado se drena en el próximo turno ocioso.

**TOOLSETS**: `toolsets.py` — `_HERMES_CORE_TOOLS`, `get_toolset()`, `resolve_toolset()`, `get_all_toolsets()`. Restricción por contexto vía `check_fn` (ej. Home Assistant gated por `HASS_TOKEN`).

**HERRAMIENTAS** (`tools/*.py`, 106 archivos) agrupadas: Archivos/FS (`file_operations.py` 2517L), Terminal/Proceso (`terminal_tool.py` 3226L, `process_registry.py` 2422L), Browser (ver D12), Mensajería (`send_message_tool.py` 2111L, `discord_tool.py` 1116L), Medios (ver D5), MCP (`mcp_tool.py` 6829L — el más grande del repo), Delegación (`delegate_tool.py`), Memoria (`memory_tool.py` 1240L), Kanban (`kanban_tools.py` 2156L), Skills (`skills_hub.py` 4151L), Seguridad (`approval.py` 4161L), Cron (`cronjob_tools.py`).

**AÑADIR UNA TOOL NUEVA**: `tools/registry.py` (873L) — auto-descubrimiento vía AST (`discover_builtin_tools()`, escanea con `ast.parse` sin ejecutar el módulo, cacheado por `(mtime_ns, size)`). Crear `tools/nueva_tool.py` con `registry.register()` a nivel de módulo — se auto-descubre.

**PROVEEDORES DE MODELO**: `providers/base.py` + adaptadores en `agent/` (`anthropic_adapter.py`, `azure_identity_adapter.py`, `bedrock_adapter.py`, `codex_responses_adapter.py`, `gemini_native_adapter.py`, `vertex_adapter.py`). Soporta ~24 proveedores vía config (`auto`, `openrouter`, `nous`, `anthropic`, `gemini`, `ollama`, `lmstudio`, `custom`, etc.).

---

## D14 — SEGURIDAD

**CREDENCIALES**: **Sin cifrado propio en reposo** (0 coincidencias de encrypt/fernet/keyring en `agent/credential_pool.py` 2806L y `agent/credential_persistence.py` 174L). Viven en texto plano en `~/.hermes/.env`/`auth.json`. `credential_persistence.py` sanea qué se persiste (elimina valores crudos de proveedores externos antes de escribir a disco). Integraciones de vault externo: `agent/secret_sources/{bitwarden,onepassword}.py` (resuelven secretos vía CLI externa `bws`/`op` en el arranque). `agent/secret_scope.py`: aislamiento por perfil vía `ContextVar`, falla explícito si no hay scope en modo multiplex.

**ANTI-INYECCIÓN DE PROMPTS**: `class CronPromptInjectionBlocked` (`cron/scheduler.py:147`), `_guard_job_credential_exfil` (`cron/scheduler.py:2707`), patrones regex en `tools/cronjob_tools.py:80,98,202`.

**APROBACIONES**: `hermes_cli/subcommands/approvals.py` (minería de historial → propuestas de allowlist, excluye clases destructivas de auto-propuesta). `tools/approval.py` (4161L, "single source of truth"): `DANGEROUS_PATTERNS` (47 DANGEROUS + 12 HARDLINE), estado por sesión thread-safe, prompting interactivo CLI+gateway, **"smart approval"** vía LLM auxiliar para auto-aprobar riesgo bajo, allowlist persistente. Modo YOLO (`HERMES_YOLO_MODE`) congelado en import time (anti-escalación por prompt-injection). Cron tiene su propio modo de aprobación independiente.

**SANDBOXING**: `tools/code_execution_tool.py` (2014L) + `tools/environments/` (ver D2). Docker hardening confirmado: `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit`, límites opcionales de `--cpus`/`--memory`, `--network=none` disponible con verificación posterior del modo real de red.

**CI/CD DE SUPPLY CHAIN**: `.github/workflows/osv-scanner.yml` (escanea `uv.lock`/`package-lock.json` contra OSV, no bloquea merge). `.github/workflows/supply-chain-audit.yml` (escáner de alta señal: detecta `.pth` nuevos/modificados, combo `base64 decode + exec/eval`, `subprocess` con args ofuscados, archivos install-hook sin label `ci-reviewed`; falla el build si dependencias PyPI nuevas no tienen cota superior de versión).

**LÍMITES**: sin cifrado en reposo propio — depende de permisos de archivo (0600) o vaults externos opcionales. NO VERIFICADO si hay cifrado a nivel de filesystem/HSM para enterprise.

---

## D15 — CLI, CONFIGURACIÓN Y TESTS

**SUBCOMANDOS** (`hermes_cli/subcommands/*.py`, 38): `acp`, `approvals`, `auth`, `backup`, `claw`, `config`, `console`, `cron`, `dashboard`, `debug`, `doctor`, `dump`, `gateway`, `gui`, `hooks`, `import-agent`, `import`, `insights`, `login`, `logout`, `logs`, `mcp`, `memory`, `model`, `monitoring`, `pairing`, `plugins`, `profile`, `prompt-size`, `security`, `setup`, `skills`, `skin`, `slack`, `status`, `sync`, `tools`, `uninstall`, `update`, `version`, `webhook`, `whatsapp`.

**`cli-config.yaml.example`**: secciones top-level: `database`, `model` (~24 proveedores), `terminal`, `browser`, `tool_loop_guardrails`, `compression`, `prompt_caching`, `memory`, `session_reset`, `streaming`, `skills`, `agent`, `platform_toolsets`, `stt`, `code_execution`, `delegation`, `display`, `telemetry`, `updates`.

**`.env.example`** (24KB): organizado por proveedor de LLM, cada bloque con URL de obtención de key. Vistos: Fireworks AI, OpenRouter, NovitaAI, Google AI Studio/Gemini, Ollama Cloud, z.ai/GLM, Kimi/Moonshot, Arcee AI, MiniMax, OpenCode Zen (y más, no verificado el archivo completo).

**TESTS**: **2571 archivos** `.py` bajo `tests/`. Subdirectorios temáticos: `acp/`, `agent/`, `ci/`, `cli/`, `computer_use/`, `conformance/`, `cron/`, `dashboard/`, `docker/`, `e2e/`, `fakes/`, `fixtures/`, `gateway/`, `hermes_cli/`, `hermes_state/`, `honcho_plugin/`, `integration/`, `manual/`, `monitoring/`, `openviking_plugin/`, `plugins/`, `providers/`, `run_agent/`, `scripts/`, `secret_sources/`, `skills/`, y más.

**WORKFLOWS** (`.github/workflows/`, 22 archivos, solo por nombre — NO VERIFICADO contenido): `ci.yml`, `contributor-check.yml`, `deploy-site.yml`, `docker.yml`, `docker-lint.yml`, `docs-site-checks.yml`, `e2e-desktop.yml`, `history-check.yml`, `infographic-check.yml`, `js-autofix.yml`, `js-tests.yml`, `label-rerun.yml`, `lint.yml`, `lockfile-diff.yml`, `osv-scanner.yml`, `publish-e2e-evidence.yml`, `review-labels.yml`, `skills-index.yml`, `skills-index-freshness.yml`, `supply-chain-audit.yml`, `tests.yml`, `uv-lockfile-check.yml`.

---

## VERIFICACIÓN DE LA LISTA DE 20 IDEAS

| # | Idea | Veredicto | Ruta / evidencia |
|---|---|---|---|
| 1 | Terminal integrada en la app | **YA EXISTE** | `apps/desktop/src/app/right-sidebar/terminal/*`, `tools/terminal_tool.py`, `tools/read_terminal_tool.py`, `tools/process_registry.py` |
| 2 | Automatizaciones con reglas propias corriendo localmente | **EXISTE PARCIAL** | No hay "rule engine" con lógica condicional arbitraria, pero sí `gateway/platforms/webhook.py` con rutas declarativas (`events`, `prompt`, `deliver`, `deliver_only`) — automatización local basada en eventos externos, configurada declarativamente |
| 3 | Programación de tareas recurrentes | **YA EXISTE** | `cron/scheduler.py`, `cron/jobs.py` — cron 5-campos, intervalos, one-shot |
| 4 | Recibir instrucciones desde Telegram/WhatsApp | **YA EXISTE (evidencia indirecta, D3 no auditado a fondo)** | Confirmado como targets de entrega de cron; adaptadores probablemente en `plugins/platforms/`/`gateway/platforms/` — repetir D3 para confirmar archivo por archivo |
| 5 | Generación de imágenes con IA | **YA EXISTE (evidencia indirecta, D5 no auditado a fondo)** | `plugins/image_gen/` (7 backends), `tools/image_generation_tool.py` |
| 6 | Generación de video con IA | **YA EXISTE (evidencia indirecta, D5 no auditado a fondo)** | `plugins/video_gen/` (3 backends), `tools/video_generation_tool.py`, `tools/flux3_video_tool.py` |
| 7 | Composición de imagen: plantillas, texto, formatos sociales | **NO EXISTE** (sin evidencia) | — |
| 8 | Publicación en Instagram/Facebook | **NO VERIFICADO** | `skills/social-media/` solo tiene 2 skills, una es xurl (X); la otra sin identificar — repetir D3/D5 |
| 9 | Publicación en TikTok/LinkedIn/YouTube | **NO VERIFICADO**, sin evidencia | — |
| 10 | Publicación en X/Twitter | **YA EXISTE** | `skills/social-media/xurl/SKILL.md` (437L), `tools/x_search_tool.py` |
| 11 | Calendario de contenido | **NO EXISTE** (confirmado, 0 resultados grep) | — |
| 12 | Multi-marca/multi-cliente con configuración aislada | **YA EXISTE** | `hermes_cli/profiles.py` (2226L) — instancias `HERMES_HOME` completamente independientes |
| 13 | Perfiles de voz/tono de marca persistentes | **YA EXISTE (parcial)** | `SOUL.md` por perfil (`hermes_cli/default_soul.py`), archivo libre no estructurado |
| 14 | Métricas de rendimiento de publicaciones | **NO VERIFICADO**, sin evidencia | — |
| 15 | Medición de consumo y créditos por usuario | **YA EXISTE** | `agent/usage_pricing.py`, `agent/credits_tracker.py` |
| 16 | Integración de pagos (Stripe u otro) | **EXISTE PARCIAL, no reutilizable directo** | Stripe opera detrás del Nous Portal (`hermes_cli/nous_billing.py`); sin interfaz `BillingProvider` abstracta |
| 17 | Aprobación humana antes de acciones sensibles | **YA EXISTE, muy maduro** | `tools/approval.py` (4161L), `tools/write_approval.py` |
| 18 | Almacenamiento de archivos en la nube (S3, R2) | **NO VERIFICADO** (D5 no completado) | — |
| 19 | Alojamiento de media en URLs públicas HTTPS | **NO VERIFICADO** (D5 no completado) | — |
| 20 | Gestión de tokens OAuth con refresco automático | **YA EXISTE** | `tools/mcp_oauth_manager.py`, `agent/azure_identity_adapter.py`, auth Nous vía `get_provider_auth_state()` |

**Resumen**: de 20 ideas, **11 ya existen completas** (1,3,4*,5*,6*,10,12,13,15,17,20 — *4/5/6 con evidencia indirecta, no archivo-por-archivo), **2 existen parcial** (2, 16), **2 no verificadas por dominio incompleto pero con pista de que podrían existir** (8), y **5 son huecos reales o no verificados sin pista** (7, 9, 11, 14, 18, 19 — nota: son 6, ver conteo abajo).

Conteo estricto: **YA EXISTE** = 8 (#1,3,4,5,6,10,15,17,20 → en realidad 9). Recuento cuidadoso:
- YA EXISTE (completo): 1, 3, 4, 5, 6, 10, 12, 13, 15, 17, 20 = **11**
- EXISTE PARCIAL: 2, 16 = **2**
- NO EXISTE (confirmado): 7, 11 = **2**
- NO VERIFICADO (sin evidencia, dominio incompleto): 8, 9, 14, 18, 19 = **5**

**Total ideas ya cubiertas de alguna forma (completa o parcial): 13 de 20.**

---

## MAPA DE ARCHIVOS CLAVE

| Ruta | Líneas | Qué hace |
|---|---|---|
| `cron/scheduler.py` | 4364 | Motor de scheduler: locks, claim/heartbeat, guardas de seguridad |
| `cron/jobs.py` | 2609 | CRUD de jobs, `jobs.json`, parse de schedules |
| `run_agent.py` | 7410 | Entry point del agente (raíz) |
| `agent/conversation_loop.py` | 7040 | Bucle principal de conversación (`run_conversation`) |
| `tools/mcp_tool.py` | 6829 | Cliente MCP — el archivo más grande del repo |
| `tools/skills_hub.py` | 4151 | Hub de skills |
| `tools/approval.py` | 4161 | Sistema de aprobación de comandos peligrosos |
| `tools/delegate_tool.py` | 3974 | Delegación a subagentes (ThreadPoolExecutor) |
| `tools/tts_tool.py` | 3676 | Text-to-speech |
| `tools/terminal_tool.py` | 3226 | Tool de terminal del agente, selección de backend |
| `cron/jobs.py` | 2609 | (ver arriba) |
| `tools/transcription_tools.py` | 2674 | Speech-to-text |
| `agent/credential_pool.py` | 2806 | Pool de credenciales (sin cifrado propio) |
| `hermes_cli/plugins.py` | 2485 | Loader de plugins (4 fuentes) |
| `tools/process_registry.py` | 2422 | Registro de procesos en background |
| `hermes_cli/profiles.py` | 2226 | Perfiles multi-marca/multi-cliente |
| `apps/desktop/src/styles.css` | 2216 | CSS principal de la app |
| `hermes_cli/plugins_cmd.py` | 2082 | CLI de gestión de plugins |
| `tools/kanban_tools.py` | 2156 | Tablero Kanban multi-agente |
| `tools/file_operations.py` | 2517 | Operaciones de archivos |
| `apps/desktop/src/i18n/en.ts` | 2957 | Catálogo de strings en inglés |
| `cron/blueprint_catalog.py` | 713 | Catálogo de blueprints de automatización |
| `hermes_cli/nous_subscription.py` | 1302 | Features por tier de suscripción Nous |
| `agent/usage_pricing.py` | 1432 | Medición de uso/costo por token |
| `tools/mcp_oauth.py` | 1369 | OAuth para servidores MCP remotos |
| `apps/desktop/electron/preload.ts` | 336 | Contrato IPC completo (contextBridge) |
| `tools/environments/base.py` | 446 (clase base l.446) | Interfaz `BaseEnvironment` para backends de ejecución |
| `tools/environments/docker.py` | 1945 | Backend Docker (hardened) |
| `tools/environments/local.py` | 1627 | Backend local |
| `tools/registry.py` | 873 | Auto-descubrimiento de tools vía AST |
| `hermes-already-has-routines.md` | ~6.5KB | Doc interna sobre el sistema de cron/routines (raíz del repo) |
| `cli-config.yaml.example` | 90KB | Config de referencia completa |
| `.env.example` | 24.8KB | Variables de entorno por proveedor |
| `AGENTS.md` | 76.7KB | Guía interna para agentes que contribuyen al repo |

*(Lista no exhaustiva de 50 — prioriza los archivos más grandes/centrales confirmados durante la auditoría; varios archivos de D3/D5/D12 con líneas exactas quedaron fuera por no completarse esos dominios.)*

---

## PUNTOS DE EXTENSIÓN

Formas de añadir funcionalidad **sin tocar el núcleo**:

1. **Plugins** (`plugins/<name>/plugin.yaml` + `__init__.py::register(ctx)`) — 4 fuentes de carga (bundled, user, project opt-in, pip entry-point). Cubre: proveedores de modelo, plataformas de mensajería, memoria, backends de imagen/video, backends de navegador, dashboard auth, observabilidad.
2. **Skills** (`<categoría>/<nombre>/SKILL.md`) — recetas declarativas que el LLM decide usar por semántica; compatibles con agentskills.io; el agente puede crearlas solo en runtime.
3. **Cron providers** (`plugins/cron_providers/<name>/`) — nuevo mecanismo de disparo de triggers, cae a fallback in-process si falla.
4. **Cron blueprints/suggestions** — entradas nuevas en `CATALOG` de `cron/blueprint_catalog.py` / `cron/suggestion_catalog.py`, sin tocar el motor.
5. **Tools del agente** (`tools/<name>.py` con `registry.register()` a nivel de módulo) — auto-descubierto vía AST, sin lista central que mantener.
6. **Backends de ejecución/terminal** (`tools/environments/<name>.py`, hereda `BaseEnvironment`) — registrar en `_create_environment()`.
7. **MCP externo** — conectar cualquier servidor MCP vía config (`mcp_servers`), sin tocar código; catálogo curado en `optional-mcps/` vía PR.
8. **Perfiles** (`hermes_cli/profiles.py`) — instancia completa aislada (config, memoria, skills, cron, sesiones) por marca/cliente, sin tocar nada de código.
9. **Webhooks declarativos** (`platforms.webhook.extra.routes` en `config.yaml`) — nueva automatización disparada por evento externo sin escribir código.
10. **App de escritorio — sistema "contrib"** (`apps/desktop/src/app/contrib/`) — páginas completas aportadas por plugin (`AppView: 'extension'`), i18n de plugin vía `plugin-i18n.ts` (NO VERIFICADO en profundidad).

---

## PENDIENTES PARA UNA SIGUIENTE PASADA

Antes de dar este catálogo por definitivo al 100%, conviene una auditoría dedicada (más barata ahora que ya se cubrieron 11 de 15 dominios) de:
- **D3** (plataformas de mensajería) — confirmar `gateway/platforms/base.py`, `ADDING_A_PLATFORM.md`, detalle de `whatsapp_cloud.py` (clave para Instagram).
- **D5** (generación de medios) — es el dominio con más peso para el caso de uso de contenido/redes; confirmar si existe composición de imagen, storage en la nube, y hosting de URLs públicas (ideas #7, #18, #19 quedaron sin verificar por esto).
- **D4 y D12 a fondo** — ya tienen base sólida vía D1/D8/D13, pero faltan los detalles finos pedidos originalmente (endpoints HTTP exactos, diferencia entre los 3 backends de browser).
